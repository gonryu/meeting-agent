"""Wiki Linker — 회의록·Wiki 본문에서 알려진 업체/인물 명을 [[]] 위키링크로 감싼다.

핵심 원칙:
- 알려진 엔티티(Drive Contacts/Companies, People)에만 적용
- 코드 블록(``` 또는 ` ` ` 백틱) 내부는 변환하지 않음
- 이미 [[X]] 또는 [[X|...]] 로 감싸진 텍스트는 다시 감싸지 않음 (이중 wrap 방지)
- YAML frontmatter(--- ... ---) 내부는 변환하지 않음
- 검색은 길이 내림차순 (긴 이름이 짧은 이름보다 먼저 매칭되도록 — 예: '김민환'이 '민환'보다 먼저)
- 한국어/영문 모두 지원. 영문 이름은 단어 경계, 한국어 이름은 길이 우선 + placeholder 토큰으로 중복 매칭 차단

호출 패턴:
    from tools.wiki_linker import wrap_entities, load_known_entities

    entities = load_known_entities(creds, contacts_folder_id)
    wrapped = wrap_entities(body, entities)
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

log = logging.getLogger(__name__)


# ── 엔티티 수집 ─────────────────────────────────────────────


def load_known_entities(creds, contacts_folder_id: str) -> list[str]:
    """Drive Contacts/Companies, Contacts/People 폴더에서 알려진 엔티티 이름 목록을 반환.

    실패 시 빈 리스트 (위키링크 미적용 — 안전 폴백).
    """
    if not creds or not contacts_folder_id:
        return []

    try:
        from tools import drive as _drive
    except Exception:
        return []

    names: list[str] = []
    try:
        svc = _drive._service(creds)
        for sub in ("Companies", "People"):
            folder_id = _drive._get_subfolder_id(creds, contacts_folder_id, sub)
            if not folder_id:
                continue
            q = f"'{folder_id}' in parents and trashed=false"
            r = svc.files().list(q=q, fields="files(name)").execute()
            for f in r.get("files", []):
                name = f.get("name", "")
                if name.endswith(".md"):
                    name = name[:-3]
                if name:
                    import unicodedata
                    names.append(unicodedata.normalize("NFC", name))
    except Exception as e:
        log.warning(f"알려진 엔티티 로드 실패 (위키링크 미적용): {e}")
        return []

    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ── 본문 보호 영역 마스킹 ────────────────────────────────────

# YAML frontmatter (파일 최상단 --- ... ---)
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
# 코드 펜스 블록 ```...```
_FENCE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
# 인라인 코드 `...`
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
# 기존 위키링크 [[...]]  (alias 포함 [[X|Y]])
_EXISTING_LINK_RE = re.compile(r"\[\[[^\[\]\n]+\]\]")


def _mask_protected(body: str) -> tuple[str, list[str]]:
    """보호 영역(frontmatter, 코드, 기존 링크)을 placeholder로 치환.

    Returns: (마스킹된 본문, 원본 조각 리스트)
    """
    masks: list[str] = []

    def _store(text: str) -> str:
        masks.append(text)
        return f"\x00WLM{len(masks) - 1}\x00"

    out = body

    # 1) frontmatter (반드시 첫 번째)
    m = _FRONTMATTER_RE.match(out)
    if m:
        repl = _store(m.group(0))
        out = repl + out[m.end():]

    # 2) 코드 펜스 블록
    out = _FENCE_BLOCK_RE.sub(lambda mm: _store(mm.group(0)), out)
    # 3) 인라인 코드
    out = _INLINE_CODE_RE.sub(lambda mm: _store(mm.group(0)), out)
    # 4) 기존 위키링크
    out = _EXISTING_LINK_RE.sub(lambda mm: _store(mm.group(0)), out)

    return out, masks


def _unmask(body: str, masks: list[str]) -> str:
    """placeholder를 원본 조각으로 복원. 중첩되어 있을 수 있으므로 변화가 없을 때까지 반복."""
    while True:
        prev = body
        body = re.sub(
            r"\x00WLM(\d+)\x00",
            lambda m: masks[int(m.group(1))],
            body,
        )
        if body == prev:
            break
    return body


# ── 변환 ────────────────────────────────────────────────────


def _is_ascii(name: str) -> bool:
    try:
        name.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _wrap_in_text(text: str, entities_sorted: list[str]) -> str:
    """비보호 텍스트에서 엔티티 이름을 [[]] 로 감싼다."""
    if not text:
        return text

    # 이미 변환된 영역을 다시 변환하지 않도록 placeholder로 격리
    converted: list[str] = []

    def _store(s: str) -> str:
        converted.append(s)
        return f"\x00WLC{len(converted) - 1}\x00"

    out = text
    for name in entities_sorted:
        if not name or len(name) <= 1:
            # 1자 엔티티는 안전상 제외 (오매칭 위험)
            continue
        pat = re.escape(name)
        if _is_ascii(name):
            # 영문 이름: 앞뒤가 알파벳/숫자가 아니어야 함 (한국어 조사가 붙는 경우 허용).
            # \b는 한국어 다음에 오는 영문 단어를 잘 처리하지 못하므로 명시적 lookaround 사용.
            regex = re.compile(rf"(?<!\[)(?<![A-Za-z0-9_]){pat}(?![A-Za-z0-9_])(?!\]\])")
        else:
            # 한국어: 단어 경계가 모호 → [[ ]] 인접 회피만 적용
            regex = re.compile(rf"(?<!\[){pat}(?!\]\])")

        def _sub(m: re.Match) -> str:
            return _store(f"[[{m.group(0)}]]")

        out = regex.sub(_sub, out)

    # placeholder 복원
    out = re.sub(r"\x00WLC(\d+)\x00", lambda m: converted[int(m.group(1))], out)
    return out


def wrap_entities(body: str, entities: Iterable[str]) -> str:
    """본문 내 알려진 엔티티 이름을 [[]] 위키링크로 감싼다.

    - frontmatter, 코드블록, 인라인 코드, 기존 [[]] 위키링크는 보호.
    - 1자 이름은 안전상 제외.
    - 길이 내림차순으로 매칭하여 부분 매칭 충돌을 줄임.
    """
    if not body:
        return body
    ents = sorted({e for e in entities if e}, key=lambda x: (-len(x), x))
    if not ents:
        return body

    masked, masks = _mask_protected(body)
    wrapped = _wrap_in_text(masked, ents)
    return _unmask(wrapped, masks)
