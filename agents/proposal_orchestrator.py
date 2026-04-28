"""Proposal Orchestrator — 제안서 작성 다중 에이전트 파이프라인

harness-100 #86 proposal-writer (client-analyst / solution-architect /
differentiator / pricing-strategist / proposal-designer) 패턴을 본 서비스
구조에 맞춰 Python 프롬프트 체인으로 적용.

파이프라인:
  1. client_analysis (Sonnet)         — 고객 이해, 의사결정 구조, 결정 기준
  2. solution_architecture (Sonnet) ┐
  3. differentiation (Sonnet)       ├─ 1단계 결과 공유 후 병렬 실행
  4. pricing_strategy (Sonnet)      ┘
  5. proposal_writer (Sonnet)         — 최종 제안서 마크다운 조립

진입점:
  - generate_outline()        : 1단계만 — 사용자 검토용 짧은 개요 반환
  - generate_full_proposal()  : 5단계 전체 — 최종 제안서 마크다운
  - revise_proposal()         : Sonnet 단발 호출로 사용자 수정 요청 반영

실패 시 폴백: 호출부에서 기존 단일 호출 경로로 자동 전환.
환경변수 PROPOSAL_ORCHESTRATOR_ENABLED=false 로 비활성화 가능.
"""
from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

_TEMPLATES_DIR = Path(__file__).parent.parent / "prompts" / "templates" / "proposal"

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_HAIKU = "claude-haiku-4-5"
_SONNET = "claude-sonnet-4-5"

_SYSTEM_PROMPT = """\
당신은 B2B 제안서 작성 다중 에이전트 시스템의 일부입니다. 다음 원칙을 준수하세요:

1. **사실 기반**: 회의록·업체 정보·회사 지식에 명시된 내용만 활용합니다. 임의 추론·창작 금지.
2. **JSON 출력 시 형식 엄수**: JSON만 출력하라고 지시받은 경우, 코드펜스·설명 없이 순수 JSON 객체만.
3. **빈 결과 허용**: 정보가 없으면 빈 배열·빈 문자열로 두세요. 채우려고 추측하지 마세요.
4. **고객 언어**: 자사 기술 용어가 아닌 고객의 비즈니스 용어로 작성합니다.
5. **한국어 출력**: 별도 지시가 없으면 한국어로 작성합니다.
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


# ── 단계 1: client_analysis ──────────────────────────────────────


def _stage_client_analysis(*, title: str, date_str: str,
                           minutes_body: str, company_info: str,
                           previous_context: str, client_hint: str) -> dict:
    template = _load_template("client_analysis")
    prompt = _render(
        template,
        title=title,
        date_str=date_str,
        minutes_body=minutes_body or "(회의록 없음)",
        company_info=company_info or "(없음)",
        previous_context=previous_context or "(없음)",
        client_hint=client_hint or "(없음)",
    )
    raw = _call_llm(prompt, model=_SONNET, max_tokens=4096)
    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        log.warning(f"client_analysis JSON 파싱 실패: {e} / 원문: {raw[:200]}")
        return {
            "client_company": "",
            "client_unit": "",
            "client_industry": "",
            "needs": [],
            "decision_makers": [],
            "decision_criteria": [],
            "concerns": [],
            "success_criteria": [],
            "strategic_direction": "",
            "outline_summary": (raw[:500] if raw else ""),
        }


# ── 단계 2/3/4: 병렬 실행 가능한 추출 단계 ───────────────────────


def _stage_solution(*, title: str, client_analysis: dict,
                    minutes_body: str, company_info: str,
                    knowledge: str) -> dict:
    template = _load_template("solution_architecture")
    prompt = _render(
        template,
        title=title,
        client_analysis=json.dumps(client_analysis, ensure_ascii=False, indent=2),
        minutes_body=minutes_body or "(회의록 없음)",
        company_info=company_info or "(없음)",
        knowledge=knowledge or "(없음)",
    )
    raw = _call_llm(prompt, model=_SONNET, max_tokens=4096)
    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        log.warning(f"solution_architecture JSON 파싱 실패: {e}")
        return {
            "summary": "",
            "core_value": "",
            "components": [],
            "methodology": "",
            "phases": [],
            "team_roles": [],
            "assumptions": [],
            "risks": [],
        }


def _stage_differentiation(*, title: str, client_analysis: dict,
                           solution_architecture: dict,
                           knowledge: str, company_info: str) -> dict:
    template = _load_template("differentiation")
    prompt = _render(
        template,
        title=title,
        client_analysis=json.dumps(client_analysis, ensure_ascii=False, indent=2),
        solution_architecture=json.dumps(solution_architecture, ensure_ascii=False, indent=2),
        knowledge=knowledge or "(없음)",
        company_info=company_info or "(없음)",
    )
    raw = _call_llm(prompt, model=_SONNET, max_tokens=4096)
    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        log.warning(f"differentiation JSON 파싱 실패: {e}")
        return {
            "win_themes": [],
            "usps": [],
            "concern_responses": [],
            "avoid_topics": [],
            "narrative": "",
        }


def _stage_pricing(*, title: str, client_analysis: dict,
                   solution_architecture: dict, knowledge: str) -> dict:
    template = _load_template("pricing_strategy")
    prompt = _render(
        template,
        title=title,
        client_analysis=json.dumps(client_analysis, ensure_ascii=False, indent=2),
        solution_architecture=json.dumps(solution_architecture, ensure_ascii=False, indent=2),
        knowledge=knowledge or "(없음)",
    )
    raw = _call_llm(prompt, model=_SONNET, max_tokens=4096)
    try:
        result = _parse_json(raw)
    except json.JSONDecodeError as e:
        log.warning(f"pricing_strategy JSON 파싱 실패: {e}")
        result = {
            "draft_for_review": True,
            "review_note": "이 가격은 내부 검토용 초안입니다.",
            "positioning": "",
            "model": "",
            "tiers": [],
            "phased_option": {"available": False, "description": ""},
            "roi_message": "",
            "negotiation_levers": [],
        }
    # 안전장치 — draft_for_review 누락 시 강제 True
    result.setdefault("draft_for_review", True)
    result.setdefault(
        "review_note",
        "이 가격은 내부 검토용 초안입니다. 실제 제출 전 사용자 확인 필요.",
    )
    return result


# ── 단계 5: 최종 조립 ────────────────────────────────────────────


def _stage_writer(*, title: str, date_str: str, prior_outline: str,
                  client_analysis: dict, solution_architecture: dict,
                  differentiation: dict, pricing_strategy: dict,
                  minutes_body: str, company_info: str) -> str:
    template = _load_template("proposal_writer")
    prompt = _render(
        template,
        title=title,
        date_str=date_str,
        prior_outline=prior_outline or "(없음)",
        client_analysis=json.dumps(client_analysis, ensure_ascii=False, indent=2),
        solution_architecture=json.dumps(solution_architecture, ensure_ascii=False, indent=2),
        differentiation=json.dumps(differentiation, ensure_ascii=False, indent=2),
        pricing_strategy=json.dumps(pricing_strategy, ensure_ascii=False, indent=2),
        minutes_body=minutes_body or "(회의록 없음)",
        company_info=company_info or "(없음)",
    )
    return _call_llm(prompt, model=_SONNET, max_tokens=8192)


# ── 메인 진입점 ─────────────────────────────────────────────────


def is_enabled() -> bool:
    """환경변수로 오케스트레이터 활성/비활성 토글"""
    return os.getenv("PROPOSAL_ORCHESTRATOR_ENABLED", "true").lower() != "false"


def generate_outline(*, title: str, date_str: str, minutes_body: str,
                     company_info: str = "", previous_context: str = "",
                     client_hint: str = "") -> dict:
    """1단계만 실행해 사용자 검토용 짧은 개요 반환.

    Returns:
        client_analysis JSON dict. `outline_summary` 필드에 사용자 검토용 텍스트 포함.

    Raises:
        Exception: 단계 실패 시. 호출부가 캐치하여 폴백 처리.
    """
    log.info(f"Proposal Orchestrator 개요 생성: {title}")
    t0 = datetime.now()
    analysis = _stage_client_analysis(
        title=title, date_str=date_str,
        minutes_body=minutes_body, company_info=company_info,
        previous_context=previous_context, client_hint=client_hint,
    )
    t1 = datetime.now()
    log.info(f"  [1/1] client_analysis 완료 ({(t1-t0).total_seconds():.1f}s, "
             f"needs={len(analysis.get('needs', []))}, "
             f"makers={len(analysis.get('decision_makers', []))})")
    return analysis


def generate_full_proposal(*, title: str, date_str: str, minutes_body: str,
                           company_info: str = "", knowledge: str = "",
                           previous_context: str = "", client_hint: str = "",
                           prior_outline: str = "",
                           prior_analysis: dict | None = None) -> dict:
    """5단계 전체 파이프라인 실행 → 최종 제안서 마크다운.

    Args:
        prior_analysis: generate_outline()의 산출물을 재사용할 경우 전달.
                        지정 시 1단계 스킵.

    Returns:
        {
            "proposal_md": 최종 제안서 마크다운,
            "client_analysis": dict,
            "solution_architecture": dict,
            "differentiation": dict,
            "pricing_strategy": dict,
        }
    """
    log.info(f"Proposal Orchestrator 시작: {title}")
    t0 = datetime.now()

    # Stage 1: client_analysis (재사용 가능)
    if prior_analysis:
        analysis = prior_analysis
        log.info("  [1/5] client_analysis 재사용")
        t1 = datetime.now()
    else:
        analysis = _stage_client_analysis(
            title=title, date_str=date_str,
            minutes_body=minutes_body, company_info=company_info,
            previous_context=previous_context, client_hint=client_hint,
        )
        t1 = datetime.now()
        log.info(f"  [1/5] client_analysis 완료 ({(t1-t0).total_seconds():.1f}s)")

    # Stage 2: solution_architecture (단독 — 3·4가 이 결과를 참조하므로 먼저)
    solution = _stage_solution(
        title=title, client_analysis=analysis,
        minutes_body=minutes_body, company_info=company_info,
        knowledge=knowledge,
    )
    t2 = datetime.now()
    log.info(f"  [2/5] solution_architecture 완료 ({(t2-t1).total_seconds():.1f}s, "
             f"components={len(solution.get('components', []))}, "
             f"phases={len(solution.get('phases', []))})")

    # Stage 3·4: differentiation + pricing 병렬 (둘 다 1·2 결과만 참조)
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_diff = pool.submit(
            _stage_differentiation,
            title=title, client_analysis=analysis,
            solution_architecture=solution,
            knowledge=knowledge, company_info=company_info,
        )
        f_pricing = pool.submit(
            _stage_pricing,
            title=title, client_analysis=analysis,
            solution_architecture=solution, knowledge=knowledge,
        )
        differentiation = f_diff.result()
        pricing = f_pricing.result()

    t3 = datetime.now()
    log.info(f"  [3-4/5] differentiation+pricing 병렬 완료 "
             f"({(t3-t2).total_seconds():.1f}s, "
             f"win_themes={len(differentiation.get('win_themes', []))}, "
             f"tiers={len(pricing.get('tiers', []))})")

    # Stage 5: proposal_writer
    proposal_md = _stage_writer(
        title=title, date_str=date_str, prior_outline=prior_outline,
        client_analysis=analysis, solution_architecture=solution,
        differentiation=differentiation, pricing_strategy=pricing,
        minutes_body=minutes_body, company_info=company_info,
    )
    t4 = datetime.now()
    log.info(f"  [5/5] proposal_writer 완료 ({(t4-t3).total_seconds():.1f}s) "
             f"— 총 {(t4-t0).total_seconds():.1f}s, {len(proposal_md):,}자")

    return {
        "proposal_md": proposal_md,
        "client_analysis": analysis,
        "solution_architecture": solution,
        "differentiation": differentiation,
        "pricing_strategy": pricing,
    }


def revise_proposal(*, current_proposal_md: str, user_request: str) -> str:
    """사용자 수정 요청을 반영해 제안서를 재작성.

    Slack 스레드 답글 등에서 들어온 자연어 수정 요청을 단일 Sonnet 호출로 처리.
    기존 UX(반복 수정)와 호환되도록 단발 LLM 호출 형태를 유지.
    """
    log.info("Proposal Orchestrator 수정 요청 처리")
    t0 = datetime.now()
    prompt = (
        "다음 제안서를 사용자 수정 요청에 따라 수정해주세요. 반드시 한국어로.\n\n"
        f"[기존 제안서]\n{current_proposal_md}\n\n"
        f"[사용자 수정 요청]\n{user_request}\n\n"
        "수정 규칙:\n"
        "1. 요청된 부분만 정확히 반영하고, 나머지 내용·구조·섹션 헤더(##, ###)는 유지하세요.\n"
        "2. 마크다운 형식 그대로 유지.\n"
        "3. 가격이 검토용 초안 표시(`draft_for_review` 안내 문구)로 들어가 있다면 그 표시는 보존하세요.\n"
        "4. 수정 결과 전체 마크다운만 반환 (설명 없이).\n"
    )
    out = _call_llm(prompt, model=_SONNET, max_tokens=8192)
    log.info(f"  수정 완료 ({(datetime.now()-t0).total_seconds():.1f}s, {len(out):,}자)")
    return out
