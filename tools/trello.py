"""Trello API 래퍼 — 파이프라인(업체) 카드 읽기/쓰기

보드: parametapipeline (Board ID: TRELLO_BOARD_ID 환경변수)
규칙:
  - 업체 1개 = 카드 1개
  - 카드명은 "업체명 - 사업내용" 형식을 권장
  - 액션아이템은 카드 내 "Action Items" 체크리스트 항목으로 추가
  - 카드 이동/삭제/체크리스트 완료 처리 금지
  - 신규 카드 기본 생성 위치: Contact/Meeting 리스트
인증:
  - API Key: .env의 TRELLO_API_KEY (앱 공통)
  - Token: 사용자별로 user_store에 암호화 저장 (OAuth 인증 후)
"""
import logging
import os
import json
import re

from store import user_store

log = logging.getLogger(__name__)

DEFAULT_PIPELINE_BOARD_ID = "RlFAf1Q1"
BOARD_ID = os.getenv("TRELLO_BOARD_ID", DEFAULT_PIPELINE_BOARD_ID)
CONTACT_LIST_NAME = "Contact/Meeting"
CHECKLIST_NAME = "Action Items"
_DEFAULT_COMPANY_ALIASES = {
    "다날": ["다날핀테크", "다날 핀테크"],
}
_DEFAULT_COMPANY_CARD_SHORTLINKS = {
    "NH상호금융": ["RYIPeZxh"],
}

# ── 내부 헬퍼 ────────────────────────────────────────────────

# 사용자별 클라이언트 캐시 (user_id → TrelloClient)
_client_cache: dict[str, object] = {}
# 사용자별 보드 캐시 ((user_id, board_id) → Board)
_board_cache: dict[tuple[str, str], object] = {}


def _is_dry_run() -> bool:
    dry = os.getenv("DRY_RUN_TRELLO", os.getenv("DRY_RUN", "false"))
    return dry.lower() in ("true", "1", "yes")


def _client_for_user(user_id: str):
    """사용자별 TrelloClient 반환. 토큰 미등록이면 None."""
    if user_id in _client_cache:
        return _client_cache[user_id]

    api_key = os.getenv("TRELLO_API_KEY", "")
    if not api_key:
        if _is_dry_run():
            log.info("Trello DRY_RUN: API Key 없이 더미 모드 실행")
            return None
        log.warning("TRELLO_API_KEY 환경변수 미설정")
        return None

    token = user_store.get_trello_token(user_id)
    if not token:
        log.info(f"Trello 토큰 미등록: {user_id}")
        return None

    import requests as _requests
    session = _requests.Session()
    session.verify = False  # 사내 방화벽 SSL 인증서 이슈 대응

    from trello import TrelloClient
    client = TrelloClient(api_key=api_key, token=token, http_service=session)
    _client_cache[user_id] = client
    return client


def _board_for_user(user_id: str, board_id: str | None = None):
    """사용자별 보드 객체 반환."""
    target_board_id = board_id or BOARD_ID
    cache_key = (user_id, target_board_id)
    if cache_key in _board_cache:
        return _board_cache[cache_key]

    client = _client_for_user(user_id)
    if client is None:
        return None

    try:
        board = client.get_board(target_board_id)
        _board_cache[cache_key] = board
        return board
    except Exception as e:
        log.warning(f"Trello 보드 로드 실패 ({target_board_id}): {e}")
        return None


def clear_user_cache(user_id: str) -> None:
    """사용자 캐시 초기화 (토큰 갱신/해제 시 호출)"""
    _client_cache.pop(user_id, None)
    for key in list(_board_cache):
        if key[0] == user_id:
            _board_cache.pop(key, None)


def _normalize_company_name(name: str) -> str:
    """업체명 비교용 정규화. 영문/국문 별칭은 별도 alias로 처리."""
    value = (name or "").strip().lower()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[\(\)\[\]{}]", " ", value)
    value = re.sub(r"\b(inc|corp|corporation|co|ltd|limited|주식회사|㈜)\b\.?", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _normalize_company_variants(name: str) -> set[str]:
    """공백 표기 차이를 흡수한 비교 후보."""
    normalized = _normalize_company_name(name)
    variants = {normalized}
    compact = re.sub(r"\s+", "", normalized)
    if compact:
        variants.add(compact)
    return {v for v in variants if v}


def _card_company_part(card_name: str) -> str:
    """'업체명 - 사업내용' 카드명에서 업체명 부분만 추출."""
    head = re.split(r"\s+[-–—]\s+", card_name or "", maxsplit=1)[0]
    return head.strip()


def _company_aliases(company_name: str) -> set[str]:
    """업체명 별칭 후보.

    확장 방법:
    TRELLO_COMPANY_ALIASES='{"파라메타":["PARAMETA"],"카카오":["Kakao"]}'
    """
    aliases = {company_name}
    normalized_company = _normalize_company_name(company_name)
    normalized_company_variants = _normalize_company_variants(company_name)
    for key, values in _DEFAULT_COMPANY_ALIASES.items():
        names = {key, *values}
        normalized = set().union(*(_normalize_company_variants(v) for v in names if v))
        if normalized_company_variants & normalized:
            aliases.update(v for v in names if v)

    raw = os.getenv("TRELLO_COMPANY_ALIASES", "").strip()
    if raw:
        try:
            mapping = json.loads(raw)
            for key, values in mapping.items():
                names = {key, *values} if isinstance(values, list) else {key, values}
                normalized = set().union(*(_normalize_company_variants(v) for v in names if v))
                if normalized_company_variants & normalized:
                    aliases.update(v for v in names if v)
        except Exception as e:
            log.warning(f"TRELLO_COMPANY_ALIASES 파싱 실패: {e}")
    variants: set[str] = set()
    for alias in aliases:
        variants.update(_normalize_company_variants(alias))
    return variants


def _company_card_shortlinks(company_name: str) -> set[str]:
    """업체명에 고정 매핑된 Trello 카드 shortLink 후보."""
    shortlinks: set[str] = set()
    normalized_company = _normalize_company_name(company_name)

    for key, values in _DEFAULT_COMPANY_CARD_SHORTLINKS.items():
        names = {key}
        if normalized_company in {_normalize_company_name(v) for v in names}:
            shortlinks.update(v for v in values if v)

    raw = os.getenv("TRELLO_COMPANY_CARD_SHORTLINKS", "").strip()
    if raw:
        try:
            mapping = json.loads(raw)
            for key, values in mapping.items():
                if normalized_company != _normalize_company_name(key):
                    continue
                if isinstance(values, list):
                    shortlinks.update(v for v in values if v)
                elif values:
                    shortlinks.add(values)
        except Exception as e:
            log.warning(f"TRELLO_COMPANY_CARD_SHORTLINKS 파싱 실패: {e}")
    return {s.strip() for s in shortlinks if s and s.strip()}


def _card_matches_shortlink(card, shortlinks: set[str]) -> bool:
    if not shortlinks:
        return False
    candidates = [
        getattr(card, "shortLink", ""),
        getattr(card, "short_link", ""),
        getattr(card, "id", ""),
        getattr(card, "url", ""),
    ]
    return any(
        shortlink in str(candidate)
        for shortlink in shortlinks
        for candidate in candidates
        if candidate
    )


def _card_matches_company(card_name: str, company_name: str) -> bool:
    """브리핑용 정확 매칭: 카드 업체명 부분이 업체명/별칭과 같을 때만 True."""
    company_parts = _normalize_company_variants(_card_company_part(card_name))
    return bool(company_parts & _company_aliases(company_name))


def _find_card(user_id: str, company_name: str):
    """보드에서 업체명으로 카드 검색. py-trello Card 객체 반환.

    브리핑 오매칭을 막기 위해 카드명 전체 유사도 대신
    "업체명 - 사업내용"의 업체명 부분만 exact/alias 매칭한다.
    """
    board_ids = [BOARD_ID]
    if DEFAULT_PIPELINE_BOARD_ID not in board_ids:
        board_ids.append(DEFAULT_PIPELINE_BOARD_ID)

    for board_id in board_ids:
        board = _board_for_user(user_id, board_id)
        if board is None:
            continue

        try:
            cards = board.open_cards()
            card_names = [c.name for c in cards]
            card_shortlinks = _company_card_shortlinks(company_name)
            log.info(
                f"Trello 카드 검색: target='{company_name}', board='{board_id}', "
                f"보드 카드={card_names}"
            )
            for card in cards:
                if (
                    _card_matches_shortlink(card, card_shortlinks)
                    or _card_matches_company(card.name, company_name)
                ):
                    return card
        except Exception as e:
            log.warning(f"Trello 카드 검색 오류 ({board_id}): {e}")
    return None


def get_lookup_diagnostic(user_id: str, company_name: str) -> dict:
    """Trello 카드 조회 실패 시 Slack에 보여줄 최소 진단 정보."""
    if _is_dry_run():
        return {"status": "dry_run", "message": "Trello DRY_RUN 모드"}
    if not os.getenv("TRELLO_API_KEY", ""):
        return {"status": "missing_api_key", "message": "Trello API Key 미설정"}
    if not user_store.get_trello_token(user_id):
        return {"status": "missing_token", "message": "Trello 계정 미연동"}

    board_ids = [BOARD_ID]
    if DEFAULT_PIPELINE_BOARD_ID not in board_ids:
        board_ids.append(DEFAULT_PIPELINE_BOARD_ID)

    searched = []
    for board_id in board_ids:
        board = _board_for_user(user_id, board_id)
        if board is None:
            searched.append(f"{board_id}: 접근 실패")
            continue
        try:
            cards = board.open_cards()
            card_names = [getattr(c, "name", "") for c in cards]
            searched.append(f"{board_id}: {len(card_names)}개 카드 조회")
            matches = [
                name for name in card_names
                if _card_matches_company(name, company_name)
            ]
            if matches:
                return {"status": "found", "message": f"카드 발견: {matches[0]}"}
            shortlinks = _company_card_shortlinks(company_name)
            shortlink_matches = [
                getattr(card, "name", "") for card in cards
                if _card_matches_shortlink(card, shortlinks)
            ]
            if shortlink_matches:
                return {"status": "found", "message": f"카드 발견: {shortlink_matches[0]}"}
        except Exception as e:
            searched.append(f"{board_id}: 조회 실패 ({str(e)[:80]})")

    aliases = ", ".join(sorted(_company_aliases(company_name)))
    shortlinks = ", ".join(sorted(_company_card_shortlinks(company_name)))
    shortlink_part = f", card: {shortlinks}" if shortlinks else ""
    detail = "; ".join(searched) if searched else "조회 보드 없음"
    return {
        "status": "not_found",
        "message": f"카드 미발견 (검색명: {company_name}, alias: {aliases}{shortlink_part}; {detail})",
    }


def _card_similarity(target: str, card_name: str) -> float:
    """두 문자열 간 유사도 점수 (0.0~1.0). 부분 포함·접두사·공통 토큰 기반."""
    t = target.strip().lower()
    c = card_name.strip().lower()
    if t == c:
        return 1.0
    # 한쪽이 다른 쪽에 포함
    if t in c or c in t:
        return 0.8
    # 공통 토큰 비율 (Jaccard-like)
    t_tokens = set(t.split())
    c_tokens = set(c.split())
    if t_tokens and c_tokens:
        intersection = t_tokens & c_tokens
        union = t_tokens | c_tokens
        if intersection:
            return 0.5 * len(intersection) / len(union)
    return 0.0


def search_cards(user_id: str, query: str, limit: int = 5) -> list[dict]:
    """업체명 기반으로 유사한 카드 후보 검색.
    Returns: [{"card_id", "card_name", "list_name", "url", "exact_match": bool}, ...]
    유사도 순으로 정렬, limit개까지 반환.
    """
    if _is_dry_run():
        log.info(f"[DRY_RUN] 카드 검색: {query}")
        return []

    board = _board_for_user(user_id)
    if board is None:
        return []

    target = query.strip().lower()
    if not target:
        return []

    # 리스트 이름은 board.list_lists()로 한 번에 조회 후 매핑 (N+1 회피)
    list_name_by_id: dict[str, str] = {}
    try:
        for lst in board.list_lists():
            list_name_by_id[lst.id] = lst.name
    except Exception as e:
        log.warning(f"Trello 리스트 일괄 조회 오류: {e}")

    results = []
    try:
        cards = board.open_cards()
        for card in cards:
            score = _card_similarity(target, card.name)
            if score > 0:
                list_id = getattr(card, "idList", "") or ""
                results.append({
                    "card_id": card.id,
                    "card_name": card.name,
                    "list_name": list_name_by_id.get(list_id, ""),
                    "url": card.url,
                    "exact_match": score >= 1.0,
                    "_score": score,
                })
    except Exception as e:
        log.warning(f"Trello 카드 검색 오류: {e}")
        return []

    results.sort(key=lambda x: x["_score"], reverse=True)
    for r in results:
        del r["_score"]
    return results[:limit]


def list_all_cards(user_id: str) -> list[dict]:
    """보드의 모든 오픈 카드 목록 반환.
    Returns: [{"card_id", "card_name", "list_name", "url"}, ...]
    """
    if _is_dry_run():
        log.info("[DRY_RUN] 전체 카드 목록 조회")
        return []

    board = _board_for_user(user_id)
    if board is None:
        return []

    # 리스트 이름은 board.list_lists()로 한 번에 조회 후 idList → name 매핑
    # (per-card get_list() API 호출 회피)
    list_name_by_id: dict[str, str] = {}
    try:
        for lst in board.list_lists():
            list_name_by_id[lst.id] = lst.name
    except Exception as e:
        log.warning(f"Trello 리스트 일괄 조회 오류: {e}")

    results = []
    try:
        cards = board.open_cards()
        for card in cards:
            list_id = getattr(card, "idList", "") or ""
            results.append({
                "card_id": card.id,
                "card_name": card.name,
                "list_name": list_name_by_id.get(list_id, ""),
                "url": card.url,
            })
    except Exception as e:
        log.warning(f"Trello 전체 카드 조회 오류: {e}")
    return results


def _find_or_create_checklist(card, checklist_name: str = CHECKLIST_NAME):
    """카드에서 체크리스트 찾기. 없으면 새로 생성."""
    for cl in card.checklists:
        if cl.name == checklist_name:
            return cl
    return card.add_checklist(checklist_name)


def _format_checklist_item(item: dict) -> str:
    """액션아이템 → 체크리스트 항목 문자열 변환.
    item: {"assignee": str, "content": str, "due_date": str|None}
    """
    assignee = item.get("assignee", "")
    content = item.get("content", "")
    due = item.get("due_date") or "미정"
    return f"[{assignee}] {content} (기한: {due})"


# ── DRY_RUN 더미 객체 ────────────────────────────────────────

class _DummyChecklist:
    def __init__(self, name: str = CHECKLIST_NAME):
        self.name = name
        self.items = []

    def add_checklist_item(self, name: str, checked: bool = False):
        self.items.append({"name": name, "checked": checked})
        log.info(f"[DRY_RUN] 체크리스트 항목 추가: {name}")


class _DummyCard:
    def __init__(self, name: str):
        self.name = name
        self.id = "dry-run-card-id"
        self.url = f"https://trello.com/c/dry-run/{name}"
        self.checklists = [_DummyChecklist()]
        self.comments = []

    def comment(self, text: str):
        self.comments.append(text)
        log.info(f"[DRY_RUN] 코멘트 추가: {text[:60]}...")

    def add_checklist(self, name: str):
        cl = _DummyChecklist(name)
        self.checklists.append(cl)
        return cl


# ── READ 함수 (Before Agent용) ──────────────────────────────

def find_card_by_name(user_id: str, company_name: str) -> dict | None:
    """업체명으로 카드 검색.
    Returns: {"card_id", "card_name", "list_name", "url"} 또는 None
    """
    if _is_dry_run():
        log.info(f"[DRY_RUN] 카드 검색: {company_name}")
        return None

    card = _find_card(user_id, company_name)
    if card is None:
        return None

    list_name = ""
    try:
        list_name = card.get_list().name
    except Exception:
        pass

    return {
        "card_id": card.id,
        "card_name": card.name,
        "list_name": list_name,
        "url": card.url,
    }


def get_card_context(user_id: str, company_name: str, limit_comments: int = 3) -> dict:
    """업체 카드의 컨텍스트 조회 (Before Agent 브리핑용).
    Returns: {
        "card_name": str,
        "incomplete_items": [str, ...],
        "recent_comments": [{"author": str, "text": str}, ...],
        "url": str,
    }
    카드 없거나 미등록 사용자면 빈 딕셔너리 반환.
    """
    if _is_dry_run():
        log.info(f"[DRY_RUN] 카드 컨텍스트 조회: {company_name}")
        return {}

    card = _find_card(user_id, company_name)
    if card is None:
        log.info(f"Trello 카드 미발견 (브리핑 컨텍스트): '{company_name}' — 보드 카드 목록 확인 필요")
        return {}

    log.info(f"Trello 카드 발견: '{card.name}' (id={card.id})")
    # 미완료 체크리스트 항목
    incomplete = []
    try:
        for cl in card.checklists:
            for item in cl.items:
                if item.get("state") != "complete":
                    incomplete.append(item.get("name", ""))
    except Exception as e:
        log.warning(f"체크리스트 조회 실패: {e}")

    # 최근 코멘트
    comments = []
    try:
        for c in card.comments[:limit_comments]:
            comments.append({
                "author": c.get("memberCreator", {}).get("fullName", ""),
                "text": c.get("data", {}).get("text", ""),
            })
    except Exception as e:
        log.warning(f"코멘트 조회 실패: {e}")

    log.info(f"Trello 컨텍스트: card='{card.name}', incomplete={incomplete}, comments={len(comments)}개")
    description = getattr(card, "description", "") or getattr(card, "desc", "")
    if not isinstance(description, str):
        description = ""

    return {
        "card_name": card.name,
        "description": description,
        "incomplete_items": incomplete,
        "recent_comments": comments,
        "url": card.url,
    }


# ── WRITE 함수 (After Agent용) ──────────────────────────────

def create_card(user_id: str, company_name: str, list_name: str = CONTACT_LIST_NAME,
                description: str = "") -> dict | None:
    """새 카드 생성. 기본 리스트: Contact/Meeting.
    Returns: {"card_id", "card_name", "url"} 또는 None (실패 시)
    """
    if _is_dry_run():
        dummy = _DummyCard(company_name)
        log.info(f"[DRY_RUN] 카드 생성: {company_name} → {list_name}")
        return {"card_id": dummy.id, "card_name": dummy.name, "url": dummy.url}

    board = _board_for_user(user_id)
    if board is None:
        return None

    target_list = None
    try:
        for lst in board.list_lists():
            if lst.name == list_name:
                target_list = lst
                break
    except Exception as e:
        log.warning(f"리스트 조회 실패: {e}")
        return None

    if target_list is None:
        log.warning(f"리스트 '{list_name}' 없음")
        return None

    try:
        card = target_list.add_card(company_name, desc=description)
        log.info(f"Trello 카드 생성: {company_name} → {list_name}")
        return {"card_id": card.id, "card_name": card.name, "url": card.url}
    except Exception as e:
        log.warning(f"카드 생성 실패: {e}")
        return None


def add_checklist_items(user_id: str, company_name: str, items: list[dict],
                        create_if_missing: bool = True) -> int:
    """업체 카드의 'Action Items' 체크리스트에 항목 추가.
    items: [{"assignee": str, "content": str, "due_date": str|None}, ...]
    create_if_missing: True이면 카드 없을 때 Contact/Meeting에 자동 생성
    Returns: 추가된 항목 수
    """
    if not items:
        return 0

    if _is_dry_run():
        for item in items:
            desc = _format_checklist_item(item)
            log.info(f"[DRY_RUN] 체크리스트 항목: {desc}")
        return len(items)

    card = _find_card(user_id, company_name)
    if card is None and create_if_missing:
        result = create_card(user_id, company_name)
        if result is None:
            log.warning(f"카드 생성 실패로 체크리스트 추가 불가: {company_name}")
            return 0
        card = _find_card(user_id, company_name)

    if card is None:
        log.warning(f"Trello 카드 없음: {company_name}")
        return 0

    try:
        checklist = _find_or_create_checklist(card, CHECKLIST_NAME)
        count = 0
        for item in items:
            desc = _format_checklist_item(item)
            checklist.add_checklist_item(desc)
            count += 1
        log.info(f"Trello 체크리스트 항목 {count}개 추가: {company_name}")
        return count
    except Exception as e:
        log.warning(f"체크리스트 항목 추가 실패: {e}")
        return 0


def _find_card_by_id(user_id: str, card_id: str):
    """카드 ID로 직접 조회. py-trello Card 객체 반환."""
    client = _client_for_user(user_id)
    if client is None:
        return None
    try:
        return client.get_card(card_id)
    except Exception as e:
        log.warning(f"Trello 카드 ID 조회 실패 ({card_id}): {e}")
        return None


def add_checklist_items_by_id(user_id: str, card_id: str,
                                items: list[dict]) -> tuple[int, str]:
    """카드 ID로 직접 체크리스트 항목 추가.

    items: [{"assignee": str, "content": str, "due_date": str|None}, ...]
    Returns: (추가된 항목 수, 에러 메시지). 성공 시 에러 메시지는 빈 문자열.
    """
    if not items:
        return 0, ""

    if _is_dry_run():
        for item in items:
            desc = _format_checklist_item(item)
            log.info(f"[DRY_RUN] 체크리스트 항목 (by id): {desc}")
        return len(items), ""

    card = _find_card_by_id(user_id, card_id)
    if card is None:
        log.warning(f"Trello 카드 없음 (id={card_id})")
        return 0, f"카드를 찾을 수 없어요 (card_id={card_id[:8]}...)"

    try:
        # 주의: client.get_card()가 이미 카드 JSON을 fetch해서 Card 객체를 만들어주므로
        # 추가 card.fetch() 호출은 불필요. 일부 경로에서 redundant fetch가 InvalidIDError
        # 등을 일으킬 수 있어 제거.
        checklist = _find_or_create_checklist(card, CHECKLIST_NAME)
        count = 0
        for item in items:
            desc = _format_checklist_item(item)
            checklist.add_checklist_item(desc)
            count += 1
        log.info(f"Trello 체크리스트 항목 {count}개 추가 (card_id={card_id})")
        return count, ""
    except Exception as e:
        log.exception(f"체크리스트 항목 추가 실패 (card_id={card_id}): {e}")
        return 0, f"{type(e).__name__}: {e}"


def add_comment_by_id(user_id: str, card_id: str, comment: str) -> bool:
    """카드 ID로 직접 코멘트 추가. Returns: 성공 여부"""
    if _is_dry_run():
        log.info(f"[DRY_RUN] 코멘트 추가 (card_id={card_id}): {comment[:60]}...")
        return True

    card = _find_card_by_id(user_id, card_id)
    if card is None:
        log.warning(f"코멘트 추가 실패 — 카드 없음 (card_id={card_id})")
        return False

    try:
        card.comment(comment)
        log.info(f"Trello 코멘트 추가 (card_id={card_id})")
        return True
    except Exception as e:
        log.warning(f"코멘트 추가 실패 (card_id={card_id}): {e}")
        return False


def add_comment(user_id: str, company_name: str, comment: str) -> bool:
    """업체 카드에 코멘트 추가. Returns: 성공 여부"""
    if _is_dry_run():
        log.info(f"[DRY_RUN] 코멘트 추가 ({company_name}): {comment[:60]}...")
        return True

    card = _find_card(user_id, company_name)
    if card is None:
        log.warning(f"코멘트 추가 실패 — 카드 없음: {company_name}")
        return False

    try:
        card.comment(comment)
        log.info(f"Trello 코멘트 추가: {company_name}")
        return True
    except Exception as e:
        log.warning(f"코멘트 추가 실패: {e}")
        return False
