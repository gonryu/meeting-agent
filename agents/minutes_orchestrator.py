"""Minutes Orchestrator — 회의록 생성 다중 에이전트 파이프라인

harness-100 #84 meeting-strategist, #81 technical-writer, #88 risk-register, #82 report-generator
패턴을 본 서비스 구조에 맞춰 Python 프롬프트 체인으로 적용.

파이프라인:
  1. content_organizer (Haiku)        — 트랜스크립트·노트 정리, 주제·발언자 추출
  2. decision_extractor (Sonnet)  ┐
  3. action_extractor (Sonnet)    ├─ 병렬 실행
  4. discussion_writer (Sonnet)   ┘
  5. minutes_assembler (Sonnet)       — 최종 마크다운 조립

실패 시 폴백: 호출부에서 기존 _generate_minutes() 단일 호출로 자동 전환.
환경변수 MINUTES_ORCHESTRATOR_ENABLED=false 로 비활성화 가능.
"""
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

_TEMPLATES_DIR = Path(__file__).parent.parent / "prompts" / "templates" / "minutes"

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_HAIKU = "claude-haiku-4-5"
_SONNET = "claude-sonnet-4-5"

_SYSTEM_PROMPT = """\
당신은 회의록 작성 다중 에이전트 시스템의 일부입니다. 다음 원칙을 반드시 준수하세요:

1. **사실 기반**: 제공된 자료에 실제로 언급된 내용만 활용합니다. 유추·추론·창작 금지.
2. **JSON 출력 시 형식 엄수**: JSON만 출력하라고 지시받은 경우, 코드펜스·설명 없이 순수 JSON 객체만 출력합니다.
3. **빈 결과 허용**: 정보가 없으면 빈 배열·빈 문자열로 두세요. 채우려고 추측하지 마세요.
4. **불명확 처리**: 들리지 않거나 맥락 없는 부분은 `[불명확]`으로 표시합니다.
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


def _build_sources_block(transcript_text: str, notes_text: str) -> str:
    """content_organizer.md의 {{sources}} 자리에 들어갈 원본 자료 블록 생성"""
    parts = []
    if transcript_text:
        parts.append(f"## 트랜스크립트\n\n{transcript_text}")
    if notes_text:
        parts.append(f"## 수동 노트\n\n{notes_text}")
    return "\n\n".join(parts) if parts else "(자료 없음)"


# ── 단계 1: content_organizer ─────────────────────────────────────


def _stage_organize(*, title: str, date: str, attendees: str,
                    transcript_text: str, notes_text: str) -> dict:
    template = _load_template("content_organizer")
    prompt = _render(
        template,
        title=title,
        date=date,
        attendees=attendees,
        sources=_build_sources_block(transcript_text, notes_text),
    )
    raw = _call_llm(prompt, model=_HAIKU, max_tokens=4096)
    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        log.warning(f"content_organizer JSON 파싱 실패: {e} / 원문: {raw[:200]}")
        # 최소 구조로 폴백
        return {
            "speakers": [],
            "topics": [{
                "title": title,
                "summary": "",
                "key_points": [],
                "raw_excerpts": [(transcript_text or notes_text or "")[:2000]],
            }],
            "decision_hints": [],
            "action_hints": [],
            "unresolved_issues": [],
            "internal_observations": [],
        }


# ── 단계 2/3/4: 병렬 실행 가능한 추출 단계 ─────────────────────────


def _stage_extract_decisions(*, title: str, attendees: str,
                              organized: dict) -> dict:
    template = _load_template("decision_extractor")
    prompt = _render(
        template,
        title=title,
        attendees=attendees,
        organized_content=json.dumps(organized, ensure_ascii=False, indent=2),
    )
    raw = _call_llm(prompt, model=_SONNET, max_tokens=4096)
    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        log.warning(f"decision_extractor JSON 파싱 실패: {e}")
        return {"decisions": []}


def _stage_extract_actions(*, title: str, attendees: str,
                            organized: dict) -> dict:
    template = _load_template("action_extractor")
    today = datetime.now(KST).strftime("%Y-%m-%d")
    prompt = _render(
        template,
        title=title,
        attendees=attendees,
        today=today,
        organized_content=json.dumps(organized, ensure_ascii=False, indent=2),
    )
    raw = _call_llm(prompt, model=_SONNET, max_tokens=4096)
    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        log.warning(f"action_extractor JSON 파싱 실패: {e}")
        return {"action_items": []}


def _stage_write_discussion(*, title: str, attendees: str,
                             organized: dict) -> str:
    template = _load_template("discussion_writer")
    prompt = _render(
        template,
        title=title,
        attendees=attendees,
        organized_content=json.dumps(organized, ensure_ascii=False, indent=2),
    )
    return _call_llm(prompt, model=_SONNET, max_tokens=6144)


# ── 단계 5: 최종 조립 ────────────────────────────────────────────


def _stage_assemble(*, title: str, date: str, attendees: str,
                    organized: dict, decisions: dict, action_items: dict,
                    discussion_body: str) -> str:
    template = _load_template("minutes_assembler")
    prompt = _render(
        template,
        title=title,
        date=date,
        attendees=attendees,
        organized_content=json.dumps(organized, ensure_ascii=False, indent=2),
        decisions=json.dumps(decisions, ensure_ascii=False, indent=2),
        action_items=json.dumps(action_items, ensure_ascii=False, indent=2),
        discussion_body=discussion_body or "(논의 내용 없음)",
    )
    return _call_llm(prompt, model=_SONNET, max_tokens=8192)


# ── 메인 진입점 ─────────────────────────────────────────────────


def is_enabled() -> bool:
    """환경변수로 오케스트레이터 활성/비활성 토글"""
    return os.getenv("MINUTES_ORCHESTRATOR_ENABLED", "true").lower() != "false"


def generate_internal_minutes(*, title: str, date: str, attendees: str,
                               transcript_text: str, notes_text: str) -> str:
    """내부용 회의록 생성 (5단계 파이프라인).

    Returns:
        완성된 내부용 회의록 마크다운.

    Raises:
        Exception: 어떤 단계든 실패 시. 호출부가 캐치하여 폴백 처리.
    """
    log.info(f"Minutes Orchestrator 시작: {title}")
    t0 = datetime.now()

    # Stage 1: organize
    organized = _stage_organize(
        title=title, date=date, attendees=attendees,
        transcript_text=transcript_text, notes_text=notes_text,
    )
    t1 = datetime.now()
    log.info(f"  [1/5] organize 완료 ({(t1-t0).total_seconds():.1f}s, "
             f"topics={len(organized.get('topics', []))}, "
             f"speakers={len(organized.get('speakers', []))})")

    # Stage 2/3/4: parallel extract & write
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_decisions = pool.submit(_stage_extract_decisions,
                                   title=title, attendees=attendees,
                                   organized=organized)
        f_actions = pool.submit(_stage_extract_actions,
                                 title=title, attendees=attendees,
                                 organized=organized)
        f_discussion = pool.submit(_stage_write_discussion,
                                    title=title, attendees=attendees,
                                    organized=organized)
        decisions = f_decisions.result()
        action_items = f_actions.result()
        discussion_body = f_discussion.result()

    t2 = datetime.now()
    log.info(f"  [2-4/5] extract 병렬 완료 ({(t2-t1).total_seconds():.1f}s, "
             f"decisions={len(decisions.get('decisions', []))}, "
             f"actions={len(action_items.get('action_items', []))})")

    # Stage 5: assemble
    final_md = _stage_assemble(
        title=title, date=date, attendees=attendees,
        organized=organized, decisions=decisions, action_items=action_items,
        discussion_body=discussion_body,
    )
    t3 = datetime.now()
    log.info(f"  [5/5] assemble 완료 ({(t3-t2).total_seconds():.1f}s) "
             f"— 총 {(t3-t0).total_seconds():.1f}s, {len(final_md):,}자")

    return final_md
