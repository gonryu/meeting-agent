"""Action Items Orchestrator — 액션아이템 추출·평가·실행계획 다중 에이전트 파이프라인

harness-100 #88 risk-register 패턴을 액션아이템 라이프사이클에 적용:

  1. extractor (Haiku)            — 회의록(표/자유형식 모두)에서 구조화 액션 추출
  2. assessor (Sonnet)        ┐
                               ├─ 병렬 실행 (각 액션에 대해)
  3. response_planner (Sonnet) ┘
  4. (호출 시점) dm_writer (Sonnet) — 담당자별 DM 메시지 생성

실패 시 폴백: 호출부에서 기존 단일 호출 로직(extract_action_items_prompt) 사용.
환경변수 ACTION_ITEMS_ORCHESTRATOR_ENABLED=false 로 비활성화 가능.
"""
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Callable, Optional

import anthropic

log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

_TEMPLATES_DIR = Path(__file__).parent.parent / "prompts" / "templates" / "action_items"

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_HAIKU = "claude-haiku-4-5"
_SONNET = "claude-sonnet-4-5"

_SYSTEM_PROMPT = """\
당신은 회의 액션아이템 라이프사이클 다중 에이전트 시스템의 일부입니다. 다음 원칙을 반드시 준수하세요:

1. **사실 기반**: 회의록에 명시된 내용만 활용합니다. 담당자·기한 추측 금지.
2. **JSON 출력 시 형식 엄수**: JSON만 출력하라고 지시받은 경우, 코드펜스·설명 없이 순수 JSON 객체만 출력합니다.
3. **빈 결과 허용**: 정보가 없으면 빈 배열·빈 문자열·null로 두세요.
4. **점수·등급 일관성**: P×I 산정과 severity 매핑은 정확히 따르세요.
"""


# ── 유틸 ──────────────────────────────────────────────────────


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


# ── 단계 1: 추출 (Haiku) ──────────────────────────────────────


def _stage_extract(*, internal_minutes: str, meeting_title: str,
                   meeting_date: str) -> list[dict]:
    template = _load_template("extractor")
    today = datetime.now(KST).strftime("%Y-%m-%d")
    prompt = _render(
        template,
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        today=today,
        internal_minutes=internal_minutes,
    )
    raw = _call_llm(prompt, model=_HAIKU, max_tokens=4096)
    try:
        data = _parse_json(raw)
        items = data.get("action_items", [])
        if not isinstance(items, list):
            return []
        return items
    except json.JSONDecodeError as e:
        log.warning(f"action extractor JSON 파싱 실패: {e} / 원문: {raw[:200]}")
        return []


# ── 단계 2: 평가 (Sonnet) ─────────────────────────────────────


def _stage_assess(*, items: list[dict], meeting_title: str,
                  meeting_date: str) -> list[dict]:
    if not items:
        return []
    template = _load_template("assessor")
    prompt = _render(
        template,
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        action_items=json.dumps(items, ensure_ascii=False, indent=2),
    )
    raw = _call_llm(prompt, model=_SONNET, max_tokens=4096)
    try:
        data = _parse_json(raw)
        return data.get("assessments", [])
    except json.JSONDecodeError as e:
        log.warning(f"action assessor JSON 파싱 실패: {e} / 원문: {raw[:200]}")
        return []


# ── 단계 3: 대응·모니터링 계획 (Sonnet) ────────────────────────


def _stage_plan(*, enriched_items: list[dict], meeting_title: str,
                meeting_date: str) -> list[dict]:
    if not enriched_items:
        return []
    template = _load_template("response_planner")
    today = datetime.now(KST).strftime("%Y-%m-%d")
    prompt = _render(
        template,
        meeting_title=meeting_title,
        meeting_date=meeting_date,
        today=today,
        enriched_items=json.dumps(enriched_items, ensure_ascii=False, indent=2),
    )
    raw = _call_llm(prompt, model=_SONNET, max_tokens=4096)
    try:
        data = _parse_json(raw)
        return data.get("plans", [])
    except json.JSONDecodeError as e:
        log.warning(f"action response_planner JSON 파싱 실패: {e} / 원문: {raw[:200]}")
        return []


# ── 병합 헬퍼 ─────────────────────────────────────────────────


def _merge_assessment(items: list[dict], assessments: list[dict]) -> list[dict]:
    """입력 items + 평가 결과를 task 인덱스 매칭으로 병합.

    LLM이 순서를 유지하지 못해도 task 문자열 비교로 보정.
    """
    by_task = {a.get("task", ""): a for a in assessments}
    out: list[dict] = []
    for idx, item in enumerate(items):
        merged = dict(item)
        # task로 매칭 우선, 실패 시 인덱스 폴백
        a = by_task.get(item.get("task", ""))
        if a is None and idx < len(assessments):
            a = assessments[idx]
        if a:
            merged["probability"] = a.get("probability")
            merged["impact"] = a.get("impact")
            merged["risk_score"] = a.get("risk_score")
            merged["severity"] = a.get("severity")
            merged["escalation_path"] = a.get("escalation_path") or {}
            merged["assessment_rationale"] = a.get("rationale", "")
        out.append(merged)
    return out


def _merge_plan(enriched: list[dict], plans: list[dict]) -> list[dict]:
    by_task = {p.get("task", ""): p for p in plans}
    out: list[dict] = []
    for idx, item in enumerate(enriched):
        merged = dict(item)
        p = by_task.get(item.get("task", ""))
        if p is None and idx < len(plans):
            p = plans[idx]
        if p:
            merged["success_indicator"] = p.get("success_indicator", "")
            merged["monitoring_cadence"] = p.get("monitoring_cadence", "")
            merged["next_check_date"] = p.get("next_check_date")
            merged["secondary_risks"] = p.get("secondary_risks") or []
        out.append(merged)
    return out


# ── 메인 진입점 ──────────────────────────────────────────────


def is_enabled() -> bool:
    """환경변수로 액션아이템 오케스트레이터 활성/비활성 토글"""
    return os.getenv("ACTION_ITEMS_ORCHESTRATOR_ENABLED", "true").lower() != "false"


def extract_and_enrich(
    internal_minutes_md: str,
    *,
    meeting_title: str,
    meeting_date: str,
    fallback: Optional[Callable[[], list]] = None,
) -> list[dict]:
    """액션아이템 3단계 파이프라인.

    Args:
        internal_minutes_md: 내부용 회의록 마크다운
        meeting_title: 회의 제목
        meeting_date: 회의 일자(`YYYY-MM-DD` 또는 자유 형식 문자열)
        fallback: 실패 시 호출할 폴백 함수. 기본 액션아이템 리스트 반환해야 함.

    Returns:
        풍부화된 액션아이템 dict 리스트. 실패 시 fallback() 결과 또는 빈 리스트.

    Raises:
        예외를 외부로 던지지 않습니다. 실패 시 fallback 사용.
    """
    log.info(f"Action Items Orchestrator 시작: {meeting_title}")
    t0 = datetime.now()

    try:
        # Stage 1: extract (Haiku)
        items = _stage_extract(
            internal_minutes=internal_minutes_md,
            meeting_title=meeting_title,
            meeting_date=meeting_date,
        )
        t1 = datetime.now()
        log.info(f"  [1/3] extract 완료 ({(t1-t0).total_seconds():.1f}s, "
                 f"items={len(items)})")

        if not items:
            log.info("  추출된 액션아이템 없음 — 빈 리스트 반환")
            return []

        # Stage 2/3: assess + plan 병렬 실행
        # — assess와 plan은 의존 관계가 있지만(plan이 severity 필요),
        #    plan은 enriched_items(=items + 가짜 severity)도 처리 가능하도록 함.
        #    안정성을 위해 순차 실행: assess → merge → plan.
        assessments = _stage_assess(
            items=items,
            meeting_title=meeting_title,
            meeting_date=meeting_date,
        )
        t2 = datetime.now()
        log.info(f"  [2/3] assess 완료 ({(t2-t1).total_seconds():.1f}s, "
                 f"assessments={len(assessments)})")

        enriched = _merge_assessment(items, assessments)

        plans = _stage_plan(
            enriched_items=enriched,
            meeting_title=meeting_title,
            meeting_date=meeting_date,
        )
        t3 = datetime.now()
        log.info(f"  [3/3] plan 완료 ({(t3-t2).total_seconds():.1f}s, "
                 f"plans={len(plans)})")

        final = _merge_plan(enriched, plans)
        log.info(f"Action Items Orchestrator 완료 — 총 {(t3-t0).total_seconds():.1f}s, "
                 f"최종 {len(final)}건")
        return final

    except Exception as e:
        log.warning(f"Action Items Orchestrator 실패, 폴백 사용: {e}")
        if fallback:
            try:
                return fallback() or []
            except Exception as e2:
                log.warning(f"폴백 호출도 실패: {e2}")
                return []
        return []


# ── DM 메시지 생성 ────────────────────────────────────────────


def format_owner_dm(
    item: dict,
    *,
    meeting_title: str,
    meeting_url: str = "",
) -> str:
    """단일 액션아이템에 대해 담당자 DM 메시지 텍스트(Slack mrkdwn) 생성.

    실패 시 기본 포맷으로 폴백.
    """
    try:
        template = _load_template("dm_writer")
        prompt = _render(
            template,
            meeting_title=meeting_title,
            meeting_url=meeting_url or "",
            item=json.dumps(item, ensure_ascii=False, indent=2),
        )
        text = _call_llm(prompt, model=_SONNET, max_tokens=1024)
        return text.strip()
    except Exception as e:
        log.warning(f"DM writer 실패, 기본 포맷 사용: {e}")
        # 안전 폴백 — 최소 필드만 사용
        sev = item.get("severity") or ""
        emoji = {"Critical": "🔴", "High": "🟠",
                 "Medium": "🟡", "Low": "🟢"}.get(sev, "•")
        lines = [f"{emoji} *{meeting_title}* 액션아이템",
                 f"*{item.get('task', '')}*"]
        if item.get("due"):
            lines.append(f"📅 *기한*: {item['due']}")
        if item.get("success_indicator"):
            lines.append(f"🎯 *완료 기준*: {item['success_indicator']}")
        if meeting_url:
            lines.append(f"📄 <{meeting_url}|회의록 보기>")
        return "\n".join(lines)
