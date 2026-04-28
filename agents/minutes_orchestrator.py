"""Minutes Orchestrator — 회의록 생성 다중 에이전트 파이프라인 (Obsidian 호환)

파이프라인:
  1. content_organizer (Haiku)        — 트랜스크립트·노트 정리, 6범주 추출
  2. decision_extractor (Sonnet)  ┐
  3. action_extractor (Sonnet)    ├─ 병렬 실행 (자사/상대 분리 포함)
  4. discussion_writer (Sonnet)   ┘
  5. minutes_assembler (Sonnet)       — meeting_type 별 템플릿 분기
  6. validator (Sonnet)               — 숫자·기한·자사/상대 정합성 검수

산출 회의록은 YAML frontmatter + Obsidian [[]] 호환 본문.
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
    parts = []
    if transcript_text:
        parts.append(f"## 트랜스크립트\n\n{transcript_text}")
    if notes_text:
        parts.append(f"## 수동 노트\n\n{notes_text}")
    return "\n\n".join(parts) if parts else "(자료 없음)"


# ── 참석자 분류 ────────────────────────────────────────────────


def _internal_domains() -> set[str]:
    raw = os.getenv("INTERNAL_DOMAINS", "parametacorp.com,iconloop.com")
    return {d.strip().lower() for d in raw.split(",") if d.strip()}


def classify_attendees(attendees_raw: list[dict] | None) -> dict:
    """이메일 도메인 기반으로 자사/상대 분리.

    Returns:
        {"our_side": [...], "their_side": [...], "unknown": [...]}
        각 항목은 {"name": str, "email": str, "domain": str}.
    """
    out = {"our_side": [], "their_side": [], "unknown": []}
    if not attendees_raw:
        return out

    internal = _internal_domains()
    for a in attendees_raw:
        email = (a.get("email") or "").strip()
        name = (a.get("displayName") or a.get("name") or "").strip() or email
        domain = email.split("@")[-1].lower() if "@" in email else ""
        entry = {"name": name, "email": email, "domain": domain}
        if not email:
            out["unknown"].append(entry)
        elif domain in internal:
            out["our_side"].append(entry)
        else:
            out["their_side"].append(entry)
    return out


def _attendees_structured_for_prompt(structured: dict) -> str:
    """LLM 프롬프트에 넣을 간략 텍스트 표현."""
    def _names(side: str) -> str:
        items = structured.get(side, [])
        if not items:
            return "(없음)"
        return ", ".join(a["name"] for a in items if a.get("name"))
    return (
        f"자사(our_side): {_names('our_side')}\n"
        f"외부(their_side): {_names('their_side')}\n"
        f"미분류(unknown): {_names('unknown')}"
    )


# ── 단계 1: content_organizer ──────────────────────────────────


def _stage_organize(*, title: str, date: str, attendees: str,
                    attendees_structured: dict, meeting_type: str,
                    transcript_text: str, notes_text: str) -> dict:
    template = _load_template("content_organizer")
    prompt = _render(
        template,
        title=title,
        date=date,
        attendees=attendees,
        attendees_structured=_attendees_structured_for_prompt(attendees_structured),
        meeting_type=meeting_type,
        sources=_build_sources_block(transcript_text, notes_text),
    )
    raw = _call_llm(prompt, model=_HAIKU, max_tokens=4096)
    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        log.warning(f"content_organizer JSON 파싱 실패: {e} / 원문: {raw[:200]}")
        return {
            "speakers": [],
            "topics": [{
                "title": title,
                "summary": "",
                "key_points": [],
                "raw_excerpts": [(transcript_text or notes_text or "")[:2000]],
            }],
            "decision_hints": [],
            "promise_hints": [],
            "numbers": [],
            "proper_nouns": [],
            "unresolved_issues": [],
            "risks": [],
            "internal_observations": [],
        }


# ── 단계 2/3/4 ─────────────────────────────────────────────────


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
                            attendees_structured: dict, meeting_type: str,
                            organized: dict) -> dict:
    template = _load_template("action_extractor")
    today = datetime.now(KST).strftime("%Y-%m-%d")
    prompt = _render(
        template,
        title=title,
        attendees=attendees,
        attendees_structured=_attendees_structured_for_prompt(attendees_structured),
        meeting_type=meeting_type,
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


# ── 단계 5: 최종 조립 (meeting_type 분기) ───────────────────────


def _pick_assembler_template(meeting_type: str) -> str:
    """meeting_type 에 따라 템플릿 이름 선택."""
    if meeting_type in ("vendor", "external", "mixed"):
        return "assembler_external"
    return "assembler_internal"


def _stage_assemble(*, title: str, date: str, attendees: str,
                    attendees_structured: dict, meeting_type: str,
                    organized: dict, decisions: dict, action_items: dict,
                    discussion_body: str) -> str:
    template_name = _pick_assembler_template(meeting_type)
    try:
        template = _load_template(template_name)
    except FileNotFoundError:
        log.warning(f"조립 템플릿 누락 ({template_name}), legacy minutes_assembler 사용")
        template = _load_template("minutes_assembler")

    prompt = _render(
        template,
        title=title,
        date=date,
        attendees=attendees,
        attendees_structured=_attendees_structured_for_prompt(attendees_structured),
        meeting_type=meeting_type,
        organized_content=json.dumps(organized, ensure_ascii=False, indent=2),
        decisions=json.dumps(decisions, ensure_ascii=False, indent=2),
        action_items=json.dumps(action_items, ensure_ascii=False, indent=2),
        discussion_body=discussion_body or "(논의 내용 없음)",
    )
    return _call_llm(prompt, model=_SONNET, max_tokens=8192)


# ── 단계 6: validator ─────────────────────────────────────────


def _stage_validate(*, title: str, meeting_type: str,
                    attendees_structured: dict,
                    transcript_text: str, notes_text: str,
                    assembled_minutes: str) -> str:
    """검수 LLM. 문제가 없으면 입력 회의록을 그대로 반환, 보정이 필요하면 수정본 반환."""
    if os.getenv("MINUTES_VALIDATOR_ENABLED", "true").lower() == "false":
        return assembled_minutes

    try:
        template = _load_template("validator")
    except FileNotFoundError:
        log.info("validator 템플릿 없음 — 검수 단계 생략")
        return assembled_minutes

    prompt = _render(
        template,
        title=title,
        meeting_type=meeting_type,
        attendees_structured=_attendees_structured_for_prompt(attendees_structured),
        sources=_build_sources_block(transcript_text, notes_text),
        assembled_minutes=assembled_minutes,
    )
    try:
        raw = _call_llm(prompt, model=_SONNET, max_tokens=8192)
        result = _parse_json(raw)
    except Exception as e:
        log.warning(f"validator 파싱 실패 — 원본 회의록 유지: {e}")
        return assembled_minutes

    status = (result.get("status") or "").lower()
    issues = result.get("issues") or []
    corrected = (result.get("corrected_minutes") or "").strip()

    if status == "ok" or not corrected:
        log.info(f"검수 OK: {title} (이슈 {len(issues)})")
        return assembled_minutes

    if issues:
        log.info(f"검수 보정 적용: {title} — 이슈={issues}")
    # 검수 결과가 너무 짧으면 무시 (LLM 오작동 방지)
    if len(corrected) < max(200, int(len(assembled_minutes) * 0.5)):
        log.warning("검수 보정본이 비정상적으로 짧음 — 원본 유지")
        return assembled_minutes
    return corrected


# ── frontmatter 자동 생성 ─────────────────────────────────────


def _slugify_title(title: str) -> str:
    """파일명 역할의 frontmatter title 정규화 (공백 → _, 위험 문자 제거)."""
    s = re.sub(r"\s+", "_", (title or "").strip())
    s = re.sub(r"[\\/<>:\"|?*]", "", s)
    return s


def build_frontmatter(*, title: str, date: str, meeting_type: str,
                       attendees_structured: dict,
                       has_transcript: bool, source_basename: str | None = None) -> str:
    """회의록 본문 앞에 붙일 YAML frontmatter 생성.

    source_basename: 트랜스크립트 원본 파일명(확장자 제외) — `[[…_원문]]` 형태로 reference.
    """
    fm_type = "vendor" if meeting_type in ("vendor", "external", "mixed") else "internal"

    # related_entities: 자사·외부 측 이름
    related: list[str] = []
    for a in attendees_structured.get("our_side", []):
        nm = a.get("name")
        if nm and nm not in related:
            related.append(nm)
    for a in attendees_structured.get("their_side", []):
        nm = a.get("name")
        if nm and nm not in related:
            related.append(nm)

    source_refs: list[str] = []
    related_notes: list[str] = []
    if has_transcript and source_basename:
        ref = f"[[{source_basename}]]"
        source_refs.append(ref)
        related_notes.append(ref)

    file_title = _slugify_title(f"{date}_{title}")

    lines = ["---", f"title: {file_title}", f"date: {date}",
             "type: meeting", "stage: structured", "status: draft",
             f"meeting_type: {fm_type}"]

    def _list_block(key: str, items: list[str]) -> list[str]:
        if not items:
            return [f"{key}: []"]
        out = [f"{key}:"]
        for it in items:
            s = it
            if ":" in s or s.startswith("[["):
                s = '"' + s.replace('"', '\\"') + '"'
            out.append(f"  - {s}")
        return out

    lines += _list_block("source_refs", source_refs)
    lines += _list_block("related_entities", related)
    lines += _list_block("related_notes", related_notes)
    lines += _list_block("tags", ["meeting", fm_type])
    lines.append("---")
    lines.append("")
    return "\n".join(lines)


# ── 위키링크 자동 적용 ────────────────────────────────────────


def _apply_wikilinks(body: str) -> str:
    """기본은 no-op. 호출부에서 known entities 와 함께 명시적으로 호출."""
    return body


def apply_wiki_links(body: str, known_entities: list[str] | None) -> str:
    """알려진 엔티티 이름을 본문에서 [[]] 위키링크로 감싼다.

    Drive 의존성을 끊기 위해 이 함수는 엔티티 리스트만 받는다.
    """
    if not known_entities:
        return body
    try:
        from tools.wiki_linker import wrap_entities
        return wrap_entities(body, known_entities)
    except Exception as e:
        log.warning(f"위키링크 적용 실패 (무시): {e}")
        return body


# ── 메인 진입점 ───────────────────────────────────────────────


def is_enabled() -> bool:
    """환경변수로 오케스트레이터 활성/비활성 토글"""
    return os.getenv("MINUTES_ORCHESTRATOR_ENABLED", "true").lower() != "false"


def generate_internal_minutes(*, title: str, date: str, attendees: str,
                               transcript_text: str, notes_text: str,
                               attendees_raw: list[dict] | None = None,
                               meeting_type: str = "internal",
                               known_entities: list[str] | None = None,
                               source_basename: str | None = None) -> str:
    """회의록 생성 (6단계 파이프라인).

    Args:
        title, date, attendees, transcript_text, notes_text — 기본 입력
        attendees_raw — [{email, name|displayName}, ...] (자사/상대 분리에 사용)
        meeting_type — "internal" / "vendor" / "external" / "mixed"
                       내부 회의면 자사/상대 분리·외부 섹션 생략
        known_entities — Contacts 폴더 기반 알려진 엔티티 리스트 (위키링크 자동 적용)
        source_basename — frontmatter source_refs 에 들어갈 트랜스크립트 원본 파일명
                          (확장자 제외, 예: "2026-04-28_Allobank_사전논의_원문")

    Returns:
        YAML frontmatter + Obsidian 호환 회의록 마크다운.

    Raises:
        Exception: 어떤 단계든 실패 시. 호출부가 캐치하여 폴백 처리.
    """
    log.info(f"Minutes Orchestrator 시작: {title} (type={meeting_type})")
    t0 = datetime.now()

    structured = classify_attendees(attendees_raw)

    # Stage 1: organize
    organized = _stage_organize(
        title=title, date=date, attendees=attendees,
        attendees_structured=structured, meeting_type=meeting_type,
        transcript_text=transcript_text, notes_text=notes_text,
    )
    t1 = datetime.now()
    log.info(f"  [1/6] organize 완료 ({(t1-t0).total_seconds():.1f}s, "
             f"topics={len(organized.get('topics', []))}, "
             f"speakers={len(organized.get('speakers', []))})")

    # Stage 2/3/4: parallel
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_decisions = pool.submit(_stage_extract_decisions,
                                   title=title, attendees=attendees,
                                   organized=organized)
        f_actions = pool.submit(_stage_extract_actions,
                                 title=title, attendees=attendees,
                                 attendees_structured=structured,
                                 meeting_type=meeting_type,
                                 organized=organized)
        f_discussion = pool.submit(_stage_write_discussion,
                                    title=title, attendees=attendees,
                                    organized=organized)
        decisions = f_decisions.result()
        action_items = f_actions.result()
        discussion_body = f_discussion.result()

    t2 = datetime.now()
    log.info(f"  [2-4/6] extract 병렬 완료 ({(t2-t1).total_seconds():.1f}s, "
             f"decisions={len(decisions.get('decisions', []))}, "
             f"actions={len(action_items.get('action_items', []))})")

    # Stage 5: assemble
    body_md = _stage_assemble(
        title=title, date=date, attendees=attendees,
        attendees_structured=structured, meeting_type=meeting_type,
        organized=organized, decisions=decisions, action_items=action_items,
        discussion_body=discussion_body,
    )
    t3 = datetime.now()
    log.info(f"  [5/6] assemble 완료 ({(t3-t2).total_seconds():.1f}s, "
             f"{len(body_md):,}자)")

    # Stage 6: validate
    body_md = _stage_validate(
        title=title, meeting_type=meeting_type,
        attendees_structured=structured,
        transcript_text=transcript_text, notes_text=notes_text,
        assembled_minutes=body_md,
    )
    t4 = datetime.now()
    log.info(f"  [6/6] validate 완료 ({(t4-t3).total_seconds():.1f}s)")

    # 위키링크 자동 적용 (알려진 엔티티 한정)
    body_md = apply_wiki_links(body_md, known_entities)

    # frontmatter 부착
    fm = build_frontmatter(
        title=title, date=date, meeting_type=meeting_type,
        attendees_structured=structured,
        has_transcript=bool(transcript_text),
        source_basename=source_basename,
    )
    final_md = fm + body_md.lstrip("\n")

    log.info(f"Minutes Orchestrator 완료 — 총 {(t4-t0).total_seconds():.1f}s, "
             f"{len(final_md):,}자")
    return final_md
