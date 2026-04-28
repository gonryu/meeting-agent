"""Minutes Normalizer — 저장된 회의록 양식 보정

사용자가 Drive 또는 Obsidian 에서 회의록을 직접 편집하면 frontmatter,
[[]] 위키링크, `## 출처 로그`/`## 관련 문서` 자동 영역이 깨질 수 있다.
이 모듈은 두 가지 보정 모드를 제공한다.

- **L1 라이트 보정** (`normalize_light`): LLM 호출 없이 frontmatter / 위키링크 /
  자동 섹션을 보강. 사용자 본문은 절대 손대지 않고 누락된 구조만 추가.
- **L2 풀 보정** (`normalize_full_llm`): Sonnet 한 번 호출. 표준 7/10섹션 양식으로
  재구조화하되 사용자 콘텐츠는 보존. 프롬프트에 명시적 보존 규칙 포함.

진단(`diagnose_minutes`)은 LLM 없이 동작하며, `/회의록` 목록 UI 에서 ⚠️ 마커 노출에 사용된다.
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import anthropic

from store import user_store
from tools import drive
from tools.wiki_linker import load_known_entities, wrap_entities
from agents.minutes_orchestrator import (
    classify_attendees,
    build_frontmatter,
)

log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

_TEMPLATES_DIR = Path(__file__).parent.parent / "prompts" / "templates" / "minutes_normalize"
_CANON_TEMPLATES_DIR = Path(__file__).parent.parent / "prompts" / "templates" / "minutes"

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_SONNET = "claude-sonnet-4-5"

# ── 진단 / 정상화에 사용하는 표준 섹션 정의 ────────────────────────

# 표준 회의록 본문에서 우리가 자동 영역으로 관리하는 섹션
# (없으면 L1 라이트 보정 시 자동 추가)
_AUTO_SECTIONS = ("출처 로그", "관련 문서")

# 회의 유형별 필수 섹션 (양식 깨짐 진단용)
_REQUIRED_SECTIONS_INTERNAL = (
    "회의 개요",
    "결론",
    "액션아이템",
    "주요 논의 내용",
)
_REQUIRED_SECTIONS_EXTERNAL = (
    "회의 개요",
    "회의 목적",
    "주요 논의",
    "결정 사항",
    "액션아이템",
)

# 진단 임계치
_BROKEN_SEVERITY_THRESHOLD = 2  # 누락 항목 2개 이상이면 broken


# ── 보정 대상 제외 영역 매처 ───────────────────────────────────


_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_BROKEN_LINK_RE = re.compile(r"\[\[([^\[\]\n]*)\]\]")


# ── 진단 ───────────────────────────────────────────────────────


def _has_frontmatter(content: str) -> bool:
    """파일 최상단에 YAML frontmatter (`---` ... `---`) 가 있는지 빠르게 확인."""
    if not content:
        return False
    head = content.lstrip("﻿")
    return bool(_FRONTMATTER_RE.match(head))


def _section_headers(body: str) -> set[str]:
    """본문에 등장하는 ## 섹션 헤더 이름 집합."""
    out: set[str] = set()
    for m in re.finditer(r"^##\s+(.+?)\s*$", body or "", re.MULTILINE):
        title = m.group(1).strip()
        # "1. 회의 개요" → "회의 개요" 정규화 (외부용 템플릿 대비)
        title = re.sub(r"^\d+\.\s*", "", title)
        out.add(title)
    return out


def _detect_broken_wiki_links(body: str) -> list[str]:
    """`[[]]` 위키링크 중 비정상적인(빈 이름 / 줄바꿈 포함) 링크를 찾아 반환."""
    broken: list[str] = []
    for m in _BROKEN_LINK_RE.finditer(body or ""):
        inner = (m.group(1) or "").strip()
        if not inner:
            broken.append(m.group(0))
            continue
        # alias 분리: [[name|alias]]
        name = inner.split("|", 1)[0].strip()
        if not name:
            broken.append(m.group(0))
    return broken


def diagnose_minutes(content: str, expected_meeting_type: str = "internal") -> dict:
    """회의록 양식 진단.

    Returns:
        {
          "has_frontmatter": bool,
          "missing_required_sections": [...],
          "missing_auto_sections": [...],
          "broken_links": [...],
          "needs_normalization": bool,
          "severity": "ok" | "warning" | "broken",
        }
    """
    if not content:
        return {
            "has_frontmatter": False,
            "missing_required_sections": list(_REQUIRED_SECTIONS_INTERNAL),
            "missing_auto_sections": list(_AUTO_SECTIONS),
            "broken_links": [],
            "needs_normalization": True,
            "severity": "broken",
        }

    has_fm = _has_frontmatter(content)
    headers = _section_headers(content)

    if expected_meeting_type in ("vendor", "external", "mixed"):
        required = _REQUIRED_SECTIONS_EXTERNAL
    else:
        required = _REQUIRED_SECTIONS_INTERNAL

    missing_required = [s for s in required if s not in headers]
    missing_auto = [s for s in _AUTO_SECTIONS if s not in headers]
    broken_links = _detect_broken_wiki_links(content)

    needs = (not has_fm) or bool(missing_required) or bool(missing_auto) or bool(broken_links)

    # severity 산정
    score = 0
    if not has_fm:
        score += 2
    score += len(missing_required)
    score += min(len(missing_auto), 1)  # auto 섹션은 누적 1점만
    if broken_links:
        score += 1

    if score == 0:
        severity = "ok"
    elif score >= _BROKEN_SEVERITY_THRESHOLD:
        severity = "broken"
    else:
        severity = "warning"

    return {
        "has_frontmatter": has_fm,
        "missing_required_sections": missing_required,
        "missing_auto_sections": missing_auto,
        "broken_links": broken_links,
        "needs_normalization": needs,
        "severity": severity,
    }


def diagnose_minutes_light(content_head: str) -> dict:
    """리스트 UI 용 *경량* 진단.

    파일 본문 전체를 읽지 않고 처음 30줄 정도만 검사하여 frontmatter 유무만 빠르게 확인.
    상세 진단은 사용자가 보정 버튼을 클릭한 뒤 별도 호출.
    """
    head = (content_head or "").lstrip("﻿")
    has_fm = _has_frontmatter(head)
    return {
        "has_frontmatter": has_fm,
        "needs_normalization": not has_fm,
        "severity": "ok" if has_fm else "warning",
    }


# ── 파일명 → 메타데이터 추정 ───────────────────────────────────


def parse_filename_metadata(filename: str) -> dict:
    """`{YYYY-MM-DD}_{title}_{내부용|외부용}.md` 패턴에서 날짜·제목·유형 추출.

    매칭 실패 시 가능한 만큼만 추출하고 나머지는 빈 문자열.
    """
    name = filename or ""
    # 확장자 제거
    if name.endswith(".md"):
        name = name[:-3]

    date = ""
    title = name
    meeting_type = "internal"

    m = re.match(r"^(\d{4}-\d{2}-\d{2})_(.+)$", name)
    if m:
        date = m.group(1)
        rest = m.group(2)
    else:
        rest = name

    if rest.endswith("_내부용"):
        title = rest[: -len("_내부용")]
        meeting_type = "internal"
    elif rest.endswith("_외부용"):
        title = rest[: -len("_외부용")]
        meeting_type = "vendor"
    else:
        title = rest

    return {"date": date, "title": title, "meeting_type": meeting_type}


# ── L1 라이트 보정 ─────────────────────────────────────────────


def _ensure_auto_section(body: str, header: str) -> str:
    """본문 끝에 `## header` 섹션이 없으면 빈 자동 영역과 함께 추가. 있으면 그대로."""
    pat = re.compile(rf"^##\s+{re.escape(header)}\s*$", re.MULTILINE)
    if pat.search(body or ""):
        return body
    suffix = (
        f"\n\n## {header}\n"
        f"{drive.AUTO_START}\n"
        f"<!-- 자동 갱신 영역. 사용자 편집은 마커 밖에 작성하세요. -->\n"
        f"{drive.AUTO_END}\n"
    )
    return (body or "").rstrip() + suffix


def normalize_light(
    content: str,
    meeting_metadata: dict,
    known_entities: list[str] | None = None,
) -> str:
    """LLM 없이 양식 보정.

    - frontmatter 누락 시 합성
    - 알려진 엔티티 [[]] 자동 wrap
    - `## 출처 로그`, `## 관련 문서` 섹션 자동 추가 (마커 영역 포함)
    - 사용자 본문은 절대 수정하지 않음 (오직 ADD)

    meeting_metadata: {
        "title": str,
        "date": str (YYYY-MM-DD),
        "meeting_type": "internal" | "vendor" | "external" | "mixed",
        "attendees_raw": [{ "email": ..., "name"|"displayName": ...}, ...] (선택),
        "source_basename": str | None,
    }
    """
    text = content or ""
    text = text.lstrip("﻿")

    fm_present = _has_frontmatter(text)
    if fm_present:
        new_text = text
    else:
        # 본문 시작 위치
        body = text
        attendees_raw = meeting_metadata.get("attendees_raw") or []
        structured = classify_attendees(attendees_raw)
        fm_block = build_frontmatter(
            title=meeting_metadata.get("title") or "",
            date=meeting_metadata.get("date") or datetime.now(KST).strftime("%Y-%m-%d"),
            meeting_type=meeting_metadata.get("meeting_type") or "internal",
            attendees_structured=structured,
            has_transcript=False,
            source_basename=meeting_metadata.get("source_basename"),
        )
        # 본문이 비어 있으면 H1 헤더 한 줄 추가
        if not body.strip():
            title = meeting_metadata.get("title") or "회의록"
            body = f"# {title}\n"
        new_text = fm_block + body.lstrip("\n")

    # 자동 섹션 추가 — frontmatter 와 본문 분리 후 본문 측에만 작업
    fm_dict, body_only = drive.parse_frontmatter(new_text)
    for sec in _AUTO_SECTIONS:
        body_only = _ensure_auto_section(body_only, sec)

    # 위키링크 자동 wrap
    if known_entities:
        try:
            body_only = wrap_entities(body_only, known_entities)
        except Exception as e:
            log.warning(f"위키링크 wrap 실패 (무시): {e}")

    if fm_dict:
        out = drive.render_frontmatter(fm_dict) + body_only.lstrip("\n")
    else:
        out = body_only

    return out


# ── L2 풀 보정 (LLM) ───────────────────────────────────────────


def _load_canonical_template(meeting_type: str) -> str:
    """assembler 템플릿을 풀 보정 프롬프트의 참고 자료로 로드."""
    name = (
        "assembler_external"
        if meeting_type in ("vendor", "external", "mixed")
        else "assembler_internal"
    )
    try:
        return (_CANON_TEMPLATES_DIR / f"{name}.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _load_normalize_template() -> str:
    return (_TEMPLATES_DIR / "full_normalize.md").read_text(encoding="utf-8")


def _render(template: str, **vars) -> str:
    out = template
    for k, v in vars.items():
        out = out.replace("{{" + k + "}}", str(v) if v is not None else "")
    return out


def normalize_full_llm(
    content: str,
    meeting_metadata: dict,
    known_entities: list[str] | None = None,
) -> str:
    """Sonnet 1회 호출로 전체 양식 보정.

    프롬프트에 사용자 콘텐츠 보존 규칙이 강하게 명시되어 있으나,
    검증 차원에서 호출 후 결과 길이가 원본의 50% 미만이면 원본을 반환한다.
    """
    if not content:
        return content

    template = _load_normalize_template()
    meeting_type = (meeting_metadata.get("meeting_type") or "internal").strip()
    canonical = _load_canonical_template(meeting_type)

    attendees_raw = meeting_metadata.get("attendees_raw") or []
    structured = classify_attendees(attendees_raw)
    our = ", ".join(a.get("name", "") for a in structured.get("our_side", []) if a.get("name"))
    their = ", ".join(a.get("name", "") for a in structured.get("their_side", []) if a.get("name"))

    known_text = "\n".join(known_entities or []) if known_entities else "(없음)"

    prompt = _render(
        template,
        meeting_type=meeting_type,
        title=meeting_metadata.get("title") or "",
        date=meeting_metadata.get("date") or "",
        our_side=our or "(없음)",
        their_side=their or "(없음)",
        known_entities=known_text,
        canonical_template=canonical or "(템플릿 없음)",
        existing_minutes=content,
    )

    log.info(f"L2 풀 보정 요청: {meeting_metadata.get('title')} (meeting_type={meeting_type})")
    msg = _claude.messages.create(
        model=_SONNET,
        max_tokens=8192,
        system=(
            "당신은 회의록 양식 보정 전문가입니다. "
            "사용자 편집 내용을 절대 삭제하지 않고 양식만 재구조화합니다."
        ),
        messages=[{"role": "user", "content": prompt}],
    )
    out = msg.content[0].text.strip()
    # 코드펜스 래핑 방지
    if out.startswith("```"):
        out = re.sub(r"^```(?:markdown|md)?\s*", "", out)
        out = re.sub(r"\s*```$", "", out).strip()

    # 결과가 비정상적으로 짧으면 원본 유지
    if len(out) < max(200, int(len(content) * 0.5)):
        log.warning("L2 보정 결과가 비정상적으로 짧음 — 원본 반환")
        return content

    return out


# ── Drive 헬퍼 ─────────────────────────────────────────────────


def _get_minutes_folder_id(user_id: str) -> tuple[object | None, str | None, str | None]:
    """사용자의 (creds, minutes_folder_id, contacts_folder_id) 반환."""
    try:
        creds = user_store.get_credentials(user_id)
    except Exception as e:
        log.warning(f"_get_minutes_folder_id: 자격증명 실패 ({user_id}): {e}")
        return None, None, None
    user = user_store.get_user(user_id) or {}
    return creds, user.get("minutes_folder_id"), user.get("contacts_folder_id")


def _read_minutes_file(creds, file_id: str) -> str:
    """Drive 회의록 파일 본문을 텍스트로 읽기."""
    return drive._read_file(creds, file_id)


# ── 사용자 정상화 세션 상태 ────────────────────────────────────

# 사용자가 보정 버튼을 클릭했을 때 임시 보관하는 메타.
# { user_id: {
#       file_id, filename, original_content, light_normalized,
#       meeting_type, title, date, channel, thread_ts,
#   } }
_pending_normalize: dict[str, dict] = {}


def _store_pending(user_id: str, payload: dict) -> None:
    _pending_normalize[user_id] = payload


def _pop_pending(user_id: str) -> dict | None:
    return _pending_normalize.pop(user_id, None)


def get_pending(user_id: str) -> dict | None:
    return _pending_normalize.get(user_id)


# ── 리스팅 ─────────────────────────────────────────────────────


def list_minutes_for_normalize(
    slack_client,
    user_id: str,
    *,
    days: int = 180,
    keyword: str | None = None,
    channel: str | None = None,
    thread_ts: str | None = None,
    limit: int = 25,
) -> None:
    """`/회의록정리` — 보정 대상 회의록 목록을 Slack 으로 발송.

    각 항목에 ⚠️ 양식 깨짐 마커 + `[🔧 양식 보정]` 버튼을 함께 표시.
    """
    creds, minutes_folder_id, _ = _get_minutes_folder_id(user_id)
    if not creds or not minutes_folder_id:
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text="⚠️ Minutes 폴더가 설정되지 않았습니다. `/재등록` 으로 재인증해주세요.",
        )
        return

    try:
        files = drive.list_minutes(creds, minutes_folder_id)
    except Exception as e:
        log.exception("회의록 목록 조회 실패")
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text=f"⚠️ 회의록 조회 실패: {e}",
        )
        return

    kw = (keyword or "").strip().lower()
    filtered: list[dict] = []
    for f in files:
        name = f.get("name", "")
        if kw and kw not in name.lower():
            continue
        filtered.append(f)

    if not filtered:
        hint = f" (키워드: {keyword})" if keyword else ""
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text=f"📭 보정 대상 회의록을 찾지 못했어요.{hint}",
        )
        return

    capped = filtered[:limit]
    capped_note = f"\n_({len(filtered)}건 중 {limit}건만 진단)_" if len(filtered) > limit else ""

    # 진단은 상위 limit 건만 (성능 — 다량 파일 환경 보호)
    diagnostics: list[tuple[dict, dict]] = []  # [(file, diag)]
    broken_count = 0
    for f in capped:
        try:
            head = _read_minutes_file(creds, f.get("id"))
        except Exception as e:
            log.warning(f"진단용 파일 읽기 실패 ({f.get('name')}): {e}")
            head = ""
        meta = parse_filename_metadata(f.get("name", ""))
        diag = diagnose_minutes(head, meta["meeting_type"])
        if diag.get("needs_normalization"):
            broken_count += 1
        diagnostics.append((f, diag))

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🧹 *회의록 양식 보정* — 진단 {len(capped)}건 중 ⚠️ 보정 필요 {broken_count}건"
                    f"{capped_note}"
                ),
            },
        },
        {"type": "divider"},
    ]

    for f, diag in diagnostics:
        name = f.get("name", "").replace(".md", "")
        modified = (f.get("modifiedTime") or "")[:10]
        file_id = f.get("id", "")
        sev = diag.get("severity", "ok")
        if sev == "broken":
            mark = "⚠️ *양식 깨짐*"
        elif sev == "warning":
            mark = "⚠️ 일부 보정 필요"
        else:
            mark = "✅ 양식 OK"

        block: dict = {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{name}*  _{modified}_\n{mark}",
            },
        }
        if diag.get("needs_normalization") and file_id:
            block["accessory"] = {
                "type": "button",
                "text": {"type": "plain_text", "text": "🔧 양식 보정"},
                "action_id": "summon_minutes_for_normalize",
                "value": file_id,
            }
        blocks.append(block)

    slack_client.chat_postMessage(
        channel=channel or user_id, thread_ts=thread_ts,
        text="회의록 양식 보정 목록", blocks=blocks,
    )


# ── 진단 + 미리보기 (개별 파일 소환) ───────────────────────────


def _build_diagnosis_summary(diag: dict) -> str:
    """진단 결과를 사람이 읽기 쉽게 요약."""
    lines: list[str] = []
    if not diag.get("has_frontmatter"):
        lines.append("• YAML frontmatter 누락 → 새로 합성")
    miss = diag.get("missing_required_sections") or []
    if miss:
        lines.append(f"• 누락 필수 섹션: {', '.join(miss)}")
    auto = diag.get("missing_auto_sections") or []
    if auto:
        lines.append(f"• 자동 영역 누락: {', '.join(auto)}  (라이트 보정 시 자동 추가)")
    bl = diag.get("broken_links") or []
    if bl:
        lines.append(f"• 비정상 위키링크: {len(bl)}개")
    if not lines:
        lines.append("• 큰 문제 없음 — 위키링크 자동 wrap 만 적용")
    return "\n".join(lines)


def _make_diff_preview(before: str, after: str, max_lines: int = 40) -> str:
    """간단한 라인 단위 diff 코드블록 생성. before/after 의 변경 줄만 발췌."""
    import difflib

    a_lines = (before or "").splitlines()
    b_lines = (after or "").splitlines()
    diff_iter = difflib.unified_diff(
        a_lines, b_lines, fromfile="before", tofile="after", lineterm="",
    )
    out: list[str] = []
    count = 0
    for line in diff_iter:
        out.append(line)
        count += 1
        if count >= max_lines:
            out.append("... (이하 생략)")
            break
    if not out:
        return "(변경 없음)"
    return "```diff\n" + "\n".join(out) + "\n```"


def summon_minutes_for_normalize(
    slack_client,
    user_id: str,
    file_id: str,
    *,
    channel: str | None = None,
    thread_ts: str | None = None,
) -> None:
    """선택된 회의록을 진단하고 미리보기 메시지 발송.

    L1 라이트 결과의 diff 를 미리 계산해 보여주고, 사용자가 버튼으로 선택하도록 함.
    """
    creds, _, contacts_folder_id = _get_minutes_folder_id(user_id)
    if not creds:
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text="⚠️ Google 인증이 필요합니다. `/등록` 으로 먼저 인증해주세요.",
        )
        return

    # 파일 메타 + 본문
    try:
        svc = drive._service(creds)
        meta_resp = svc.files().get(fileId=file_id, fields="id,name").execute()
    except Exception as e:
        log.exception("normalize 대상 파일 메타 조회 실패")
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text=f"⚠️ 파일 메타 조회 실패: {e}",
        )
        return

    filename = meta_resp.get("name") or ""
    try:
        original = _read_minutes_file(creds, file_id)
    except Exception as e:
        log.exception("normalize 대상 본문 읽기 실패")
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text=f"⚠️ 회의록 읽기 실패: {e}",
        )
        return

    meta = parse_filename_metadata(filename)
    diag = diagnose_minutes(original, meta["meeting_type"])

    # 알려진 엔티티 미리 로드 (라이트 보정에서 사용)
    known: list[str] = []
    if contacts_folder_id:
        try:
            known = load_known_entities(creds, contacts_folder_id)
        except Exception as e:
            log.warning(f"알려진 엔티티 로드 실패 (무시): {e}")
            known = []

    light_md = normalize_light(
        original,
        {
            "title": meta["title"],
            "date": meta["date"],
            "meeting_type": meta["meeting_type"],
            "attendees_raw": [],
        },
        known_entities=known,
    )

    # 사용자 세션 보관
    _store_pending(user_id, {
        "file_id": file_id,
        "filename": filename,
        "original_content": original,
        "light_normalized": light_md,
        "meeting_type": meta["meeting_type"],
        "title": meta["title"],
        "date": meta["date"],
        "channel": channel,
        "thread_ts": thread_ts,
        "known_entities": known,
        "contacts_folder_id": contacts_folder_id,
    })

    summary = _build_diagnosis_summary(diag)
    diff_block = _make_diff_preview(original, light_md)

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🧹 *회의록 보정 미리보기*\n"
                    f"*파일*: `{filename}`\n"
                    f"*심각도*: `{diag.get('severity')}`\n\n"
                    f"*진단 결과*\n{summary}"
                ),
            },
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*L1 라이트 보정 변경 미리보기*\n{diff_block}"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ L1 라이트로 보정"},
                    "style": "primary",
                    "action_id": "normalize_apply_light",
                    "value": file_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🛠 L2 풀 보정 (LLM)"},
                    "action_id": "normalize_apply_full",
                    "value": file_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ 취소"},
                    "style": "danger",
                    "action_id": "normalize_cancel",
                    "value": file_id,
                },
            ],
        },
    ]

    slack_client.chat_postMessage(
        channel=channel or user_id, thread_ts=thread_ts,
        text="회의록 양식 보정 미리보기", blocks=blocks,
    )


# ── 보정 적용 ──────────────────────────────────────────────────


def _save_minutes_overwrite(creds, file_id: str, content: str) -> None:
    """기존 file_id 를 그대로 유지하면서 본문만 덮어쓰기."""
    from googleapiclient.http import MediaInMemoryUpload

    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
    drive._service(creds).files().update(fileId=file_id, media_body=media).execute()


def apply_light(
    slack_client,
    user_id: str,
    *,
    channel: str | None = None,
    thread_ts: str | None = None,
) -> None:
    """L1 라이트 보정 결과를 Drive 에 저장."""
    payload = _pop_pending(user_id)
    if not payload:
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text="⚠️ 보정 세션이 만료되었습니다. `/회의록정리` 로 다시 시작해주세요.",
        )
        return

    creds, _, _ = _get_minutes_folder_id(user_id)
    if not creds:
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text="⚠️ Google 인증이 필요합니다.",
        )
        return

    try:
        _save_minutes_overwrite(creds, payload["file_id"], payload["light_normalized"])
    except Exception as e:
        log.exception("L1 라이트 보정 저장 실패")
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text=f"⚠️ 저장 실패: {e}",
        )
        return

    link = f"https://drive.google.com/file/d/{payload['file_id']}/view"
    slack_client.chat_postMessage(
        channel=channel or user_id, thread_ts=thread_ts,
        text=f"✅ L1 라이트 보정 완료: <{link}|{payload['filename']}>",
    )


def apply_full(
    slack_client,
    user_id: str,
    *,
    channel: str | None = None,
    thread_ts: str | None = None,
) -> None:
    """L2 풀 보정 — LLM 호출 후 두 번째 미리보기 + 승인 버튼 표시."""
    payload = get_pending(user_id)
    if not payload:
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text="⚠️ 보정 세션이 만료되었습니다. `/회의록정리` 로 다시 시작해주세요.",
        )
        return

    slack_client.chat_postMessage(
        channel=channel or user_id, thread_ts=thread_ts,
        text="🛠 L2 풀 보정 시작 — LLM 응답에 약 10초 정도 걸립니다…",
    )

    try:
        full_md = normalize_full_llm(
            payload["original_content"],
            {
                "title": payload.get("title"),
                "date": payload.get("date"),
                "meeting_type": payload.get("meeting_type"),
                "attendees_raw": [],
            },
            known_entities=payload.get("known_entities") or [],
        )
    except Exception as e:
        log.exception("L2 풀 보정 실패")
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text=f"⚠️ L2 보정 실패: {e}",
        )
        return

    payload["full_normalized"] = full_md
    _store_pending(user_id, payload)

    diff_block = _make_diff_preview(payload["original_content"], full_md, max_lines=80)
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🛠 *L2 풀 보정 결과 미리보기*\n"
                    f"*파일*: `{payload['filename']}`\n\n"
                    f"{diff_block}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ 이대로 저장"},
                    "style": "primary",
                    "action_id": "normalize_confirm_full",
                    "value": payload["file_id"],
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ 취소"},
                    "style": "danger",
                    "action_id": "normalize_cancel",
                    "value": payload["file_id"],
                },
            ],
        },
    ]
    slack_client.chat_postMessage(
        channel=channel or user_id, thread_ts=thread_ts,
        text="L2 풀 보정 미리보기", blocks=blocks,
    )


def confirm_full(
    slack_client,
    user_id: str,
    *,
    channel: str | None = None,
    thread_ts: str | None = None,
) -> None:
    """L2 풀 보정 결과를 최종 저장."""
    payload = _pop_pending(user_id)
    if not payload or not payload.get("full_normalized"):
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text="⚠️ 보정 세션이 만료되었습니다.",
        )
        return

    creds, _, _ = _get_minutes_folder_id(user_id)
    if not creds:
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text="⚠️ Google 인증이 필요합니다.",
        )
        return

    try:
        _save_minutes_overwrite(creds, payload["file_id"], payload["full_normalized"])
    except Exception as e:
        log.exception("L2 풀 보정 저장 실패")
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text=f"⚠️ 저장 실패: {e}",
        )
        return

    link = f"https://drive.google.com/file/d/{payload['file_id']}/view"
    slack_client.chat_postMessage(
        channel=channel or user_id, thread_ts=thread_ts,
        text=f"✅ L2 풀 보정 저장 완료: <{link}|{payload['filename']}>",
    )


def cancel_normalize(
    slack_client,
    user_id: str,
    *,
    channel: str | None = None,
    thread_ts: str | None = None,
) -> None:
    """보정 세션 취소."""
    _pop_pending(user_id)
    slack_client.chat_postMessage(
        channel=channel or user_id, thread_ts=thread_ts,
        text="❌ 보정을 취소했습니다.",
    )
