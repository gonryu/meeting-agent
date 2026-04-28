"""Weekly Report Orchestrator — 주간 Trello 보고서 강화 파이프라인

harness-100 #82 report-generator (analyst / executive-summarizer / report-writer)
+ #88 risk-register 패턴을 본 서비스 구조에 맞춰 적용.

기존 trello_report.send_weekly_report() 가 수집한 데이터(_collect 산출 + 카드별
요약이 포함된 base markdown)를 입력으로 받아 다음을 만들어냅니다:

파이프라인:
  1. trend_analyst (Sonnet)      ┐  → 가속/정체/블로커/모멘텀
  2. risk_highlighter (Sonnet)   ┘  → 지연/정체/리스크 카드 (병렬)
  3. executive_summary (Sonnet)  ┐  → Slack 상단 5줄 요약
  4. detailed_writer (Sonnet)    ┘  → Google Docs 상세 본문 (병렬)

진입점: enrich_report(collected) → {executive_summary_md, detailed_md, trends, risks}

실패 시 폴백: 호출부에서 기존 mechanical 본문/요약으로 자동 전환.
환경변수 WEEKLY_REPORT_ORCHESTRATOR_ENABLED=false 로 비활성화 가능.
"""
from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

_TEMPLATES_DIR = Path(__file__).parent.parent / "prompts" / "templates" / "weekly_report"

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_HAIKU = "claude-haiku-4-5"
_SONNET = "claude-sonnet-4-5"

_SYSTEM_PROMPT = """\
당신은 Trello 주간 보고서 다중 에이전트 시스템의 일부입니다. 다음 원칙을 준수하세요:

1. **사실 기반**: 입력 데이터(액션·기한·통계)에 명시된 내용만 활용합니다. 추론·창작 금지.
2. **JSON 출력 시 형식 엄수**: JSON만 출력하라고 지시받은 경우, 코드펜스·설명 없이 순수 JSON 객체만.
3. **빈 결과 허용**: 신호가 없으면 빈 배열로 두세요. 채우려고 추측하지 마세요.
4. **카드명·보드명 보존**: 식별 가능하도록 입력 데이터의 이름을 그대로 인용하세요.
5. **한국어 출력**.
"""


def _load_template(name: str) -> str:
    return (_TEMPLATES_DIR / f"{name}.md").read_text(encoding="utf-8")


def _render(template: str, **vars) -> str:
    out = template
    for k, v in vars.items():
        out = out.replace("{{" + k + "}}", str(v) if v is not None else "")
    return out


def _call_llm(prompt: str, *, model: str, max_tokens: int = 4096) -> str:
    msg = _claude.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _parse_json(text: str) -> dict:
    """LLM 응답에서 JSON을 안전하게 추출. 코드펜스가 섞여 와도 처리."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    return json.loads(text)


def _fmt_dt(dt: datetime) -> str:
    """입력 datetime을 KST 'YYYY-MM-DD' 로."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KST).strftime("%Y-%m-%d")


def _build_stats_summary(actions: list[dict], upcoming_count: int,
                         actors_count: int) -> str:
    new_cards = sum(1 for a in actions if a.get("kind") == "card_created")
    comments = sum(1 for a in actions if a.get("kind") == "comment")
    completed = sum(1 for a in actions if a.get("kind") == "checkitem_completed")
    return (
        f"신규 카드 {new_cards} · 코멘트 {comments} · "
        f"완료 항목 {completed} · 다음주 기한 {upcoming_count} · "
        f"참여 {actors_count}명"
    )


def _slim_actions(actions: list[dict], max_items: int = 200) -> list[dict]:
    """LLM 입력 절약 — 액션 데이터 핵심 필드만 추림."""
    out = []
    for a in actions[:max_items]:
        out.append({
            "kind": a.get("kind"),
            "when": a.get("when"),
            "actor": a.get("actor"),
            "card_name": a.get("card_name"),
            "card_url": a.get("card_url"),
            "detail": (a.get("detail") or "")[:600],
        })
    return out


def _slim_upcoming(upcoming_cards: list[dict],
                   upcoming_items: list[dict]) -> list[dict]:
    out = []
    for c in upcoming_cards:
        out.append({
            "type": "card_due",
            "card_name": c.get("card_name"),
            "card_url": c.get("card_url"),
            "due": c.get("due"),
        })
    for it in upcoming_items:
        out.append({
            "type": "item_due",
            "card_name": it.get("card_name"),
            "card_url": it.get("card_url"),
            "checklist_name": it.get("checklist_name"),
            "item_name": it.get("item_name"),
            "due": it.get("due"),
        })
    return out


# ── 단계 1: trend_analyst ──────────────────────────────────────


def _stage_trends(*, collected: dict, prior_context: str = "") -> dict:
    template = _load_template("trend_analyst")
    boards = collected.get("boards", [])
    actions = collected.get("actions", [])
    upcoming_cards = collected.get("upcoming_cards", [])
    upcoming_items = collected.get("upcoming_items", [])
    actors = {a.get("actor") for a in actions if a.get("actor")}

    prompt = _render(
        template,
        workspace_name=collected.get("workspace_name", ""),
        since=_fmt_dt(collected.get("since")),
        until=_fmt_dt(collected.get("until")),
        boards_summary=", ".join(b.get("name", "") for b in boards) or "(보드 없음)",
        stats_summary=_build_stats_summary(
            actions, len(upcoming_cards) + len(upcoming_items), len(actors)
        ),
        actions_json=json.dumps(_slim_actions(actions), ensure_ascii=False, indent=2),
        upcoming_json=json.dumps(
            _slim_upcoming(upcoming_cards, upcoming_items),
            ensure_ascii=False, indent=2,
        ),
        prior_context=prior_context or "(없음)",
    )
    raw = _call_llm(prompt, model=_SONNET, max_tokens=4096)
    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        log.warning(f"trend_analyst JSON 파싱 실패: {e}")
        return {
            "accelerating": [],
            "stalling": [],
            "blockers": [],
            "momentum_signals": [],
        }


# ── 단계 2: risk_highlighter ───────────────────────────────────


def _stage_risks(*, collected: dict) -> dict:
    template = _load_template("risk_highlighter")
    actions = collected.get("actions", [])
    upcoming_cards = collected.get("upcoming_cards", [])
    upcoming_items = collected.get("upcoming_items", [])
    today = datetime.now(KST).strftime("%Y-%m-%d")

    # 미완료 due 항목 — collected 단계에서 별도 추출되지 않으므로 actions/upcoming에서 보강
    # (실제 지연 판단은 LLM이 today와 due 비교해 수행)
    due_items = []
    for c in upcoming_cards:
        due_items.append({
            "card_name": c.get("card_name"),
            "card_url": c.get("card_url"),
            "due": c.get("due"),
        })
    for it in upcoming_items:
        due_items.append({
            "card_name": it.get("card_name"),
            "card_url": it.get("card_url"),
            "item_name": it.get("item_name"),
            "checklist_name": it.get("checklist_name"),
            "due": it.get("due"),
        })

    prompt = _render(
        template,
        since=_fmt_dt(collected.get("since")),
        until=_fmt_dt(collected.get("until")),
        today=today,
        actions_json=json.dumps(_slim_actions(actions), ensure_ascii=False, indent=2),
        upcoming_json=json.dumps(
            _slim_upcoming(upcoming_cards, upcoming_items),
            ensure_ascii=False, indent=2,
        ),
        due_items_json=json.dumps(due_items, ensure_ascii=False, indent=2),
    )
    raw = _call_llm(prompt, model=_SONNET, max_tokens=4096)
    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        log.warning(f"risk_highlighter JSON 파싱 실패: {e}")
        return {"delayed": [], "stale": [], "at_risk": []}


# ── 단계 3: executive_summary ──────────────────────────────────


def _stage_exec_summary(*, collected: dict, trends: dict, risks: dict) -> str:
    template = _load_template("executive_summary")
    actions = collected.get("actions", [])
    upcoming_cards = collected.get("upcoming_cards", [])
    upcoming_items = collected.get("upcoming_items", [])
    actors = {a.get("actor") for a in actions if a.get("actor")}
    prompt = _render(
        template,
        workspace_name=collected.get("workspace_name", ""),
        since=_fmt_dt(collected.get("since")),
        until=_fmt_dt(collected.get("until")),
        stats_summary=_build_stats_summary(
            actions, len(upcoming_cards) + len(upcoming_items), len(actors)
        ),
        trends_json=json.dumps(trends, ensure_ascii=False, indent=2),
        risks_json=json.dumps(risks, ensure_ascii=False, indent=2),
    )
    return _call_llm(prompt, model=_SONNET, max_tokens=2048)


# ── 단계 4: detailed_writer ────────────────────────────────────


def _stage_detailed(*, collected: dict, trends: dict, risks: dict,
                    base_report_md: str) -> str:
    template = _load_template("detailed_writer")
    actions = collected.get("actions", [])
    upcoming_cards = collected.get("upcoming_cards", [])
    upcoming_items = collected.get("upcoming_items", [])
    actors = {a.get("actor") for a in actions if a.get("actor")}
    boards = collected.get("boards", [])

    prompt = _render(
        template,
        workspace_name=collected.get("workspace_name", ""),
        since=_fmt_dt(collected.get("since")),
        until=_fmt_dt(collected.get("until")),
        next_start=_fmt_dt(collected.get("next_start")),
        next_end=_fmt_dt(collected.get("next_end")),
        boards_summary=", ".join(b.get("name", "") for b in boards) or "(보드 없음)",
        stats_summary=_build_stats_summary(
            actions, len(upcoming_cards) + len(upcoming_items), len(actors)
        ),
        trends_json=json.dumps(trends, ensure_ascii=False, indent=2),
        risks_json=json.dumps(risks, ensure_ascii=False, indent=2),
        base_report_md=base_report_md or "(기본 본문 없음)",
    )
    return _call_llm(prompt, model=_SONNET, max_tokens=8192)


# ── 메인 진입점 ─────────────────────────────────────────────────


def is_enabled() -> bool:
    """환경변수로 오케스트레이터 활성/비활성 토글"""
    return os.getenv("WEEKLY_REPORT_ORCHESTRATOR_ENABLED", "true").lower() != "false"


def enrich_report(collected: dict, *, base_report_md: str = "",
                  prior_context: str = "") -> dict:
    """수집된 Trello 데이터를 받아 트렌드·리스크·요약·상세 본문 생성.

    Args:
        collected: trello_report._collect() 의 산출물
            (workspace_name / boards / actions / upcoming_cards /
             upcoming_items / since / until / next_start / next_end)
        base_report_md: 기존 _build_full_report() 산출 마크다운
            (카드별 요약 본문 포함 — detailed_writer가 본문 재구성에 사용)
        prior_context: 이전 주 컨텍스트 (선택, 향후 기능)

    Returns:
        {
            "executive_summary_md": Slack 상단 5줄 요약,
            "detailed_md": Google Docs용 상세 본문 마크다운,
            "trends": dict,
            "risks": dict,
        }

    Raises:
        Exception: 어떤 단계든 실패 시. 호출부가 캐치하여 폴백 처리.
    """
    log.info(f"Weekly Report Orchestrator 시작: "
             f"{collected.get('workspace_name', '?')}")
    t0 = datetime.now()

    # Stage 1+2: trends + risks 병렬 (둘 다 collected만 의존)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_trends = pool.submit(
            _stage_trends, collected=collected, prior_context=prior_context
        )
        f_risks = pool.submit(_stage_risks, collected=collected)
        trends = f_trends.result()
        risks = f_risks.result()
    t1 = datetime.now()
    log.info(
        f"  [1-2/4] trends+risks 병렬 완료 ({(t1-t0).total_seconds():.1f}s, "
        f"accel={len(trends.get('accelerating', []))}, "
        f"stall={len(trends.get('stalling', []))}, "
        f"delayed={len(risks.get('delayed', []))}, "
        f"at_risk={len(risks.get('at_risk', []))})"
    )

    # Stage 3+4: executive_summary + detailed_writer 병렬 (1·2 결과만 의존)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_exec = pool.submit(
            _stage_exec_summary,
            collected=collected, trends=trends, risks=risks,
        )
        f_detail = pool.submit(
            _stage_detailed,
            collected=collected, trends=trends, risks=risks,
            base_report_md=base_report_md,
        )
        executive_summary_md = f_exec.result()
        detailed_md = f_detail.result()
    t2 = datetime.now()
    log.info(
        f"  [3-4/4] exec_summary+detailed 병렬 완료 "
        f"({(t2-t1).total_seconds():.1f}s, "
        f"summary={len(executive_summary_md):,}자, "
        f"detail={len(detailed_md):,}자) — 총 {(t2-t0).total_seconds():.1f}s"
    )

    return {
        "executive_summary_md": executive_summary_md,
        "detailed_md": detailed_md,
        "trends": trends,
        "risks": risks,
    }
