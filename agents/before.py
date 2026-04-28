"""Before 에이전트 — 미팅 준비 오케스트레이터"""
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

import logging
import anthropic
from dotenv import load_dotenv
from slack_sdk import WebClient

log = logging.getLogger(__name__)

from tools import calendar as cal
from tools import drive, gmail, trello
# Obsidian 헬퍼 — 테스트에서 `before.drive` 가 mock 되어도 동작하도록 별도 alias
from tools.drive import (
    parse_frontmatter as _drive_parse_frontmatter,
    render_frontmatter as _drive_render_frontmatter,
    merge_frontmatter as _drive_merge_frontmatter,
    AUTO_START as _DRIVE_AUTO_START,
    AUTO_END as _DRIVE_AUTO_END,
)
from tools.slack_tools import (
    build_briefing_message,
    build_meeting_header_block,
    build_company_research_block,
    build_persons_block,
    build_context_block,
    ask_company_name,
    ask_email,
    format_time,
)
from prompts.briefing import (
    company_news_prompt,
    person_info_prompt,
    service_connection_prompt,
    parse_meeting_prompt,
    merge_meeting_prompt,
    update_knowledge_prompt,
)
from store import user_store

load_dotenv(override=True)

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_CLAUDE_MODEL = "claude-haiku-4-5"

_PARAMETA_RELEVANCE_KEYWORDS = (
    "스테이블코인", "stablecoin", "블록체인", "blockchain", "rwa",
    "디지털자산", "digital asset", "토큰증권", "sto", "did", "신원인증",
    "전자지갑", "wallet", "web3", "cbdc", "핀테크", "fintech", "결제",
    "payment", "금융", "규제", "라이선스", "보안", "인증", "가상자산",
    "토큰화", "tokenization",
)

# ParaScope 봇 채널 조회
_PARASCOPE_BOT_ID = os.getenv("PARASCOPE_BOT_ID", "")
_PARASCOPE_BOT_APP_ID = os.getenv("PARASCOPE_BOT_APP_ID", "")
_PARASCOPE_CHANNEL_ID = os.getenv("PARASCOPE_CHANNEL_ID", "")
_slack_client_for_parascope = WebClient(token=os.getenv("SLACK_BOT_TOKEN", ""))

# 어젠다 대기 중인 미팅: {thread_ts: [event_id, user_id]}
# 서버 재시작 후에도 유지되도록 파일로 영속화
_PENDING_AGENDA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "pending_agenda.json")


def _load_pending_agenda() -> dict:
    try:
        os.makedirs(os.path.dirname(_PENDING_AGENDA_FILE), exist_ok=True)
        with open(_PENDING_AGENDA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_pending_agenda(agenda: dict):
    try:
        os.makedirs(os.path.dirname(_PENDING_AGENDA_FILE), exist_ok=True)
        with open(_PENDING_AGENDA_FILE, "w", encoding="utf-8") as f:
            json.dump(agenda, f, ensure_ascii=False)
    except Exception as e:
        log.warning(f"pending_agenda 저장 실패: {e}")


# 동시성 보호용 Lock (INF-07)
_agenda_lock = threading.Lock()     # _pending_agenda
_drafts_lock = threading.Lock()     # _meeting_drafts

_pending_agenda: dict[str, list] = _load_pending_agenda()


# ── 업체명 추론 (FR-B13, FR-B14) ─────────────────────────────


def _infer_company_from_title(title: str, company_candidates: list[str] = None) -> str:
    """미팅 제목에서 업체명을 LLM으로 추론.

    Args:
        title: 캘린더 이벤트 제목
        company_candidates: Drive에 저장된 기존 업체명 목록 (참고용)

    Returns:
        추론된 업체명 문자열. 추론 실패 시 빈 문자열.
    """
    candidates_text = ""
    if company_candidates:
        candidates_text = (
            "\n## 기존 업체 목록 (참고용)\n"
            + ", ".join(company_candidates)
        )

    prompt = (
        f"다음 회의 제목에서 업체(회사)명을 추출하세요.{candidates_text}\n\n"
        f"## 규칙\n"
        f"- 제목에 업체명이 명시되어 있으면 그대로 반환\n"
        f"- 인물명만 있으면 NONE 반환 (업체명이 아님)\n"
        f"- 업체명을 알 수 없으면 반드시 NONE만 반환\n"
        f"- 기존 업체 목록에 유사한 이름이 있으면 목록의 정확한 이름 사용\n"
        f"- 약어나 영문명도 기존 목록과 매칭 (예: '카카오' = 'Kakao')\n"
        f"- 업체명 또는 NONE 한 단어만 반환. 설명, 이유, 부연 금지\n\n"
        f"회의 제목: {title}\n업체명:"
    )
    try:
        result = _generate(prompt).strip().strip('"').strip("'")
        # 빈 응답이나 "없음" 등은 빈 문자열로 처리
        if not result or result.upper() == "NONE" or result in ("없음", "없다", "N/A", "null", "-"):
            return ""
        return result
    except Exception as e:
        log.warning(f"업체명 추론 실패: {e}")
        return ""


def _infer_company_from_attendees(
    attendees: list[dict], creds=None, contacts_folder_id: str = None,
) -> str:
    """FR-B16: 참석자 이메일 도메인·인물 파일에서 소속 회사 역추론.

    Returns: 추론된 업체명 문자열. 실패 시 빈 문자열.
    """
    _internal_domains = set(
        os.getenv("INTERNAL_DOMAINS", "parametacorp.com,iconloop.com").split(",")
    )

    external_domains = set()
    external_names = []

    for a in attendees:
        email = a.get("email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        if domain and domain not in _internal_domains:
            external_domains.add(domain)
            name = a.get("displayName") or a.get("name", "")
            if name:
                external_names.append(name)

    # 1단계: 인물 파일에서 소속 조회
    if creds and contacts_folder_id and external_names:
        for name in external_names[:3]:  # 상위 3명만 조회
            try:
                content, _ = drive.get_person_info(creds, contacts_folder_id, name)
                if content:
                    # "소속: ..." 행에서 업체명 추출
                    for line in content.splitlines():
                        if "소속:" in line:
                            import re
                            # [[기업명]] 또는 일반 텍스트
                            match = re.search(r'\[\[(.+?)\]\]', line)
                            if match:
                                return match.group(1)
                            # "소속: 카카오" 형태
                            company = line.split("소속:")[-1].strip().strip("-").strip()
                            if company and company not in ("", "없음"):
                                return company
            except Exception:
                pass

    # 2단계: 이메일 도메인에서 업체명 추론
    # 잘 알려진 도메인 매핑
    domain_hints = {
        "kakao.com": "카카오", "kakaocorp.com": "카카오",
        "samsung.com": "삼성전자", "samsungsds.com": "삼성SDS",
        "lgcns.com": "LGCNS", "lg.com": "LG",
        "sk.com": "SK", "sktelecom.com": "SK텔레콤",
        "naver.com": "네이버", "navercorp.com": "네이버",
        "kisa.or.kr": "KISA", "bok.or.kr": "한국은행",
    }
    for domain in external_domains:
        if domain in domain_hints:
            return domain_hints[domain]

    # 3단계: 도메인에서 업체명 추출 (2차 도메인)
    _free_email = {"gmail", "yahoo", "outlook", "hotmail", "naver"}
    _second_level_tld = {"co", "or", "ac", "go", "ne", "re"}  # .co.kr, .or.kr 등
    for domain in external_domains:
        parts = domain.split(".")
        if len(parts) >= 3 and parts[-2] in _second_level_tld:
            candidate = parts[-3]  # e.g., "shinhan" from "shinhan.co.kr"
        elif len(parts) >= 2:
            candidate = parts[-2]  # e.g., "kakao" from "kakao.com"
        else:
            continue
        if len(candidate) >= 3 and candidate not in _free_email:
            return candidate.capitalize()

    return ""


def _hint(text: str) -> str:
    """공통 디스커버리 힌트 푸터 — 상위 메시지 하단에 한 줄 이탤릭으로 부가."""
    return f"\n_💡 {text}_"


def _post(slack_client, *, user_id: str, channel=None, thread_ts=None,
          text=None, blocks=None, unfurl_links=False) -> dict:
    """channel 기본값을 user_id(DM)로 적용한 chat_postMessage 헬퍼"""
    kwargs = {"channel": channel or user_id, "unfurl_links": unfurl_links}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    if blocks is not None:
        kwargs["blocks"] = blocks
    if text is not None:
        kwargs["text"] = text
    return slack_client.chat_postMessage(**kwargs)


# ── LLM 호출 헬퍼 (Claude 단일) ────────────────────────────

def _search(prompt: str) -> str:
    """웹 검색 포함 LLM 호출 — Claude web_search 도구 사용"""
    resp = _claude.beta.messages.create(
        model=_CLAUDE_MODEL,
        max_tokens=2048,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
        betas=["web-search-2025-03-05"],
    )
    return "\n".join(block.text for block in resp.content if hasattr(block, "text")).strip()


def generate_text(prompt: str) -> str:
    """일반 LLM 호출 (public) — Claude"""
    return _generate(prompt)


def _generate(prompt: str) -> str:
    """일반 LLM 호출 — Claude"""
    resp = _claude.messages.create(
        model=_CLAUDE_MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


# ── 모호 입력 추천 (intent fallback) ──────────────────────────

# 슬래시 명령 카탈로그 (사용자에게 노출 가능한 핵심 명령만)
_COMMAND_CATALOG: list[dict] = [
    {"cmd": "/브리핑", "desc": "오늘/지정 기간 미팅 브리핑"},
    {"cmd": "/미팅시작", "desc": "회의 시작 (메모 세션 시작)"},
    {"cmd": "/메모", "desc": "진행 중 회의 메모 추가"},
    {"cmd": "/미팅종료", "desc": "회의 종료 + 회의록 생성"},
    {"cmd": "/회의록작성", "desc": "현재 세션 회의록 즉시 생성"},
    {"cmd": "/회의록", "desc": "저장된 회의록 목록·검색"},
    {"cmd": "/회의록정리", "desc": "저장된 회의록 양식·구조 보정"},
    {"cmd": "/미팅편집", "desc": "기존 캘린더 미팅 편집·수정·시간 변경"},
    {"cmd": "/회의실예약", "desc": "드림플러스 회의실 예약"},
    {"cmd": "/회의실조회", "desc": "회의실 예약 현황"},
    {"cmd": "/회의실취소", "desc": "회의실 예약 취소"},
    {"cmd": "/크레딧조회", "desc": "드림플러스 잔여 크레딧"},
    {"cmd": "/트렐로조회", "desc": "Trello 카드 조회·검색"},
    {"cmd": "/트렐로주간보고", "desc": "Trello 워크스페이스 주간 보고서"},
    {"cmd": "/도움말", "desc": "전체 사용 가이드"},
]


def _load_command_suggester_template() -> str:
    """프롬프트 템플릿 로드 — 매 호출 시 디스크에서 읽어 핫리로드 지원."""
    path = os.path.join(
        os.path.dirname(__file__), "..", "prompts", "templates",
        "intent", "command_suggester.md",
    )
    with open(path, encoding="utf-8") as f:
        return f.read()


def suggest_commands(user_text: str) -> list[dict]:
    """모호한 사용자 입력에 대해 1~2개 슬래시 명령 후보를 추천.

    반환: [{"command": "/회의록정리", "reason": "회의록 양식 정리 의도로 보입니다"}, ...]
    명백히 명령 의도가 아닌 입력(인사·잡담·짧은 텍스트)은 빈 리스트.
    LLM 호출 실패 시에도 빈 리스트를 반환 (graceful fallback).
    """
    enabled = os.getenv("COMMAND_SUGGESTER_ENABLED", "true").lower() != "false"
    if not enabled:
        return []
    text = (user_text or "").strip()
    # 4자 미만은 명령 후보 추천을 시도하지 않음 (잡음 차단)
    if len(text) < 4:
        return []

    try:
        template = _load_command_suggester_template()
    except Exception as e:
        log.warning(f"command_suggester 템플릿 로드 실패: {e}")
        return []

    commands_list = "\n".join(
        f"- `{item['cmd']}` — {item['desc']}" for item in _COMMAND_CATALOG
    )
    prompt = (template
              .replace("{{user_text}}", text.replace('"', "'"))
              .replace("{{commands_list}}", commands_list))

    try:
        raw = _generate(prompt)
    except Exception as e:
        log.warning(f"command_suggester LLM 호출 실패: {e}")
        return []

    # 코드 펜스 제거 후 JSON 파싱
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.lstrip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip().rstrip("`").strip()
    try:
        data = json.loads(cleaned)
    except Exception as e:
        log.warning(f"command_suggester JSON 파싱 실패: {e} / 원문: {raw[:200]}")
        return []

    candidates = data.get("candidates") or []
    if not isinstance(candidates, list):
        return []

    # 화이트리스트 필터 + 최대 2개 + 중복 제거
    valid_cmds = {item["cmd"] for item in _COMMAND_CATALOG}
    seen: set[str] = set()
    result: list[dict] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        cmd = (c.get("command") or "").strip()
        reason = (c.get("reason") or "").strip()
        if not cmd or cmd in seen:
            continue
        if cmd not in valid_cmds:
            continue
        seen.add(cmd)
        result.append({"command": cmd, "reason": reason or "관련 명령으로 보입니다"})
        if len(result) >= 2:
            break
    return result


# ── 이메일→이름 변환 ─────────────────────────────────────────

# Slack 멤버 캐시 (email → display_name). 프로세스 수명 동안 유지.
_slack_email_name_cache: dict[str, str] = {}
_slack_cache_loaded = False


def _load_slack_email_cache(slack_client):
    """Slack users.list를 한 번 호출하여 email→이름 캐시 구축"""
    global _slack_cache_loaded
    if _slack_cache_loaded:
        return
    try:
        resp = slack_client.users_list()
        for member in resp.get("members", []):
            if member.get("deleted") or member.get("is_bot"):
                continue
            profile = member.get("profile", {})
            email = profile.get("email", "").lower()
            name = (profile.get("display_name") or profile.get("real_name") or "").strip()
            if email and name:
                _slack_email_name_cache[email] = name
        _slack_cache_loaded = True
    except Exception as e:
        log.warning(f"Slack email→name 캐시 로드 실패: {e}")


def _resolve_attendee_names(attendees: list[dict], user_id: str, slack_client) -> list[str]:
    """참석자 목록의 이메일을 이름으로 변환.
    우선순위: Calendar displayName → Slack 프로필 → Google 주소록 → 이메일 표시
    """
    _load_slack_email_cache(slack_client)

    creds = None
    try:
        creds = user_store.get_credentials(user_id)
    except Exception:
        pass

    names: list[str] = []
    for a in attendees:
        email = a.get("email", "")
        display = a.get("name", "").strip()

        # 1순위: 캘린더 displayName
        if display:
            names.append(display)
            continue

        # 2순위: Slack 프로필
        slack_name = _slack_email_name_cache.get(email.lower())
        if slack_name:
            names.append(slack_name)
            continue

        # 3순위: Google 주소록 (이메일로 역검색)
        if creds:
            try:
                contact_name = _find_name_in_contacts(creds, email)
                if contact_name:
                    names.append(contact_name)
                    continue
            except Exception:
                pass

        # 못 찾으면 이메일 그대로
        names.append(email)

    return names


def _find_name_in_contacts(creds, email: str) -> str | None:
    """Google 주소록(People API)에서 이메일로 이름 검색"""
    try:
        from googleapiclient.discovery import build as gapi_build
        svc = gapi_build("people", "v1", credentials=creds)
        result = svc.people().searchContacts(
            query=email,
            readMask="names,emailAddresses",
            pageSize=3,
        ).execute()
        for item in result.get("results", []):
            person = item.get("person", {})
            person_emails = [e.get("value", "").lower()
                             for e in person.get("emailAddresses", [])]
            if email.lower() in person_emails:
                name_list = person.get("names", [])
                if name_list:
                    return name_list[0].get("displayName", "")
    except Exception:
        pass
    return None


# ── 리서치 ──────────────────────────────────────────────────

def _get_creds_and_config(user_id: str):
    """사용자 credentials + drive config 반환"""
    creds = user_store.get_credentials(user_id)
    user = user_store.get_user(user_id)
    return creds, user["contacts_folder_id"], user["knowledge_file_id"]


def _query_parascope(company_name: str, timeout: int = 60) -> str | None:
    """#meeting-agent-testing 채널에 업체명을 전송하고 @ParaScope 응답을 반환한다.
    Slack은 봇 토큰으로 다른 봇에 DM 불가(cannot_dm_bot)이므로 공유 채널을 사용.
    timeout초 이내에 응답이 없으면 None 반환.
    """
    if not _PARASCOPE_BOT_ID or not _PARASCOPE_CHANNEL_ID:
        log.warning("PARASCOPE_BOT_ID 또는 PARASCOPE_CHANNEL_ID 미설정 — ParaScope 조회 건너뜀")
        return None
    try:
        # 채널에 @ParaScope 멘션 + 업체명 전송
        sent = _slack_client_for_parascope.chat_postMessage(
            channel=_PARASCOPE_CHANNEL_ID,
            text=f"<@{_PARASCOPE_BOT_ID}> {company_name}",
        )
        sent_ts = sent["ts"]
        log.info(f"ParaScope 채널 전송: '{company_name}' → {_PARASCOPE_CHANNEL_ID} (ts={sent_ts})")

        # ParaScope 응답 폴링 (최대 timeout초, 3초 간격)
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            history = _slack_client_for_parascope.conversations_history(
                channel=_PARASCOPE_CHANNEL_ID,
                oldest=sent_ts,
                limit=20,
            )
            for msg in history.get("messages", []):
                # 내가 보낸 메시지(sent_ts) 제외, ParaScope 봇 응답만 수집
                if msg.get("ts") == sent_ts:
                    continue
                bot_id = msg.get("bot_id", "")
                user_id = msg.get("user", "")
                if (bot_id in (_PARASCOPE_BOT_ID, _PARASCOPE_BOT_APP_ID)
                        or user_id == _PARASCOPE_BOT_ID):
                    # "생성 중" 안내 메시지는 건너뛰고 실제 내용 대기
                    base_text = msg.get("text", "")
                    if "생성 중" in base_text or "hourglass" in base_text:
                        log.info("ParaScope 생성 중 메시지 수신, 실제 응답 대기...")
                        continue

                    # text + blocks + attachments 전체 내용 수집
                    parts = []
                    if base_text:
                        parts.append(base_text)

                    # blocks에서 텍스트 추출
                    for block in msg.get("blocks", []):
                        bt = block.get("text", {})
                        if isinstance(bt, dict) and bt.get("text"):
                            parts.append(bt["text"])
                        for elem in block.get("elements", []):
                            et = elem.get("text", {})
                            if isinstance(et, dict) and et.get("text"):
                                parts.append(et["text"])
                            elif isinstance(et, str) and et:
                                parts.append(et)

                    # attachments에서 텍스트 추출
                    for att in msg.get("attachments", []):
                        for key in ("text", "pretext", "fallback"):
                            val = att.get(key, "")
                            if val:
                                parts.append(val)

                    response_text = "\n".join(p for p in parts if p).strip()
                    log.info(f"ParaScope 응답 수신 ({len(response_text)}자)")
                    return response_text
        log.warning(f"ParaScope 응답 타임아웃 ({timeout}초): {company_name}")
        return None
    except Exception as e:
        log.warning(f"ParaScope 채널 조회 실패 ({company_name}): {e}")
        return None


_NEWS_PREAMBLE_KEYWORDS = (
    "검색하겠습니다", "검색해 드리겠습니다", "검색해드리겠습니다",
    "알려드리겠습니다", "정리해 드리겠습니다", "정리해드리겠습니다",
    "살펴보겠습니다", "다음과 같습니다", "다음은", "정리합니다",
    "검색 결과", "이상입니다", "없음으로 답변", "검색을 진행하겠습니다",
    "추천합니다", "추천드립니다", "확인하시기 바랍니다", "참고하시기 바랍니다",
    "더 있을 수 있", "추가로 확인", "도움이 되", "위의 정보",
    "위 정보를", "이외에도", "더 자세한 정보", "기타 정보",
)


def _clean_news_text(text: str) -> str:
    """LLM 응답에서 도입/마무리 문구 줄을 제거하고 불릿 항목만 반환"""
    cleaned = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if any(kw in stripped for kw in _NEWS_PREAMBLE_KEYWORDS):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def _to_bullet_lines(text: str) -> str:
    """각 줄을 '- ' 불릿으로 정규화. 빈 줄·LLM 도입/마무리 문구·마크다운 제목 제거."""
    result = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # 마크다운 제목(#, ##, ...) 제거
        if stripped.startswith("#"):
            continue
        if any(kw in stripped for kw in _NEWS_PREAMBLE_KEYWORDS):
            continue
        if stripped.startswith("•"):
            stripped = "- " + stripped[1:].strip()
        elif not stripped.startswith("-"):
            stripped = "- " + stripped
        result.append(stripped)
    return "\n".join(result)


def _filter_parameta_relevant_news(news_text: str) -> str:
    """웹 검색 결과에서 파라메타 사업 맥락과 무관한 단순 최신 기사 제거."""
    kept = []
    for line in news_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if "공개 정보 없음" in stripped or "정보 없음" in stripped:
            kept.append(stripped)
            continue
        if any(keyword.lower() in lowered for keyword in _PARAMETA_RELEVANCE_KEYWORDS):
            kept.append(stripped)
    if kept:
        return "\n".join(kept)
    return "- 파라메타 사업 맥락의 최근 공개 정보 없음"


def _format_email_context_section(emails: list[dict], today: str) -> str:
    """Gmail 검색 결과를 업체 Wiki의 이메일 맥락 섹션으로 정리."""
    if not emails:
        return ""
    lines = []
    for e in emails[:5]:
        subject = (e.get("subject") or "(제목 없음)").strip()
        snippet = (e.get("snippet") or "").replace("\n", " ").replace("\r", " ").strip()
        sender = (e.get("from") or "").strip()
        date = (e.get("date") or "").strip()
        context = snippet[:180] if snippet else "본문 요약 없음"
        sender_part = f" | {sender}" if sender else ""
        lines.append(f"- {date}{sender_part} | {subject} | {context}")
    return f"## 이메일 맥락\n- last_searched: {today}\n" + "\n".join(lines) + "\n"


def _build_trello_summary(trello_context: dict) -> list[str]:
    """Trello 카드 설명/댓글을 브리핑용 짧은 맥락으로 변환."""
    summary = []
    desc = (trello_context.get("description") or "").strip()
    if desc:
        one_line = re.sub(r"\s+", " ", desc)
        summary.append(f"카드: {one_line[:90]}")
    for comment in trello_context.get("recent_comments", [])[:2]:
        text = re.sub(r"\s+", " ", (comment.get("text") or "").strip())
        if not text:
            continue
        author = comment.get("author") or "작성자"
        summary.append(f"{author}: {text[:80]}")
    return summary[:3]


def _build_update_check_lines(existing_content: str | None, today: str,
                              used_orchestrator: bool) -> list[str]:
    if not existing_content:
        return [f"{today} 신규 리서치로 업체 Wiki 생성"]
    last = ""
    for line in existing_content.splitlines():
        if "last_searched" in line:
            last = line.split(":", 1)[-1].strip()
            break
    if last == today:
        return [f"{today} 기존 Wiki를 확인했고 최신 상태로 유지"]
    engine = "오케스트레이터" if used_orchestrator else "웹 검색"
    prev = f"기존 최근 확인일 {last}" if last else "기존 확인일 없음"
    return [f"{prev} → {today} {engine}으로 업데이트 여부 재확인"]


def research_company(user_id: str, company_name: str, force: bool = False) -> tuple[str, str | None]:
    """업체 정보 수집. Returns: (content, file_id)
    force=True 이면 신선도 체크 없이 강제 재검색.
    """
    creds, contacts_folder_id, knowledge_file_id = _get_creds_and_config(user_id)
    content, file_id, is_fresh = drive.get_company_info(creds, contacts_folder_id, company_name)

    if is_fresh and content and not force:
        return content, file_id

    today = datetime.now().strftime("%Y-%m-%d")

    # 1단계: ParaScope 봇 조회 (보류 — 2026-04-08)
    parascope_section = ""

    # 2단계: Gmail 이메일 맥락 수집
    email_section = ""
    try:
        emails = gmail.search_recent_emails(creds, company_name, company_name)
        email_section = _format_email_context_section(emails, today)
    except Exception as e:
        log.warning(f"Gmail 검색 실패 ({company_name}): {e}")

    # 3단계: 업체 동향 리서치
    # — 오케스트레이터 활성 시 다단계 파이프라인, 실패 시 기존 단일 호출로 폴백
    knowledge = drive.get_company_knowledge(creds, knowledge_file_id)

    news_text = ""
    used_orchestrator = False
    try:
        from agents import research_orchestrator as _ro
        if _ro.is_enabled():
            gmail_excerpt = email_section if email_section else ""
            news_text = _ro.run_company_research(
                company_name=company_name,
                knowledge_md=knowledge or "",
                gmail_context=gmail_excerpt,
            )
            used_orchestrator = True
    except Exception as e:
        log.warning(f"업체 리서치 오케스트레이터 실패, 단일 호출로 폴백 ({company_name}): {e}")
        news_text = ""

    if not used_orchestrator:
        news_text = _filter_parameta_relevant_news(
            _to_bullet_lines(_search(company_news_prompt(company_name)))
        )

    # CM-09: 웹 검색 결과에 출처 태그 추가 (오케스트레이터 산출물에는 이미 출처 URL이 인라인됨)
    if not used_orchestrator and news_text.strip():
        news_lines = []
        for line in news_text.split("\n"):
            if line.strip().startswith("- ") and "[출처:" not in line:
                news_lines.append(f"{line.rstrip()} `[출처: 웹 검색, {today}]`")
            else:
                news_lines.append(line)
        news_text = "\n".join(news_lines)

    connections = _to_bullet_lines(_generate(service_connection_prompt(news_text, knowledge)))
    update_check_lines = _build_update_check_lines(content, today, used_orchestrator)

    # CM-09: 이메일 섹션에 출처 태그 추가
    if email_section and "[출처:" not in email_section:
        email_section = email_section.replace(
            "## 이메일 맥락",
            f"## 이메일 맥락 `[출처: Gmail, {today}]`",
        )

    # CM-10: Sources/ 에 웹 검색 원본 저장
    if news_text.strip():
        try:
            source_content = f"# {company_name} 웹 검색 결과\n- 검색일: {today}\n\n{news_text}"
            drive.save_source_file(
                creds, contacts_folder_id, "Research",
                f"{today}_{company_name}_web_search.md", source_content,
            )
        except Exception as e:
            log.warning(f"Sources/Research 저장 실패 ({company_name}): {e}")

    # 기존 파일에서 리서치 대상이 아닌 섹션(내부 메모 등) 보존 + 기존 frontmatter 보존
    preserved_sections = ""
    existing_fm: dict = {}
    body_for_preserve = content
    if content:
        # frontmatter 분리 (있으면 보존하고 본문에서 제외)
        try:
            existing_fm, body_for_preserve = _drive_parse_frontmatter(content)
        except Exception:
            existing_fm = {}
            body_for_preserve = content
        _RESEARCH_HEADERS = {
            "# ", "## 최근 동향", "## 이메일 맥락", "## 파라메타 서비스 연결점",
            "## ParaScope", "## 업데이트 체크", "## 출처 로그",
        }
        current_section = []
        is_preserved = False
        for line in body_for_preserve.splitlines():
            if line.startswith("## ") or line.startswith("# "):
                if is_preserved and current_section:
                    preserved_sections += "\n".join(current_section) + "\n\n"
                is_preserved = not any(line.startswith(h) for h in _RESEARCH_HEADERS)
                current_section = [line] if is_preserved else []
            elif is_preserved:
                current_section.append(line)
        if is_preserved and current_section:
            preserved_sections += "\n".join(current_section) + "\n\n"

    # Obsidian 호환 frontmatter 구성
    fm_updates = {
        "title": company_name,
        "type": "wiki",
        "stage": "wiki",
        "status": existing_fm.get("status") or "active",
    }
    if not existing_fm.get("tags"):
        fm_updates["tags"] = ["wiki", "active"]
    new_fm = _drive_merge_frontmatter(existing_fm, fm_updates)
    fm_block = _drive_render_frontmatter(new_fm)

    # 섹션 순서: 최근 동향 → 이메일 맥락 → 파라메타 서비스 연결점 → ParaScope → 보존 섹션 → 출처 로그
    sources_log_line = f"- {today}, 웹 검색 + Gmail 자동 리서치 결과"
    new_content = (
        fm_block
        + f"# {company_name}\n\n"
        + f"## 최근 동향\n- last_searched: {today}\n{news_text}\n\n"
        + f"{email_section}\n"
        + f"## 파라메타 서비스 연결점\n{connections}\n\n"
        + f"## 업데이트 체크\n" + "\n".join(f"- {line}" for line in update_check_lines) + "\n\n"
        + f"{parascope_section}"
        + f"{preserved_sections}"
        + f"## 출처 로그\n{_DRIVE_AUTO_START}\n{sources_log_line}\n{_DRIVE_AUTO_END}\n"
    ).rstrip() + "\n"
    file_id = drive.save_company_info(creds, contacts_folder_id, company_name, new_content, file_id)
    return new_content, file_id


def research_person(user_id: str, person_name: str, company_name: str,
                    force: bool = False,
                    card_data: dict = None) -> tuple[str, str | None]:
    """인물 정보 수집. Returns: (info_text, file_id)
    force=True 이면 파일 존재 여부와 무관하게 강제 재검색.
    card_data 가 제공되면 명함 정보를 별도 섹션으로 포함.
    인물 정보 저장 후 연관 기업정보도 자동 갱신(force=False).
    """
    creds, contacts_folder_id, _ = _get_creds_and_config(user_id)
    content, file_id = drive.get_person_info(creds, contacts_folder_id, person_name)
    if content and not force:
        return content, file_id

    today = datetime.now().strftime("%Y-%m-%d")

    # 1단계: Gmail 이메일 맥락 수집 + 헤더에서 이메일 주소 추출
    email_section = ""
    extracted_email = ""
    try:
        emails = gmail.search_recent_emails(creds, person_name, company_name)
        if emails:
            lines = [f"- {e['date']} | {e['subject']} | {e.get('snippet', '')[:100]}"
                     for e in emails[:5]]
            email_section = "\n## 이메일 맥락\n" + "\n".join(lines) + "\n"

            # From/To/CC 헤더에서 person_name 매칭 이메일 주소 추출
            for e in emails:
                for field in ["from", "to", "cc"]:
                    for addr in gmail.parse_address_header(e.get(field, "")):
                        if person_name in addr["name"] and addr["email"]:
                            extracted_email = addr["email"]
                            log.info(f"Gmail 헤더에서 이메일 추출: {person_name} → {extracted_email}")
                            break
                    if extracted_email:
                        break
                if extracted_email:
                    break
    except Exception as e:
        log.warning(f"Gmail 검색 실패 ({person_name}): {e}")

    # CM-09: 이메일 섹션에 출처 태그 추가
    if email_section and "[출처:" not in email_section:
        email_section = email_section.replace(
            "## 이메일 맥락",
            f"## 이메일 맥락 `[출처: Gmail, {today}]`",
        )

    # 2단계: 인물 공개 정보 리서치
    # — 오케스트레이터 활성 시 다단계 파이프라인, 실패 시 기존 단일 호출로 폴백
    info_text = ""
    used_orchestrator = False
    try:
        from agents import research_orchestrator as _ro
        if _ro.is_enabled():
            # gmail_context: 이메일 맥락 섹션을 단순 텍스트 라인으로 전달
            gmail_excerpt = email_section if email_section else ""
            info_text = _ro.run_person_research(
                person_name=person_name,
                company_name=company_name,
                gmail_context=gmail_excerpt,
            )
            used_orchestrator = True
    except Exception as e:
        log.warning(f"인물 리서치 오케스트레이터 실패, 단일 호출로 폴백 ({person_name}): {e}")
        info_text = ""

    if not used_orchestrator:
        info_text = _search(person_info_prompt(person_name, company_name))

    # 이메일: 명함 > Gmail 헤더 순 우선
    if card_data and card_data.get("email"):
        extracted_email = card_data["email"]
    email_line = f"- 이메일: {extracted_email}\n" if extracted_email else ""

    # 명함 섹션 구성 (card_data 있을 때만)
    card_section = ""
    if card_data:
        card_field_map = [
            ("title",      "직책"),
            ("department", "부서"),
            ("phone",      "전화"),
            ("mobile",     "휴대폰"),
            ("fax",        "팩스"),
            ("address",    "주소"),
            ("website",    "웹사이트"),
            ("sns",        "SNS"),
        ]
        card_lines = [f"- last_updated: {today}"]
        for key, label in card_field_map:
            val = (card_data.get(key) or "").strip()
            if val:
                card_lines.append(f"- {label}: {val}")
        card_section = "\n## 명함 정보\n" + "\n".join(card_lines) + "\n"

    # CM-09: 공개 정보에 출처 태그 추가 (오케스트레이터 산출물에는 URL 인라인됨)
    if not used_orchestrator and info_text.strip() and "[출처:" not in info_text:
        info_text = info_text.rstrip() + f" `[출처: 웹 검색, {today}]`"

    # Obsidian 호환 frontmatter 구성
    existing_fm: dict = {}
    if content:
        try:
            existing_fm, _ = _drive_parse_frontmatter(content)
        except Exception:
            existing_fm = {}
    fm_updates = {
        "title": person_name,
        "type": "wiki",
        "stage": "wiki",
        "status": existing_fm.get("status") or "active",
    }
    if company_name:
        fm_updates["related_entities"] = [company_name]
    if not existing_fm.get("tags"):
        fm_updates["tags"] = ["wiki", "active"]
    new_fm = _drive_merge_frontmatter(existing_fm, fm_updates)
    fm_block = _drive_render_frontmatter(new_fm)

    sources_log_line = f"- {today}, 웹 검색 + Gmail 헤더 기반 자동 리서치"
    new_content = (
        fm_block
        + f"# {person_name}\n\n"
        + f"## 기본 정보\n"
        + f"- 소속: [[{company_name}]]\n"
        + f"- last_searched: {today}\n"
        + f"{email_line}"
        + f"{card_section}"
        + f"{email_section}\n"
        + f"## 공개 정보\n{info_text}\n\n"
        + f"## 최근 히스토리\n{_DRIVE_AUTO_START}\n{_DRIVE_AUTO_END}\n\n"
        + f"## 출처 로그\n{_DRIVE_AUTO_START}\n{sources_log_line}\n{_DRIVE_AUTO_END}\n"
    )
    file_id = drive.save_person_info(creds, contacts_folder_id, person_name, new_content, file_id)

    # 연관 기업정보 갱신 (신선도 체크 적용, 7일 이내면 스킵)
    if company_name:
        try:
            research_company(user_id, company_name, force=False)
        except Exception as e:
            log.warning(f"연관 기업정보 갱신 실패 ({company_name}): {e}")

    return new_content, file_id


def get_previous_context(user_id: str, company_name: str, person_names: list[str]) -> dict:
    """Gmail 이전 이메일 + Drive 회의록 맥락 수집"""
    creds = user_store.get_credentials(user_id)

    # Gmail 이메일 검색
    emails = []
    for name in person_names[:2]:
        emails += gmail.search_recent_emails(creds, name, company_name)

    # Drive 회의록 검색 (파일명에 업체명 포함된 것)
    # NFD/NFC 정규화: macOS 업로드 파일은 NFD, 코드 생성 문자열은 NFC → 비교 전 통일
    minutes = []
    try:
        import unicodedata
        user = user_store.get_user(user_id)
        minutes_folder_id = user.get("minutes_folder_id")
        if minutes_folder_id:
            all_minutes = drive.list_minutes(creds, minutes_folder_id)
            company_lower = unicodedata.normalize("NFC", company_name).lower()
            for f in all_minutes:
                name_nfc = unicodedata.normalize("NFC", f.get("name", ""))
                if company_lower in name_nfc.lower() and "_내부용" in name_nfc:
                    minutes.append(f)
                    if len(minutes) >= 3:
                        break
    except Exception as e:
        log.warning(f"회의록 검색 실패: {e}")

    # Trello 카드 컨텍스트 조회 (미완료 체크리스트 항목 + 최근 코멘트)
    trello_items = []
    trello_context = {}
    trello_summary = []
    try:
        trello_context = trello.get_card_context(user_id, company_name, limit_comments=3)
        trello_items = trello_context.get("incomplete_items", [])
        trello_summary = _build_trello_summary(trello_context)
    except Exception as e:
        log.warning(f"Trello 조회 실패: {e}")

    return {
        "trello": trello_items,
        "trello_summary": trello_summary,
        "trello_card_name": trello_context.get("card_name", "") if trello_context else "",
        "trello_url": trello_context.get("url", "") if trello_context else "",
        "emails": emails[:3],
        "minutes": minutes,
    }


# ── 브리핑 생성 ──────────────────────────────────────────────

def run_briefing(slack_client, user_id: str, event: dict = None,
                 channel: str = None, thread_ts: str = None,
                 days: int = 1,
                 start_date: str = None, end_date: str = None,
                 period_text: str = None) -> list[str]:
    """
    브리핑 실행. event가 None이면 캘린더 조회.
    start_date/end_date가 지정되면 해당 범위 조회, 아니면 days 기준.
    Returns: 발송된 메시지 ts 목록
    """
    _cleanup_old_drafts()
    creds = user_store.get_credentials(user_id)
    user = user_store.get_user(user_id)
    contacts_folder_id = user["contacts_folder_id"]

    # 브리핑 기간 텍스트 (인트로 메시지 + 미팅 없음 메시지용)
    if not period_text:
        if days == 1:
            period_text = "향후 24시간"
        elif days == 7:
            period_text = "이번 주"
        else:
            period_text = f"향후 {days}일"

    if event:
        events = [event]
    else:
        # 인트로 메시지 먼저 발송
        try:
            user_info = slack_client.users_info(user=user_id)
            display_name = (
                user_info["user"]["profile"].get("display_name")
                or user_info["user"]["profile"].get("real_name")
                or "사용자"
            )
        except Exception:
            display_name = "사용자"
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"📅 {display_name}님의 {period_text} 일정을 보여드리겠습니다.")

        if start_date and end_date:
            events = cal.get_upcoming_meetings(creds, start_date=start_date, end_date=end_date)
        else:
            events = cal.get_upcoming_meetings(creds, days=days, from_now=True)

    if not events:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"📅 {period_text} 내 미팅이 없습니다.")
        return []

    sent_threads = []
    research_queue: list[tuple[dict, str]] = []  # (meeting, company_name)

    # 기존 업체 목록 로딩 (LLM 추론 시 후보로 사용, FR-B14)
    existing_companies = []
    try:
        contacts_folder_id = user_store.get_user(user_id).get("contacts_folder_id")
        if contacts_folder_id:
            existing_companies = drive.get_company_names(creds, contacts_folder_id)
    except Exception as e:
        log.warning(f"기존 업체 목록 로딩 실패: {e}")

    # 1단계: 모든 미팅 헤더 즉시 발송 (리서치 없이)
    for ev in events:
        meeting = cal.parse_event(ev)

        # extendedProperties에 명시된 업체명 (쉼표 구분 복수 업체 가능)
        company_raw = (
            ev.get("extendedProperties", {}).get("private", {}).get("company")
        )
        company_names = [c.strip() for c in company_raw.split(",") if c.strip()] if company_raw else []

        # FR-B13: extendedProperties 없으면 LLM 추론 폴백
        if not company_names:
            inferred = _infer_company_from_title(
                meeting.get("summary", ""),
                company_candidates=existing_companies,
            )
            if inferred:
                company_names = [c.strip() for c in inferred.split(",") if c.strip()]
                log.info(f"업체명 추론 성공 (제목): '{meeting.get('summary')}' → {company_names}")

        # FR-B16: 제목 추론 실패 시 참석자 기반 역추론
        if not company_names:
            attendees = ev.get("attendees", [])
            if attendees:
                inferred = _infer_company_from_attendees(
                    attendees, creds=creds, contacts_folder_id=contacts_folder_id,
                )
                if inferred:
                    company_names = [inferred]
                    log.info(f"업체명 추론 성공 (참석자): '{meeting.get('summary')}' → {company_names}")

        # FR-B15: 추론 결과를 extendedProperties에 저장 (다음 조회 시 재사용)
        if company_names and not company_raw:
            try:
                event_id = ev.get("id")
                if event_id:
                    cal.update_event(creds, event_id,
                                     extended_properties={"private": {"company": ", ".join(company_names)}})
                    log.info(f"추론 업체명 extendedProperties 저장: {event_id} → {company_names}")
            except Exception as ep_err:
                log.warning(f"extendedProperties 저장 실패: {ep_err}")

        if company_names:
            # 첫 번째 업체를 대표로 브리핑 헤더 발송
            ts = _send_briefing(slack_client, user_id, meeting, ", ".join(company_names),
                                channel=channel, thread_ts=thread_ts)
            if ts:
                # 각 업체별로 개별 리서치 큐 등록
                for cn in company_names:
                    research_queue.append((meeting, cn))
        else:
            ts = _send_internal_briefing(slack_client, user_id, meeting,
                                         channel=channel, thread_ts=thread_ts)
        if ts:
            sent_threads.append(ts)

    # 2단계: 업체 리서치를 단일 백그라운드 스레드에서 순차 실행 (섞임 방지)
    if research_queue:
        threading.Thread(
            target=_run_all_briefing_research,
            args=(slack_client, user_id, research_queue, channel, thread_ts),
            daemon=True,
        ).start()

    user_store.update_last_active(user_id)
    return sent_threads


def _extract_company_content_sections(company_content: str) -> tuple[list[str], list[str], list[str], list[dict], list[str]]:
    """업체 Drive 파일에서 뉴스·ParaScope·연결점·이메일 섹션을 추출.
    Returns: (news_lines, parascope_lines, connection_lines, drive_emails)
    """
    # 최근 동향 섹션 추출
    news_lines: list[str] = []
    news_section_lines: list[str] = []
    in_news = False
    for line in company_content.splitlines():
        if "최근 동향" in line and line.strip().startswith("#"):
            in_news = True
            continue
        if in_news:
            if line.strip().startswith("#"):
                break
            if "last_searched" in line:
                continue
            news_section_lines.append(line)
    news_section = "\n".join(news_section_lines)

    raw_blocks = re.split(r'\n\s*-{3,}\s*\n', news_section)
    if len(raw_blocks) <= 1:
        raw_blocks = []
        cur: list[str] = []
        for line in news_section_lines:
            if line.strip() in ("-", "•"):
                if cur:
                    raw_blocks.append("\n".join(cur))
                cur = []
            else:
                cur.append(line)
        if cur:
            raw_blocks.append("\n".join(cur))

    for block in raw_blocks:
        if len(news_lines) >= 3:
            break
        block = block.strip()
        if not block:
            continue
        block_lines = [l.strip() for l in block.splitlines() if l.strip()]
        has_bold = any("**" in l for l in block_lines)
        has_url = any("http" in l or "출처" in l for l in block_lines)
        if not has_bold and not has_url and len(block) > 120:
            continue
        title = ""
        for l in block_lines:
            m = re.search(r'\*\*(.+?)\*\*', l)
            if m:
                title = re.sub(r'^\d+\.\s*', '', m.group(1).strip())
                break
        if not title:
            title = block_lines[0][:120] if block_lines else ""
        url = ""
        for l in block_lines:
            url_m = re.search(r'https?://\S+', l)
            if url_m:
                url = url_m.group(0).rstrip(')').rstrip('.')
                break
        if title:
            news_lines.append(f"{title} ({url})" if url else title)

    # ParaScope 섹션 추출
    parascope_lines: list[str] = []
    in_parascope = False
    for line in company_content.splitlines():
        if "ParaScope 브리핑" in line and line.strip().startswith("#"):
            in_parascope = True
            continue
        if in_parascope:
            if line.strip().startswith("#"):
                break
            stripped = line.strip()
            if not stripped or "last_searched" in stripped:
                continue
            parascope_lines.append(stripped)

    # 서비스 연결점 섹션 추출
    connection_lines: list[str] = []
    in_connections = False
    for line in company_content.splitlines():
        if "## 파라메타 서비스 연결점" in line:
            in_connections = True
            continue
        if in_connections:
            if line.startswith("##"):
                break
            if line.strip().startswith("-"):
                connection_lines.append(line.strip("- ").strip())
    connection_lines = connection_lines[:3]

    # 업데이트 체크 섹션 추출
    update_lines: list[str] = []
    in_update = False
    for line in company_content.splitlines():
        if "## 업데이트 체크" in line:
            in_update = True
            continue
        if in_update:
            if line.startswith("##"):
                break
            stripped = line.strip()
            if stripped.startswith("-"):
                update_lines.append(stripped.lstrip("- ").strip())
                if len(update_lines) >= 3:
                    break

    # 이메일 맥락 섹션 추출 (Drive 파일 보완용)
    drive_emails: list[dict] = []
    in_email = False
    for line in company_content.splitlines():
        if "이메일 맥락" in line and line.strip().startswith("#"):
            in_email = True
            continue
        if in_email:
            if line.strip().startswith("#"):
                break
            stripped = line.strip()
            if not stripped or "last_searched" in stripped:
                continue
            cleaned = stripped.lstrip("-•").strip()
            if cleaned:
                parts = cleaned.split("|", 2)
                drive_emails.append({
                    "date": parts[0].strip() if len(parts) > 0 else "",
                    "subject": parts[1].strip() if len(parts) > 1 else cleaned,
                    "snippet": parts[2].strip() if len(parts) > 2 else "",
                })
                if len(drive_emails) >= 3:
                    break

    return news_lines, parascope_lines, connection_lines, drive_emails, update_lines


def _run_briefing_research(
    slack_client, user_id: str, meeting: dict, company_name: str,
    channel: str | None, thread_ts: str | None,
) -> None:
    """백그라운드 스레드: 업체·인물 리서치 후 순차적으로 Slack에 발송."""
    try:
        _ch = channel or user_id

        # 0. 회의 분류 — 내부 회의면 리서치 건너뜀 (QA 2.1)
        try:
            from agents import briefing_classifier
            if briefing_classifier.is_enabled():
                cls_result = briefing_classifier.classify_meeting(
                    title=meeting.get("summary", ""),
                    attendees=meeting.get("attendees", []),
                    company_hint=company_name or "",
                    description=meeting.get("description", ""),
                )
                if cls_result and (
                    cls_result.get("meeting_type") == "internal"
                    or cls_result.get("research_recommended") is False
                ):
                    rationale = cls_result.get("rationale", "")
                    log.info(
                        f"브리핑 리서치 스킵 (내부 회의 판정): {company_name} — {rationale}"
                    )
                    return
        except Exception as cls_err:
            log.warning(f"브리핑 분류기 호출 실패, 기본 경로로 리서치 진행: {cls_err}")

        # 1. 업체 리서치
        progress_resp = _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"🔍 *{company_name}* 업체 리서치 중...")
        progress_ts = progress_resp.get("ts") if progress_resp else None
        company_content = ""
        try:
            company_content, _ = research_company(user_id, company_name)
        except Exception as e:
            err = str(e)
            msg = ("⚠️ AI API 할당량 초과. 잠시 후 다시 시도해주세요."
                   if "429" in err else f"⚠️ 업체 리서치 오류: {err[:200]}")
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts, text=msg)

        # 진행 메시지 삭제
        if progress_ts:
            try:
                slack_client.chat_delete(channel=_ch, ts=progress_ts)
            except Exception:
                pass

        news_lines, parascope_lines, connection_lines, drive_emails, update_lines = \
            _extract_company_content_sections(company_content)
        log.info(f"news_lines ({company_name}): {news_lines}")

        company_blocks = build_company_research_block(
            company_name, news_lines, parascope_lines, connection_lines, update_lines
        )
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              blocks=company_blocks, text=f"🏢 {company_name} 업체 정보")

        # 2. 인물 리서치 (순차적으로 각 인물 완료 시 발송) — 내부 도메인 제외
        _internal_domains = set(
            os.getenv("INTERNAL_DOMAINS", "parametacorp.com,iconloop.com").split(","))
        person_names = [a.get("name") or a.get("email", "").split("@")[0]
                        for a in meeting.get("attendees", [])
                        if a.get("email", "").split("@")[-1] not in _internal_domains]
        persons_info: list[dict] = []
        for name in person_names[:3]:
            progress_resp = _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text=f"👤 *{name}* 인물 리서치 중...")
            progress_ts = progress_resp.get("ts") if progress_resp else None
            try:
                info, _ = research_person(user_id, name, company_name)
            except Exception:
                info = ""
            persons_info.append({"name": name, "raw": info})
            # 진행 메시지 삭제
            if progress_ts:
                try:
                    slack_client.chat_delete(channel=_ch, ts=progress_ts)
                except Exception:
                    pass

        if persons_info:
            person_blocks = build_persons_block([{"name": p["name"]} for p in persons_info])
            if person_blocks:
                _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                      blocks=person_blocks, text="👤 담당자 정보")

        # 3. 이전 맥락 조회
        progress_resp = _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="📨 이전 커뮤니케이션 맥락 조회 중...")
        progress_ts = progress_resp.get("ts") if progress_resp else None
        context = get_previous_context(user_id, company_name, person_names)

        if not context.get("emails") and drive_emails:
            context = {**context, "emails": drive_emails}

        if progress_ts:
            try:
                slack_client.chat_delete(channel=_ch, ts=progress_ts)
            except Exception:
                pass

        context_blocks = build_context_block(context)
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              blocks=context_blocks, text="📌 이전 미팅 맥락")

    except Exception:
        log.exception(f"브리핑 리서치 오류: {company_name}")


def _run_all_briefing_research(
    slack_client, user_id: str,
    research_queue: list[tuple[dict, str]],
    channel: str | None, thread_ts: str | None,
) -> None:
    """업체 리서치를 순차적으로 실행 (섞임 방지).
    research_queue: [(meeting, company_name), ...]
    """
    for meeting, company_name in research_queue:
        _run_briefing_research(slack_client, user_id, meeting, company_name, channel, thread_ts)


def _meeting_to_info(meeting: dict, company_name: str = None) -> dict:
    """Calendar 이벤트를 merge_meeting_prompt가 기대하는 info dict로 변환"""
    start_str = meeting.get("start_time", "")
    end_str = meeting.get("end_time", "")
    try:
        start_dt = datetime.fromisoformat(start_str)
        date_str = start_dt.strftime("%Y-%m-%d")
        time_str = start_dt.strftime("%H:%M")
    except Exception:
        date_str, time_str = "", ""
    try:
        duration = int((datetime.fromisoformat(end_str) - datetime.fromisoformat(start_str)).total_seconds() / 60)
    except Exception:
        duration = 60
    attendees = meeting.get("attendees", [])
    return {
        "date": date_str,
        "time": time_str,
        "duration_minutes": duration,
        "participants": [a.get("name", "") for a in attendees if a.get("name")],
        "participant_emails": {a["name"]: a["email"] for a in attendees if a.get("name") and a.get("email")},
        "company_candidates": [company_name] if company_name else [],
        "company_confirmed": bool(company_name),
        "title": meeting.get("summary", ""),
        "agenda": meeting.get("description", ""),
        "location": meeting.get("location", ""),
    }


def _register_briefing_draft(msg_ts: str, user_id: str, meeting: dict,
                              company_name: str = None, channel: str = None):
    """브리핑 스레드를 _meeting_drafts에 등록하여 스레드 답글로 일정 수정 가능하게 함"""
    attendees = meeting.get("attendees", [])
    _meeting_drafts[msg_ts] = {
        "user_id": user_id,
        "event_id": meeting["id"],
        "info": _meeting_to_info(meeting, company_name),
        "company": company_name,
        "attendee_emails": [a["email"] for a in attendees if a.get("email")],
        "channel": channel,
        "reply_ts": None,
        "source": "briefing",
        "created_at": datetime.now().isoformat(),
    }
    _save_meeting_drafts()


def _send_briefing(slack_client, user_id: str, meeting: dict, company_name: str,
                   channel: str = None, thread_ts: str = None) -> str | None:
    """미팅 기본 정보 블록만 즉시 발송. Returns: 발송된 메시지 ts

    리서치는 호출자(run_briefing)가 단일 백그라운드 스레드에서 관리한다.
    여러 업체가 있을 때 섞이지 않도록 스레드를 여기서 시작하지 않는다.
    """
    try:
        attendee_names = _resolve_attendee_names(
            meeting.get("attendees", []), user_id, slack_client)
        header_blocks = build_meeting_header_block(meeting, company_name, attendee_names)
        resp = _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                     blocks=header_blocks, text=f"{company_name} 미팅 브리핑")
        if not resp:
            return None
        msg_ts = resp["ts"]
        _pending_agenda[msg_ts] = [meeting["id"], user_id]
        _save_pending_agenda(_pending_agenda)
        _register_briefing_draft(msg_ts, user_id, meeting, company_name, channel)
        return msg_ts

    except Exception as e:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 브리핑 생성 중 오류 발생: {e}")
        return None


def _send_internal_briefing(slack_client, user_id: str, meeting: dict,
                             channel: str = None, thread_ts: str = None) -> str | None:
    """내부 미팅 간단 브리핑 발송"""
    try:
        time_str = format_time(meeting.get("start_time", ""))
        meet_link = meeting.get("meet_link", "")
        link_text = f"<{meet_link}|Google Meet>" if meet_link else "미팅"
        location = meeting.get("location", "")
        attendee_names = _resolve_attendee_names(
            meeting.get("attendees", []), user_id, slack_client)
        agenda = meeting.get("description", "").strip()

        location_str = f" · 📍{location}" if location else ""
        lines = [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"*📋 {meeting['summary']} — {time_str} ({link_text}){location_str}*",
        ]
        if attendee_names:
            lines.append(f"👥  *참석자*: {', '.join(attendee_names)}")
        if agenda:
            lines.append(f"📝  *어젠다*:\n{agenda}")
        else:
            lines.append("📝  _(어젠다 등록 및 내용을 수정하려면 이 스레드에 답장하세요)_")

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
        resp = _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                     blocks=blocks, text=f"{meeting['summary']} 미팅 브리핑")
        msg_ts = resp["ts"]
        _pending_agenda[msg_ts] = [meeting["id"], user_id]
        _save_pending_agenda(_pending_agenda)
        _register_briefing_draft(msg_ts, user_id, meeting, None, channel)
        return msg_ts
    except Exception as e:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 브리핑 오류: {e}")
        return None


# ── 어젠다 등록 ──────────────────────────────────────────────

def handle_agenda_reply(slack_client, thread_ts: str, text: str):
    """브리핑 스레드 답장 → 어젠다 등록"""
    entry = _pending_agenda.get(thread_ts)
    if not entry:
        return
    event_id, user_id = entry

    try:
        creds = user_store.get_credentials(user_id)
        cal.update_event_description(creds, event_id, f"{text}")
        slack_client.chat_postMessage(
            channel=user_id,
            thread_ts=thread_ts,
            text="✅ 어젠다가 Calendar 이벤트에 등록되었습니다.",
        )
        del _pending_agenda[thread_ts]
        _save_pending_agenda(_pending_agenda)
    except Exception as e:
        slack_client.chat_postMessage(
            channel=user_id,
            thread_ts=thread_ts,
            text=f"⚠️ 어젠다 등록 실패: {e}",
        )


# ── 자연어 미팅 생성 ─────────────────────────────────────────

def create_meeting_from_text(slack_client, user_id: str, user_message: str,
                             channel: str = None, thread_ts: str = None,
                             user_msg_ts: str = None):
    """자연어 요청으로 Calendar 미팅 생성"""
    try:
        raw = _generate(parse_meeting_prompt(user_message))
    except Exception as e:
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text="⚠️ AI API 할당량 초과입니다. 잠시 후 다시 시도해주세요.")
        else:
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text=f"⚠️ AI 호출 오류: {err[:200]}")
        return

    log.info(f"LLM 파싱 응답: {raw[:200]}")
    cleaned = re.sub(r"```(?:json)?", "", raw).strip().strip("`")
    json_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not json_match:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 미팅 정보를 파싱하지 못했어요.\n예: '오늘 15시에 김민환 미팅 잡아줘'\n\n디버그: `{raw[:200]}`")
        return

    try:
        info = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        log.error(f"JSON 파싱 실패: {e}\n원본: {json_match.group()}")
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ JSON 파싱 오류: {e}\n원본 응답: `{raw[:300]}`")
        return

    # 업체명 추출 (company_candidates / company_confirmed)
    company_candidates = info.get("company_candidates", [])
    company_confirmed = info.get("company_confirmed", False)
    # 하위 호환: 기존 company 필드도 처리
    if not company_candidates and info.get("company"):
        old_company = info["company"]
        if isinstance(old_company, str) and old_company.lower() not in ("null", "none", ""):
            company_candidates = [old_company]
            company_confirmed = True
    company = company_candidates[0] if (company_candidates and company_confirmed) else None
    log.info(f"미팅 파싱 결과 — candidates={company_candidates}, confirmed={company_confirmed}, participants={info.get('participants')}")

    attendee_emails = []
    missing_names = []
    pending_selections = []  # 이메일 후보가 여러 개인 참석자 (일정 생성 후 선택)
    inline_emails = info.get("participant_emails", {})

    for name in info.get("participants", []):
        # LLM이 인라인으로 이메일을 추출한 경우 우선 사용
        if inline_email := inline_emails.get(name):
            attendee_emails.append(inline_email)
            continue
        candidates = _find_email_candidates(user_id, name, slack_client)
        if len(candidates) == 1:
            attendee_emails.append(candidates[0])
        elif len(candidates) > 1:
            pending_selections.append({"name": name, "candidates": candidates})
        else:
            missing_names.append(name)

    # 생성자 본인을 항상 참석자에 포함 — Slack 프로필 이메일로 조회 (B5)
    creator_email = _lookup_slack_email(slack_client, user_id)
    if creator_email and creator_email.lower() not in {e.lower() for e in attendee_emails}:
        attendee_emails.insert(0, creator_email)

    # I2(a): 참석자·정보 확인 후 생성 — preview 블록 후 사용자 승인
    _post_create_preview(
        slack_client, user_id=user_id,
        info=info, company=company, attendee_emails=attendee_emails,
        pending_selections=pending_selections, missing_names=missing_names,
        channel=channel, thread_ts=thread_ts, user_msg_ts=user_msg_ts,
    )


def _post_create_preview(slack_client, *, user_id: str, info: dict,
                          company: str | None, attendee_emails: list[str],
                          pending_selections: list, missing_names: list,
                          channel: str | None, thread_ts: str | None,
                          user_msg_ts: str | None):
    """I2(a): 생성 직전 참석자·일정 확인 블록 발송. 승인 시 실제 생성."""
    # 미리보기 텍스트 구성
    title = info.get("title", "미팅")
    date = info.get("date") or "?"
    time_ = info.get("time") or "?"
    duration = info.get("duration_minutes", 60)
    lines = [
        f"📋 *{title}* — 아래 내용으로 생성할까요?",
        f"• 일시: {date} {time_} ({duration}분)",
    ]
    if company:
        lines.append(f"• 업체: {company}")
    if attendee_emails:
        lines.append(f"• 참석자: {', '.join(attendee_emails)}")
    if pending_selections:
        names = [s["name"] for s in pending_selections]
        lines.append(f"• ⏳ 이메일 후보 복수 — 생성 후 선택: {', '.join(names)}")
    if missing_names:
        lines.append(f"• ⚠️ 이메일 미발견: {', '.join(missing_names)}")
    agenda = info.get("agenda") or ""
    if agenda:
        lines.append(f"• 어젠다: {agenda[:100]}")
    preview_text = "\n".join(lines)

    # draft 저장 (승인 시 실제 생성에 사용)
    draft_id = f"{user_id}:{int(datetime.now().timestamp())}"
    _pending_create_confirm[draft_id] = {
        "user_id": user_id,
        "info": dict(info),
        "company": company,
        "attendee_emails": list(attendee_emails),
        "pending_selections": pending_selections or [],
        "missing_names": missing_names or [],
        "channel": channel,
        "thread_ts": thread_ts,
        "user_msg_ts": user_msg_ts,
    }
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": preview_text}},
        {"type": "actions", "elements": [
            {"type": "button", "style": "primary",
                "text": {"type": "plain_text", "text": "✅ 확인하고 생성"},
                "action_id": "create_confirm", "value": draft_id},
            {"type": "button", "style": "danger",
                "text": {"type": "plain_text", "text": "❌ 취소"},
                "action_id": "create_abort", "value": draft_id},
        ]},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "_생성 후에도 이 메시지 스레드에 답글로 제목·참석자·어젠다를 수정할 수 있어요._"}]},
    ]
    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text=f"미팅 생성 확인: {title}", blocks=blocks)


def handle_create_confirm(slack_client, user_id: str, draft_id: str,
                           body: dict = None):
    """확인 버튼 콜백 — 실제 생성 경로로 진입"""
    payload = _pending_create_confirm.pop(draft_id, None)
    if not payload:
        _post(slack_client, user_id=user_id, text="⚠️ 만료된 미팅 확인 요청입니다.")
        return

    _replace_block(slack_client, body, "✅ 확인됨 — 미팅을 생성합니다...")

    created_event_id = _create_calendar_event(
        slack_client, user_id,
        payload["info"], payload["company"], payload["attendee_emails"],
        payload.get("channel"), payload.get("thread_ts"),
        user_msg_ts=payload.get("user_msg_ts"),
    )

    if not created_event_id:
        return

    # 이메일 미확정 참석자 선택 UI
    if payload["pending_selections"]:
        _pending_meetings[user_id] = {
            "event_id": created_event_id,
            "channel": payload.get("channel"),
            "thread_ts": payload.get("thread_ts"),
            "pending_selections": payload["pending_selections"],
        }
        try:
            _post_email_selection(slack_client, user_id,
                                  payload["pending_selections"][0],
                                  payload.get("channel"), payload.get("thread_ts"))
        except Exception as e:
            log.exception(f"이메일 선택 블록 발송 실패: {e}")
            _pending_meetings.pop(user_id, None)

    # 업체명 후보가 있지만 확정 안 됨 → 확인 요청
    info = payload["info"]
    cand = info.get("company_candidates") or []
    confirmed = info.get("company_confirmed", False)
    if cand and not confirmed:
        _post_company_confirmation(
            slack_client, user_id, cand,
            event_id=created_event_id,
            channel=payload.get("channel"),
            thread_ts=payload.get("thread_ts"),
        )


def handle_create_abort(slack_client, user_id: str, draft_id: str,
                         body: dict = None):
    _pending_create_confirm.pop(draft_id, None)
    _replace_block(slack_client, body, "❌ 미팅 생성을 취소했습니다.")


def _lookup_slack_email(slack_client, user_id: str) -> str | None:
    """Slack 사용자 프로필에서 이메일 조회. 실패 시 None."""
    try:
        info = slack_client.users_info(user=user_id)
        email = (info.get("user", {}).get("profile", {}) or {}).get("email", "").strip()
        return email or None
    except Exception as e:
        log.warning(f"Slack 이메일 조회 실패 ({user_id}): {e}")
        return None


def _find_email_candidates(user_id: str, name: str, slack_client) -> list[str]:
    """이름으로 이메일 후보 전체 수집 (중복 제거, 순서 유지).
    Slack → Gmail 헤더 → Google 주소록 → Drive Contacts 순으로 탐색.
    """
    found: list[str] = []
    seen: set[str] = set()

    def _add(email: str):
        e = email.strip().lower()
        if e and e not in seen:
            seen.add(e)
            found.append(email.strip())

    # 1순위: Slack 워크스페이스 멤버
    try:
        result = slack_client.users_list()
        for user in result["members"]:
            profile = user.get("profile", {})
            if name in profile.get("real_name", "") or name in profile.get("display_name", ""):
                if e := profile.get("email"):
                    _add(e)
    except Exception:
        pass

    creds = None
    contacts_folder_id = None
    try:
        creds, contacts_folder_id, _ = _get_creds_and_config(user_id)
    except Exception as e:
        log.warning(f"_find_email_candidates: creds 로드 실패 — {e}")
        return found

    # 2순위: Gmail 이메일 헤더
    try:
        if e := gmail.find_email_by_name(creds, name):
            _add(e)
    except Exception as e:
        log.warning(f"_find_email_candidates: Gmail 헤더 조회 실패 — {e}")

    # 3순위: Google 주소록 (People API)
    try:
        if e := gmail.find_email_in_contacts(creds, name):
            _add(e)
    except Exception as e:
        log.warning(f"_find_email_candidates: Google 주소록 조회 실패 — {e}")

    # 4순위: Drive People/{이름}.md
    try:
        content, _ = drive.get_person_info(creds, contacts_folder_id, name)
        if content:
            for line in content.splitlines():
                if "이메일:" in line or "email:" in line.lower():
                    e = line.split(":", 1)[1].strip()
                    if e:
                        _add(e)
    except Exception as e:
        log.warning(f"_find_email_candidates: Drive 조회 실패 — {e}")

    log.info(f"_find_email_candidates: {name} → {found}")
    return found


def _find_email(user_id: str, name: str, slack_client) -> str | None:
    """이름으로 이메일 단일 조회 (후보 중 첫 번째 반환)"""
    candidates = _find_email_candidates(user_id, name, slack_client)
    return candidates[0] if candidates else None


# ── 이메일 선택 대기 상태 ─────────────────────────────────────
# user_id → {info, company, channel, thread_ts, attendee_emails, missing_names, pending_selections}
_pending_meetings: dict[str, dict] = {}

# ── 일정 드래프트 상태 ────────────────────────────────────────
# thread_ts → {user_id, info, company, event_id, channel, reply_ts, source, created_at, ...}
_MEETING_DRAFTS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "meeting_drafts.json")


def _load_meeting_drafts() -> dict:
    try:
        with open(_MEETING_DRAFTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_meeting_drafts():
    try:
        os.makedirs(os.path.dirname(_MEETING_DRAFTS_FILE), exist_ok=True)
        with open(_MEETING_DRAFTS_FILE, "w", encoding="utf-8") as f:
            json.dump(_meeting_drafts, f, ensure_ascii=False)
    except Exception as e:
        log.warning(f"meeting_drafts 저장 실패: {e}")


_meeting_drafts: dict[str, dict] = _load_meeting_drafts()

# F2: 일정 취소 확정 대기 — key=event_id (UI 리마인드용)
_pending_cancel: dict[str, dict] = {}
# F2 확장: 드림플러스 예약 연동 취소 대기 — key=event_id, value={user_id, event_id, reservation_id, summary, location}
_pending_meeting_cancel_with_room: dict[str, dict] = {}
# I2(a): 미팅 생성 확인 대기 — key=draft_id, value={info, company, attendee_emails, ...}
_pending_create_confirm: dict[str, dict] = {}
# I2(b): 드림플러스 회의실 자동 추천 수락 대기 — key=slack user_id, value={start_dt, end_dt, title, attendee_count, channel, thread_ts, event_id}
_pending_room_offer: dict[str, dict] = {}


def _migrate_pending_agenda():
    """기존 _pending_agenda 항목을 _meeting_drafts로 마이그레이션 (1회성)"""
    migrated = False
    for ts, entry in list(_pending_agenda.items()):
        if ts not in _meeting_drafts and isinstance(entry, list) and len(entry) == 2:
            event_id, user_id = entry
            _meeting_drafts[ts] = {
                "user_id": user_id,
                "event_id": event_id,
                "info": {"title": "", "agenda": "", "date": "", "time": "",
                         "duration_minutes": 60, "participants": [],
                         "participant_emails": {}, "company_candidates": [],
                         "company_confirmed": False, "location": ""},
                "company": None,
                "attendee_emails": [],
                "channel": None,
                "reply_ts": None,
                "source": "briefing",
                "created_at": datetime.now().isoformat(),
            }
            migrated = True
    if migrated:
        _save_meeting_drafts()
        log.info(f"pending_agenda → meeting_drafts 마이그레이션 완료: {len(_pending_agenda)}건")


def _cleanup_old_drafts(max_days: int = 7):
    """오래된 드래프트 정리"""
    now = datetime.now()
    to_remove = []
    for ts, draft in _meeting_drafts.items():
        try:
            created = datetime.fromisoformat(draft.get("created_at", ""))
            if (now - created).days > max_days:
                to_remove.append(ts)
        except Exception:
            pass
    for ts in to_remove:
        del _meeting_drafts[ts]
    if to_remove:
        _save_meeting_drafts()
        log.info(f"오래된 드래프트 {len(to_remove)}건 정리")


_migrate_pending_agenda()


def _post_company_confirmation(slack_client, user_id: str, candidates: list[str],
                                event_id: str = None,
                                channel: str = None, thread_ts: str = None):
    """업체명 다중 선택 확인 메시지 발송 (체크박스 + 확인 버튼)"""
    options = [
        {
            "text": {"type": "mrkdwn", "text": f"*{name}*"},
            "value": name,
        }
        for name in candidates
    ]
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "🏢 다음 업체가 감지되었습니다. 관련 업체를 선택해주세요:"},
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "checkboxes",
                    "action_id": "company_checkboxes",
                    "initial_options": options,  # 기본 전체 선택
                    "options": options,
                },
            ],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "확인"},
                    "value": json.dumps({"event_id": event_id, "candidates": candidates}),
                    "action_id": "confirm_company_submit",
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "업체 없음 (내부 회의)"},
                    "value": json.dumps({"event_id": event_id}),
                    "action_id": "confirm_company_none",
                },
            ],
        },
    ]
    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          blocks=blocks, text="업체명 확인")


def handle_company_confirmation(slack_client, body: dict):
    """업체 확인/업체 없음 버튼 클릭 → 캘린더 이벤트의 업체(company) 필드 업데이트"""
    user_id = body["user"]["id"]
    action = body["actions"][0]
    action_id = action.get("action_id", "")

    # 체크박스 토글은 무시 (확인 버튼 클릭 시에만 처리)
    if action_id == "company_checkboxes":
        return

    try:
        payload = json.loads(action["value"])
        event_id = payload.get("event_id")
    except (KeyError, json.JSONDecodeError):
        return

    # "업체 없음" 버튼
    if action_id == "confirm_company_none":
        slack_client.chat_postMessage(channel=user_id, text="내부 회의로 처리합니다.")
        return

    # "확인" 버튼 — 체크박스에서 선택된 업체 추출
    selected = []
    state_values = body.get("state", {}).get("values", {})
    for block_values in state_values.values():
        cb = block_values.get("company_checkboxes")
        if cb and cb.get("selected_options"):
            selected = [opt["value"] for opt in cb["selected_options"]]
            break

    if not selected:
        slack_client.chat_postMessage(channel=user_id, text="선택된 업체가 없습니다. 내부 회의로 처리합니다.")
        return

    company = ", ".join(selected)

    # 캘린더 이벤트 extendedProperties에 업체명 저장
    if event_id:
        try:
            creds = user_store.get_credentials(user_id)
            cal.update_event(creds, event_id,
                             extended_properties={"private": {"company": company}})
        except Exception as e:
            log.warning(f"업체명 extendedProperties 저장 실패: {e}")

        # _meeting_drafts에도 company 반영
        for ts_key, draft in _meeting_drafts.items():
            if draft.get("event_id") == event_id:
                draft["company"] = company

    slack_client.chat_postMessage(
        channel=user_id, text=f"🏢 업체가 *{company}*(으)로 등록되었습니다."
    )


def _post_email_selection(slack_client, user_id: str, selection: dict,
                          channel: str = None, thread_ts: str = None):
    """이메일 후보 선택 Block Kit 메시지 발송"""
    name = selection["name"]
    candidates = selection["candidates"]
    buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": email},
            "value": f"{user_id}|{email}",
            "action_id": f"select_attendee_email_{i}",
        }
        for i, email in enumerate(candidates)
    ] + [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "이 참석자 제외"},
            "value": f"{user_id}|__skip__",
            "action_id": f"select_attendee_email_{len(candidates)}",
            "style": "danger",
        }
    ]
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"*{name}*의 이메일이 여러 개 발견되었습니다. 사용할 이메일을 선택해주세요:"},
        },
        {"type": "actions", "elements": buttons},
    ]
    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          blocks=blocks, text=f"{name}의 이메일 선택")


def _create_calendar_event(slack_client, user_id: str, info: dict, company: str | None,
                           attendee_emails: list[str], channel: str = None,
                           thread_ts: str = None, user_msg_ts: str = None):
    """Calendar 이벤트 생성, 브리핑 실행, 드림플러스 회의실 예약 제안"""
    from agents import dreamplus as dreamplus_agent
    if not info.get("date") or not info.get("time"):
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="⚠️ 날짜 또는 시간을 파악하지 못했어요.\n예: '오늘 15시에 김민환 미팅 잡아줘'")
        return
    try:
        start_dt = datetime.fromisoformat(f"{info['date']}T{info['time']}:00+09:00")
    except ValueError:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 시간 형식을 파싱하지 못했어요. (date={info.get('date')}, time={info.get('time')})")
        return
    end_dt = start_dt + timedelta(minutes=int(info.get("duration_minutes", 60)))

    try:
        creds = user_store.get_credentials(user_id)
        event = cal.create_event(
            creds,
            summary=info.get("title", "미팅"),
            start_dt=start_dt,
            end_dt=end_dt,
            attendee_emails=attendee_emails,
            description=info.get("agenda", ""),
            location=info.get("location", ""),
        )
        event_id = event["id"]

        # Google Meet 트랜스크립트 자동 활성화
        conference_id = (event.get("conferenceData") or {}).get("conferenceId")
        if conference_id:
            cal.enable_meet_transcription(creds, conference_id)

        # I4: 생성 응답에 Google Meet 링크 포함 (hangoutLink 우선, 없으면 entryPoints에서 video 타입)
        meet_link = event.get("hangoutLink") or ""
        if not meet_link:
            for ep in (event.get("conferenceData") or {}).get("entryPoints", []) or []:
                if ep.get("entryPointType") == "video":
                    meet_link = ep.get("uri", "")
                    break

        time_str = format_time(event["start"]["dateTime"])
        attendee_display = ", ".join(attendee_emails) if attendee_emails else "없음"
        msg = f"✅ 미팅이 생성되었습니다.\n*{info.get('title', '미팅')}* — {time_str}\n참석자: {attendee_display}"
        if company:
            msg += f"\n업체: {company}"
        if meet_link:
            msg += f"\n🎥 *Google Meet*: <{meet_link}|회의 참여>"
        msg += "\n_이 메시지에 스레드 답글로 제목, 참석자, 어젠다를 알려주시면 업데이트해드릴게요._"
        msg += _hint("답글로 자연어 수정 가능 / [👥 참석자 추가] 버튼 / 시간이 지난 뒤엔 `/미팅편집`")
        result_blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": msg}},
            {
                "type": "actions",
                "elements": [{
                    "type": "button",
                    "text": {"type": "plain_text", "text": "👥 참석자 추가"},
                    "action_id": "add_attendee_to_event",
                    "value": event_id,
                }],
            },
        ]
        resp = _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                     text=msg, blocks=result_blocks)
        reply_ts = resp.get("ts") if resp else None
        # Slack이 반환하는 실제 thread_ts (스레드 내 답글이면 부모 ts)
        resp_thread_ts = (resp.get("message") or resp or {}).get("thread_ts")

        # 드래프트 저장 — 관련 thread_ts 모두 키로 등록
        draft_data = {
            "user_id": user_id,
            "info": dict(info),
            "company": company,
            "event_id": event_id,
            "attendee_emails": list(attendee_emails),
            "channel": channel,
            "reply_ts": reply_ts,
            "source": "create",
            "created_at": datetime.now().isoformat(),
        }
        all_ts_keys = {reply_ts, thread_ts, resp_thread_ts, user_msg_ts} - {None}
        for ts_key in all_ts_keys:
            _meeting_drafts[ts_key] = draft_data if ts_key == reply_ts else dict(draft_data)
        _save_meeting_drafts()

        if company:
            try:
                cal.update_event(creds, event_id,
                                 extended_properties={"private": {"company": company}})
            except Exception as e:
                log.warning(f"업체명 extendedProperties 저장 실패: {e}")

        # I2(b): 드림플러스 회의실 자동 추천 대신 "예약할까요?" 확인 먼저
        attendee_count = len(attendee_emails) + 1  # 참석자 + 주최자
        # 계정 미설정이면 offer 대신 기존 auto_book_room 내부 안내 경로로 흘려보냄 (dreamplus가 설정 링크 안내 처리)
        dp_creds = user_store.get_dreamplus_credentials(user_id)
        if dp_creds:
            offer_room_booking(
                slack_client, user_id=user_id,
                start_dt=start_dt, end_dt=end_dt,
                title=info.get("title", "미팅"),
                attendee_count=attendee_count,
                channel=channel, thread_ts=thread_ts,
                event_id=event_id,
            )
        else:
            threading.Thread(
                target=dreamplus_agent.auto_book_room,
                kwargs=dict(
                    slack_client=slack_client, user_id=user_id,
                    start_dt=start_dt, end_dt=end_dt,
                    title=info.get("title", "미팅"),
                    attendee_count=attendee_count,
                    channel=channel, thread_ts=thread_ts, event_id=event_id,
                ),
                daemon=True,
            ).start()

        return event_id

    except Exception as e:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 미팅 생성 실패: {e}")
        return None


def handle_email_selection(slack_client, body: dict):
    """select_attendee_email 버튼 클릭 → 기존 이벤트에 참석자 추가"""
    user_id = body["user"]["id"]
    value = body["actions"][0]["value"]
    pending = _pending_meetings.get(user_id)
    if not pending:
        _post(slack_client, user_id=user_id,
              text="⚠️ 선택 세션이 만료되었습니다 (서버 재시작 등).\n"
                   "스레드 답글로 참석자를 직접 추가해주세요. (예: '참석자 추가해줘 hoon@parametacorp.com')")
        return

    _, email = value.split("|", 1)
    added_emails = []
    if email != "__skip__":
        added_emails.append(email)
    pending["pending_selections"].pop(0)

    if pending["pending_selections"]:
        # 아직 선택이 남아있으면 다음 항목 표시
        # 먼저 이번에 선택한 이메일을 즉시 추가
        if added_emails:
            _add_attendees_to_event(slack_client, user_id, pending["event_id"], added_emails)
        try:
            _post_email_selection(slack_client, user_id,
                                  pending["pending_selections"][0],
                                  pending["channel"], pending["thread_ts"])
        except Exception as e:
            log.exception(f"이메일 선택 블록 발송 실패: {e}")
            del _pending_meetings[user_id]
    else:
        # 모든 선택 완료
        del _pending_meetings[user_id]
        if added_emails:
            _add_attendees_to_event(slack_client, user_id, pending["event_id"], added_emails)


def _add_attendees_to_event(slack_client, user_id: str, event_id: str, new_emails: list[str]):
    """기존 캘린더 이벤트에 참석자 추가"""
    try:
        creds = user_store.get_credentials(user_id)
        event = cal.get_event(creds, event_id)
        existing_emails = [a["email"].lower() for a in event.get("attendees", [])]
        all_emails = existing_emails + [e for e in new_emails if e.lower() not in existing_emails]
        cal.update_event(creds, event_id, attendee_emails=all_emails)
        # _meeting_drafts에도 반영
        for draft in _meeting_drafts.values():
            if draft.get("event_id") == event_id:
                draft["attendee_emails"] = list(all_emails)
        _save_meeting_drafts()
        _post(slack_client, user_id=user_id,
              text=f"✅ 참석자 추가 완료: {', '.join(new_emails)}")
    except Exception as e:
        log.exception(f"참석자 추가 실패: {e}")
        _post(slack_client, user_id=user_id,
              text=f"⚠️ 참석자 추가 실패: {e}\n스레드 답글로 직접 추가해주세요.")


# ── 참석자 추가 모달 (생성된 미팅에 사후 참석자 추가) ──────────


_ATTENDEE_ADD_MODAL_CALLBACK = "attendee_add_modal"


def open_attendee_add_modal(slack_client, *, trigger_id: str, user_id: str,
                              event_id: str, channel: str | None = None,
                              thread_ts: str | None = None) -> None:
    """미팅 생성 결과 메시지의 '👥 참석자 추가' 버튼 → 모달 오픈.

    이미 캘린더 이벤트가 만들어진 미팅에 나중에 참석자를 추가하는 경로.
    이메일 후보 검색 단계가 실패해 누락된 인원을 직접 입력으로 추가할 때 사용.
    """
    private_meta = json.dumps({
        "user_id": user_id,
        "event_id": event_id,
        "channel": channel,
        "thread_ts": thread_ts,
    }, ensure_ascii=False)

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "👥 *참석자 추가*\n이 회의에 한 명을 추가합니다. 이메일은 필수, 이름은 선택입니다.\n_캘린더 초대도 함께 발송됩니다._"},
        },
        {
            "type": "input",
            "block_id": "name_block",
            "optional": True,
            "label": {"type": "plain_text", "text": "이름 (선택)"},
            "element": {
                "type": "plain_text_input",
                "action_id": "name_input",
                "placeholder": {"type": "plain_text", "text": "예: 김은서"},
            },
        },
        {
            "type": "input",
            "block_id": "email_block",
            "label": {"type": "plain_text", "text": "이메일"},
            "element": {
                "type": "plain_text_input",
                "action_id": "email_input",
                "placeholder": {"type": "plain_text", "text": "예: eunseo@allobank.com"},
            },
        },
    ]
    slack_client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": _ATTENDEE_ADD_MODAL_CALLBACK,
            "title": {"type": "plain_text", "text": "참석자 추가"},
            "submit": {"type": "plain_text", "text": "추가"},
            "close": {"type": "plain_text", "text": "취소"},
            "private_metadata": private_meta,
            "blocks": blocks,
        },
    )


def handle_attendee_add_modal(slack_client, user_id: str, view: dict) -> None:
    """참석자 추가 모달 제출 — 캘린더 이벤트에 참석자 추가."""
    values = view.get("state", {}).get("values", {})
    name = ((values.get("name_block", {}).get("name_input", {}) or {}).get("value") or "").strip()
    email = ((values.get("email_block", {}).get("email_input", {}) or {}).get("value") or "").strip()

    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except Exception:
        meta = {}
    event_id = meta.get("event_id")
    channel = meta.get("channel") or user_id
    thread_ts = meta.get("thread_ts")

    # 입력 검증
    if not email or "@" not in email or "." not in email.split("@")[-1]:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="⚠️ 유효한 이메일 형식이 아닙니다.")
        return
    if not event_id:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="⚠️ 미팅 정보를 찾을 수 없습니다 (event_id 누락).")
        return

    try:
        creds = user_store.get_credentials(user_id)
    except Exception as e:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 인증 오류: {e}")
        return

    # 현재 참석자 목록 — draft 우선, 없으면 캘린더에서 직접 조회
    draft = next(
        (d for d in _meeting_drafts.values() if d.get("event_id") == event_id),
        None,
    )
    if draft:
        existing_emails = list(draft.get("attendee_emails") or [])
    else:
        try:
            event = cal.get_event(creds, event_id)
            existing_emails = [
                a.get("email", "") for a in event.get("attendees", []) if a.get("email")
            ]
        except Exception as e:
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text=f"⚠️ 캘린더 이벤트 조회 실패: {e}")
            return

    if email.lower() in [e.lower() for e in existing_emails]:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"ℹ️ `{email}` 은 이미 참석자에 포함되어 있습니다.")
        return

    new_emails = existing_emails + [email]

    try:
        cal.update_event(creds, event_id, attendee_emails=new_emails)
    except Exception as e:
        log.exception("참석자 추가 실패")
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 참석자 추가 실패: {e}")
        return

    # draft 동기화
    if draft is not None:
        draft["attendee_emails"] = new_emails
        if name:
            participants = list((draft.get("info") or {}).get("participants") or [])
            if name not in participants:
                participants.append(name)
                draft.setdefault("info", {})["participants"] = participants
        _save_meeting_drafts()

    label = f"*{name}* (`{email}`)" if name else f"`{email}`"
    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text=f"✅ 참석자 추가 완료: {label}\n_캘린더 초대 메일이 발송되었습니다._")


# ── 일정 드래프트 업데이트 ──────────────────────────────────────

def has_meeting_draft(thread_ts: str) -> bool:
    """해당 스레드에 일정 드래프트가 있는지 확인"""
    return thread_ts in _meeting_drafts if thread_ts else False


def update_meeting_from_text(slack_client, user_id: str, user_message: str,
                              channel: str = None, thread_ts: str = None) -> bool:
    """기존 드래프트에 새 메시지를 병합하여 캘린더 이벤트 업데이트.
    Returns: True if the message was handled as a meeting update, False otherwise.
    """
    draft = _meeting_drafts.get(thread_ts)
    if not draft:
        return False

    # 채널 응답은 항상 원래 스레드에 달기
    reply_channel = draft.get("channel") or channel
    reply_thread_ts = thread_ts

    # 확정된 업체명을 draft["info"]에 반영 (LLM이 기존 업체를 인식하도록)
    if draft.get("company"):
        existing_companies = [c.strip() for c in draft["company"].split(",") if c.strip()]
        draft["info"]["company_candidates"] = existing_companies
        draft["info"]["company_confirmed"] = True
    # 레거시 company 필드 제거 (LLM 스키마에 없어 혼동 유발)
    draft["info"].pop("company", None)

    # LLM으로 병합 판단
    try:
        raw = _generate(merge_meeting_prompt(draft["info"], user_message))
    except Exception as e:
        log.warning(f"merge_meeting_prompt 실패: {e}")
        return False

    cleaned = re.sub(r"```(?:json)?", "", raw).strip().strip("`")
    json_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not json_match:
        return False
    try:
        result = json.loads(json_match.group())
    except json.JSONDecodeError:
        return False

    if not result.get("is_update"):
        return False

    updated_info = result.get("updated_info", draft["info"])
    changed_fields = result.get("changed_fields", [])
    if not changed_fields:
        _post(slack_client, user_id=user_id, channel=reply_channel, thread_ts=reply_thread_ts,
              text="ℹ️ 변경할 정보를 찾지 못했습니다.")
        return True

    # 드래프트 정보 업데이트
    draft["info"] = updated_info
    _save_meeting_drafts()

    event_id = draft.get("event_id")
    creds = None
    try:
        creds = user_store.get_credentials(user_id)
    except Exception as e:
        _post(slack_client, user_id=user_id, channel=reply_channel, thread_ts=reply_thread_ts,
              text=f"⚠️ 인증 오류: {e}")
        return True

    # 캘린더 패치 인자 구성
    patch_kwargs: dict = {}
    attendee_emails = draft.get("attendee_emails", [])
    change_summary_lines = []

    if "title" in changed_fields:
        patch_kwargs["summary"] = updated_info.get("title", "미팅")
        change_summary_lines.append(f"제목 → *{patch_kwargs['summary']}*")

    if "agenda" in changed_fields:
        patch_kwargs["description"] = updated_info.get("agenda", "")
        change_summary_lines.append(f"어젠다 → _{patch_kwargs['description']}_")

    if "date" in changed_fields or "time" in changed_fields or "duration_minutes" in changed_fields:
        try:
            start_dt = datetime.fromisoformat(
                f"{updated_info['date']}T{updated_info['time']}:00+09:00"
            )
            end_dt = start_dt + timedelta(minutes=int(updated_info.get("duration_minutes", 60)))
            patch_kwargs["start_dt"] = start_dt
            patch_kwargs["end_dt"] = end_dt
            time_str = format_time(start_dt.isoformat())
            change_summary_lines.append(f"일시 → *{updated_info['date']} {time_str}*")
        except Exception as e:
            log.warning(f"날짜/시간 파싱 실패: {e}")

    if "location" in changed_fields:
        patch_kwargs["location"] = updated_info.get("location", "")
        change_summary_lines.append(f"장소 → *{patch_kwargs['location']}*")

    if "company" in changed_fields or "company_candidates" in changed_fields:
        new_candidates = updated_info.get("company_candidates", [])
        new_confirmed = updated_info.get("company_confirmed", False)
        # 하위 호환: company 필드 직접 반환 시
        if not new_candidates and updated_info.get("company"):
            raw_co = updated_info["company"]
            if isinstance(raw_co, str) and raw_co.lower() not in ("null", "none", ""):
                new_candidates = [raw_co]
                new_confirmed = True
        if new_candidates and new_confirmed:
            company = ", ".join(new_candidates)
            draft["company"] = company
            patch_kwargs["extended_properties"] = {"private": {"company": company}}
            change_summary_lines.append(f"업체 → *{company}*")
        elif new_candidates and not new_confirmed:
            # 기존 확정 업체와 새 후보를 합쳐서 확인 요청
            existing = [c.strip() for c in (draft.get("company") or "").split(",") if c.strip()]
            all_candidates = list(dict.fromkeys(existing + new_candidates))
            _post_company_confirmation(
                slack_client, user_id, all_candidates,
                event_id=event_id, channel=reply_channel, thread_ts=reply_thread_ts,
            )

    # 인라인 이메일 업데이트가 있으면 우선 처리 (사용자가 메시지로 직접 알려준 이메일)
    inline_emails = updated_info.get("participant_emails", {}) or {}
    if not isinstance(inline_emails, dict):
        inline_emails = {}

    if "participants" in changed_fields or "participant_emails" in changed_fields:
        new_names = updated_info.get("participants", []) or list(draft["info"].get("participants", []))
        # 인라인 이메일에 있는 이름은 participants에 누락되어 있어도 추가
        for n in inline_emails:
            if n and n not in new_names:
                new_names.append(n)

        existing_names = list(draft["info"].get("participants", []))
        new_emails = list(attendee_emails)  # 기존 유지 후 추가
        missing = []
        added_inline = []
        for name in new_names:
            if name in existing_names:
                # 기존 참석자에 인라인 이메일이 추가된 경우 → 이메일 보강
                if name in inline_emails:
                    em = inline_emails[name]
                    if em and em not in new_emails:
                        new_emails.append(em)
                        added_inline.append(f"{name}({em})")
                continue
            # 새 참석자 — 인라인 이메일 우선, 없으면 후보 검색
            if name in inline_emails and inline_emails[name]:
                em = inline_emails[name]
                if em not in new_emails:
                    new_emails.append(em)
                added_inline.append(f"{name}({em})")
            else:
                candidates = _find_email_candidates(user_id, name, slack_client)
                if candidates:
                    new_emails.append(candidates[0])
                else:
                    missing.append(name)

        draft["info"]["participants"] = new_names
        draft["attendee_emails"] = new_emails
        patch_kwargs["attendee_emails"] = new_emails
        names_str = ", ".join(new_names) if new_names else "없음"
        change_summary_lines.append(f"참석자 → {names_str}")
        if added_inline:
            change_summary_lines.append(f"  _(이메일 직접 등록: {', '.join(added_inline)})_")
        if missing:
            change_summary_lines.append(f"  _(이메일 미확인: {', '.join(missing)})_")

    if not patch_kwargs:
        _post(slack_client, user_id=user_id, channel=reply_channel, thread_ts=reply_thread_ts,
              text="ℹ️ 변경할 내용을 찾지 못했습니다.")
        return True

    # 캘린더 이벤트 업데이트
    if event_id and creds:
        try:
            cal.update_event(creds, event_id, **patch_kwargs)
        except Exception as e:
            log.error(f"캘린더 업데이트 실패: {e}")
            _post(slack_client, user_id=user_id, channel=reply_channel, thread_ts=reply_thread_ts,
                  text=f"⚠️ 일정 업데이트 실패: {e}")
            return True

    changes = "\n".join(f"• {line}" for line in change_summary_lines)
    _post(slack_client, user_id=user_id, channel=reply_channel, thread_ts=reply_thread_ts,
          text=f"✅ 일정이 업데이트되었습니다.\n{changes}")
    return True


# ── company_knowledge 갱신 ───────────────────────────────────

def update_company_knowledge(slack_client, user_id: str,
                             channel: str = None, thread_ts: str = None):
    """/업데이트 커맨드 처리"""
    try:
        creds, _, knowledge_file_id = _get_creds_and_config(user_id)
        current = drive.get_company_knowledge(creds, knowledge_file_id)
        new_content = _generate(update_knowledge_prompt(current))
        drive.update_company_knowledge(creds, knowledge_file_id, new_content)
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="✅ company_knowledge.md 갱신 완료")
    except Exception as e:
        err = str(e)
        msg = ("⚠️ AI API 할당량 초과. 잠시 후 다시 시도해주세요."
               if "429" in err or "RESOURCE_EXHAUSTED" in err else f"⚠️ 갱신 실패: {err[:200]}")
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts, text=msg)


# ═══════════════════════════════════════════════════════════════════
# F2: 일정 취소
# ═══════════════════════════════════════════════════════════════════

_CANCEL_PARSE_PROMPT = """다음 메시지에서 취소할 미팅을 찾기 위한 정보를 추출해줘.

메시지: "{text}"

오늘 날짜: {today}

JSON으로만 반환 (설명 없이):
{{"title_hint": "미팅 제목 키워드 (없으면 빈 문자열)", "date": "YYYY-MM-DD (언급 없으면 null)"}}"""


def cancel_meeting_from_text(slack_client, user_id: str, user_message: str,
                              channel: str = None, thread_ts: str = None):
    """자연어로 일정 취소 — 후보 조회 후 확인 버튼 발송"""
    try:
        creds = user_store.get_credentials(user_id)
    except Exception as e:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 인증 오류: {e}")
        return

    today = datetime.now(KST).strftime("%Y-%m-%d")
    title_hint = ""
    date = None
    try:
        raw = _generate(_CANCEL_PARSE_PROMPT.format(
            text=user_message.replace('"', "'"), today=today,
        ))
        cleaned = re.sub(r"```(?:json)?", "", raw).strip().strip("`")
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            info = json.loads(m.group())
            title_hint = (info.get("title_hint") or "").strip().lower()
            if info.get("date") and info["date"] != "null":
                date = info["date"]
    except Exception as e:
        log.warning(f"취소 파싱 실패 (무시): {e}")

    # 후보 조회 — date가 있으면 당일, 없으면 향후 2주
    try:
        if date:
            events = cal.get_upcoming_meetings(creds, start_date=date, end_date=date)
        else:
            events = cal.get_upcoming_meetings(creds, days=14, from_now=True)
    except Exception as e:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"❌ 캘린더 조회 실패: {e}")
        return

    if title_hint:
        events = [ev for ev in events
                  if title_hint in (ev.get("summary", "") or "").lower()]

    if not events:
        hint = f" ({date})" if date else " (향후 2주 내)"
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 취소할 미팅을 찾지 못했어요.{hint}")
        return

    if len(events) == 1:
        _post_cancel_confirm(slack_client, user_id, events[0], channel, thread_ts)
    else:
        _post_cancel_select(slack_client, user_id, events[:5], channel, thread_ts)


def _post_cancel_confirm(slack_client, user_id, event, channel, thread_ts):
    summary = event.get("summary", "(제목 없음)")
    start_str = event.get("start", {}).get("dateTime", "")
    try:
        time_str = format_time(start_str)
    except Exception:
        time_str = start_str
    event_id = event["id"]
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"🗑 이 미팅을 취소할까요?\n*{summary}* — {time_str}"}},
        {"type": "actions", "elements": [
            {"type": "button", "style": "danger",
                "text": {"type": "plain_text", "text": "취소 확정"},
                "action_id": "meeting_cancel_confirm", "value": event_id},
            {"type": "button",
                "text": {"type": "plain_text", "text": "유지"},
                "action_id": "meeting_cancel_abort", "value": event_id},
        ]},
    ]
    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text=f"이 미팅을 취소할까요? {summary}", blocks=blocks)


def _post_cancel_select(slack_client, user_id, events, channel, thread_ts):
    elements = []
    for i, ev in enumerate(events):
        summary = (ev.get("summary", "(제목 없음)") or "")[:35]
        start_str = ev.get("start", {}).get("dateTime", "")
        try:
            time_str = format_time(start_str)
        except Exception:
            time_str = start_str
        # Slack은 한 메시지 내 action_id 중복을 허용하지 않음 → 인덱스 접미사
        elements.append({
            "type": "button", "style": "danger",
            "text": {"type": "plain_text", "text": f"🗑 {summary} {time_str}"[:75]},
            "action_id": f"meeting_cancel_confirm_{i}",
            "value": ev["id"],
        })
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"여러 미팅이 조건과 일치합니다 ({len(events)}건). 취소할 미팅을 선택해주세요."}},
        {"type": "actions", "elements": elements},
    ]
    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text="취소할 미팅을 선택해주세요", blocks=blocks)


def handle_meeting_cancel_confirm(slack_client, user_id: str, event_id: str,
                                   body: dict = None):
    """취소 확정 버튼 콜백 — 드림플러스 예약 연동 여부 확인 후 분기.
    예약 없음 → 일정만 삭제 (기존 흐름).
    예약 있음 → '함께 취소할까요?' 추가 프롬프트."""
    try:
        creds = user_store.get_credentials(user_id)
    except Exception as e:
        _post(slack_client, user_id=user_id, text=f"❌ 인증 오류: {e}")
        return

    # 이벤트 조회
    try:
        ev = cal.get_event(creds, event_id)
        summary = ev.get("summary", "미팅")
        start_str = ev.get("start", {}).get("dateTime", "")
        end_str = ev.get("end", {}).get("dateTime", "")
        location = ev.get("location", "") or ""
    except Exception as e:
        log.warning(f"이벤트 조회 실패: {e}")
        # 조회 실패 시에도 삭제 시도 (기존 흐름 유지)
        _perform_meeting_cancel(slack_client, user_id, event_id,
                                 summary="미팅",
                                 cancel_reservation_id=None, body=body)
        return

    # 드림플러스 회의실 연동 여부 탐색 (location에 '드림플러스' 포함 시에만)
    reservation_id = None
    if "드림플러스" in location and start_str and end_str:
        try:
            start_dt = datetime.fromisoformat(start_str)
            end_dt = datetime.fromisoformat(end_str)
            from agents import dreamplus as dreamplus_agent
            reservation_id = dreamplus_agent.find_reservation_for_meeting(
                user_id, start_dt, end_dt
            )
        except Exception as e:
            log.warning(f"드림플러스 예약 조회 실패 (무시): {e}")

    if reservation_id:
        # 연동 프롬프트
        _pending_meeting_cancel_with_room[event_id] = {
            "user_id": user_id,
            "event_id": event_id,
            "reservation_id": reservation_id,
            "summary": summary,
            "location": location,
        }
        _replace_block(
            slack_client, body,
            f"🔍 *{summary}* — 드림플러스 회의실 예약을 확인했습니다. 함께 취소할지 선택해주세요 👇",
        )
        _post_cancel_with_room_prompt(slack_client, user_id, event_id,
                                       summary=summary, location=location)
        return

    # 연동 없음 → 기존대로 일정만 삭제
    _perform_meeting_cancel(slack_client, user_id, event_id,
                             summary=summary,
                             cancel_reservation_id=None, body=body)


def _post_cancel_with_room_prompt(slack_client, user_id: str, event_id: str,
                                    *, summary: str, location: str):
    """일정 + 회의실 함께 취소 확인 블록."""
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": (f"🏢 *{summary}* 에 드림플러스 회의실 예약이 있습니다.\n"
                     f"_{location}_\n"
                     f"함께 취소할까요?")}},
        {"type": "actions", "elements": [
            {"type": "button", "style": "danger",
                "text": {"type": "plain_text", "text": "일정 + 회의실 함께 취소"},
                "action_id": "meeting_cancel_with_room", "value": event_id},
            {"type": "button",
                "text": {"type": "plain_text", "text": "일정만 취소"},
                "action_id": "meeting_cancel_event_only", "value": event_id},
            {"type": "button",
                "text": {"type": "plain_text", "text": "유지"},
                "action_id": "meeting_cancel_abort_both", "value": event_id},
        ]},
    ]
    _post(slack_client, user_id=user_id,
          text="회의실 예약도 함께 취소할까요?", blocks=blocks)


def handle_meeting_cancel_with_room(slack_client, user_id: str, event_id: str,
                                      body: dict = None):
    """일정 + 회의실 함께 취소"""
    payload = _pending_meeting_cancel_with_room.pop(event_id, None)
    if not payload:
        _post(slack_client, user_id=user_id, text="⚠️ 만료된 취소 요청입니다.")
        return
    _perform_meeting_cancel(
        slack_client, user_id, event_id,
        summary=payload["summary"],
        cancel_reservation_id=payload["reservation_id"],
        body=body,
    )


def handle_meeting_cancel_event_only(slack_client, user_id: str, event_id: str,
                                       body: dict = None):
    """일정만 취소 (회의실 예약은 유지)"""
    payload = _pending_meeting_cancel_with_room.pop(event_id, None)
    if not payload:
        _post(slack_client, user_id=user_id, text="⚠️ 만료된 취소 요청입니다.")
        return
    _perform_meeting_cancel(
        slack_client, user_id, event_id,
        summary=payload["summary"],
        cancel_reservation_id=None,
        body=body,
    )


def handle_meeting_cancel_abort_both(slack_client, user_id: str, event_id: str,
                                       body: dict = None):
    """유지 — 일정·회의실 모두 취소하지 않음"""
    _pending_meeting_cancel_with_room.pop(event_id, None)
    _replace_block(slack_client, body, "❌ 일정 취소를 취소했습니다. (유지)")


def _perform_meeting_cancel(slack_client, user_id: str, event_id: str, *,
                             summary: str, cancel_reservation_id: int | None,
                             body: dict | None):
    """실제 cal.delete_event + (옵션) 드림플러스 예약 취소 실행."""
    try:
        creds = user_store.get_credentials(user_id)
        cal.delete_event(creds, event_id)
    except Exception as e:
        log.exception(f"일정 취소 실패: {e}")
        _post(slack_client, user_id=user_id, text=f"❌ 일정 취소 실패: {e}")
        return

    reservation_note = ""
    if cancel_reservation_id:
        try:
            from agents import dreamplus as dreamplus_agent
            dreamplus_agent.cancel_reservation_by_id(user_id, cancel_reservation_id)
            reservation_note = "\n🏢 드림플러스 회의실 예약도 함께 취소되었습니다."
        except Exception as e:
            log.warning(f"드림플러스 예약 취소 실패: {e}")
            reservation_note = (
                f"\n⚠️ 드림플러스 회의실 예약 취소는 실패했습니다: {e}\n"
                f"`/회의실취소` 로 수동 취소해주세요."
            )

    # 원본 버튼 메시지 교체
    _replace_block(slack_client, body, f"🗑 *{summary}* 취소 완료")
    _post(slack_client, user_id=user_id,
          text=(f"✅ *{summary}* 일정을 취소했습니다. 참석자에게 취소 알림이 자동 발송됩니다."
                f"{reservation_note}"))


def handle_meeting_cancel_abort(slack_client, user_id: str, event_id: str,
                                 body: dict = None):
    """유지 버튼 콜백 — 원본 메시지만 업데이트"""
    _replace_block(slack_client, body, "❌ 일정 취소를 취소했습니다. (유지)")


def _replace_block(slack_client, body: dict, text: str):
    """버튼 클릭 body에서 원본 메시지를 단일 섹션 텍스트로 교체."""
    if not body:
        return
    container = body.get("container", {}) or {}
    ch = container.get("channel_id")
    ts = container.get("message_ts")
    if not (ch and ts):
        return
    try:
        slack_client.chat_update(
            channel=ch, ts=ts, text=text,
            blocks=[{"type": "section",
                     "text": {"type": "mrkdwn", "text": text}}],
        )
    except Exception as e:
        log.warning(f"블록 교체 실패: {e}")


# ═══════════════════════════════════════════════════════════════════
# F1: FreeBusy 기반 최적 시간대 제안
# ═══════════════════════════════════════════════════════════════════

_SLOT_PARSE_PROMPT = """다음 메시지에서 공통 빈 시간대 추천에 필요한 정보를 추출해줘.

메시지: "{text}"

오늘 날짜: {today} (요일: {weekday})

JSON으로만 반환 (설명 없이):
{{
  "participants": ["이름1", "이름2"],
  "duration_minutes": 60,
  "range_start": "YYYY-MM-DD",
  "range_end": "YYYY-MM-DD"
}}

규칙:
- participants: 참석자 이름 (사용자 본인 제외)
- duration_minutes: 언급 없으면 60
- range_start/end: 기간 언급 없으면 today부터 today+7
- "다음주" = 다음주 월~금, "이번주" = 오늘~토"""


def suggest_meeting_slots(slack_client, user_id: str, user_message: str,
                           channel: str = None, thread_ts: str = None):
    """참석자 캘린더 FreeBusy 조회 → 공통 빈 시간대 제안"""
    try:
        creds = user_store.get_credentials(user_id)
    except Exception as e:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 인증 오류: {e}")
        return

    today = datetime.now(KST).strftime("%Y-%m-%d")
    weekday = ["월", "화", "수", "목", "금", "토", "일"][datetime.now(KST).weekday()]
    info = {}
    try:
        raw = _generate(_SLOT_PARSE_PROMPT.format(
            text=user_message.replace('"', "'"), today=today, weekday=weekday,
        ))
        cleaned = re.sub(r"```(?:json)?", "", raw).strip().strip("`")
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            info = json.loads(m.group())
    except Exception as e:
        log.warning(f"슬롯 파싱 실패: {e}")

    participants = info.get("participants") or []
    duration = int(info.get("duration_minutes") or 60)
    range_start = info.get("range_start") or today
    range_end = info.get("range_end")
    if not range_end:
        try:
            range_end = (datetime.fromisoformat(range_start).date()
                         + timedelta(days=7)).isoformat()
        except Exception:
            range_end = today

    if not participants:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=("⚠️ 누구 일정을 확인할지 알려주세요.\n"
                    "예: `김민환, 홍길동이랑 다음주에 1시간 미팅 잡을 시간 찾아줘`"))
        return

    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text=f"🔎 *{', '.join(participants)}*의 일정을 확인하고 있어요...")

    # 이메일 해석
    emails: list[str] = []
    missing: list[str] = []
    for name in participants:
        cands = _find_email_candidates(user_id, name, slack_client)
        if cands:
            emails.append(cands[0])
        else:
            missing.append(name)
    creator = _lookup_slack_email(slack_client, user_id)
    if creator and creator.lower() not in {e.lower() for e in emails}:
        emails.insert(0, creator)

    if missing:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 이메일을 찾지 못한 참석자: {', '.join(missing)}. 나머지만으로 검색합니다.")
    if not emails:
        return

    # FreeBusy 조회
    try:
        time_min = datetime.fromisoformat(f"{range_start}T00:00:00+09:00")
        time_max = datetime.fromisoformat(f"{range_end}T23:59:59+09:00")
    except Exception as e:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 기간 파싱 실패: {e}")
        return

    try:
        fb = cal.freebusy_query(creds, emails, time_min, time_max)
    except Exception as e:
        log.exception(f"freebusy 조회 실패: {e}")
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"❌ FreeBusy 조회 실패: {e}")
        return

    errors = fb.pop("errors", [])
    if errors:
        err_emails = sorted({e["email"] for e in errors})
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=(f"⚠️ 권한 없는 이메일: {', '.join(err_emails)}. "
                    f"해당 인원의 바쁜 시간은 반영되지 않습니다."))

    all_busy: list[tuple] = []
    for em, busy in fb.items():
        all_busy.extend(busy)

    candidates = _find_free_slots(time_min, time_max, all_busy, duration,
                                   preferred_hours=list(range(9, 18)),
                                   max_results=5)
    if not candidates:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="⚠️ 공통 빈 시간대를 찾지 못했어요. 기간을 늘려 다시 시도해주세요.")
        return

    weekday_kr = ["월", "화", "수", "목", "금", "토", "일"]
    elements = []
    for i, (s, e) in enumerate(candidates):
        wd = weekday_kr[s.weekday()]
        label = f"{s.strftime('%m/%d')} ({wd}) {s.strftime('%H:%M')}~{e.strftime('%H:%M')}"
        # Slack은 한 메시지 내 action_id 중복을 허용하지 않음 → 인덱스 접미사
        elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": label[:75]},
            "action_id": f"slot_create_meeting_{i}",
            "value": f"{s.isoformat()}|{e.isoformat()}|{','.join(emails)}",
        })
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": (f"📅 *{', '.join(participants)}* 와의 공통 빈 시간 ({duration}분)\n"
                     f"시간대를 선택하면 해당 시간으로 미팅을 생성합니다.")}},
        {"type": "actions", "elements": elements},
    ]
    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text="공통 빈 시간 추천", blocks=blocks)


def _find_free_slots(time_min, time_max, busy: list[tuple], duration_min: int,
                     preferred_hours: list[int], max_results: int = 5,
                     step_min: int = 30) -> list[tuple]:
    """공통 빈 시간대 계산. busy: 합친 바쁨 시간대 리스트."""
    # 병합·정렬
    busy_sorted = sorted([(s, e) for s, e in busy], key=lambda x: x[0])
    merged = []
    for s, e in busy_sorted:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    results = []
    cur = time_min
    # 첫 preferred_hour로 정렬
    if cur.hour not in preferred_hours:
        valid = [h for h in preferred_hours if h > cur.hour]
        if valid:
            cur = cur.replace(hour=valid[0], minute=0, second=0, microsecond=0)
        else:
            cur = (cur.replace(hour=preferred_hours[0], minute=0, second=0,
                               microsecond=0) + timedelta(days=1))

    loop_limit = 1000  # 안전장치
    while cur < time_max and len(results) < max_results and loop_limit > 0:
        loop_limit -= 1
        # 주말 skip
        if cur.weekday() >= 5:
            cur = cur.replace(hour=preferred_hours[0], minute=0) + timedelta(days=1)
            continue
        # 선호 시간대 밖이면 점프
        if cur.hour not in preferred_hours:
            valid = [h for h in preferred_hours if h > cur.hour]
            if valid:
                cur = cur.replace(hour=valid[0], minute=0)
            else:
                cur = cur.replace(hour=preferred_hours[0], minute=0) + timedelta(days=1)
            continue

        slot_start = cur
        slot_end = cur + timedelta(minutes=duration_min)
        # slot_end의 종료 시각이 마지막 preferred_hour+1 밖이면 skip
        last_pref = max(preferred_hours) + 1
        if slot_end.hour > last_pref or (slot_end.hour == last_pref and slot_end.minute > 0):
            cur = cur.replace(hour=preferred_hours[0], minute=0) + timedelta(days=1)
            continue

        # 충돌 검사
        conflict_until = None
        for bs, be in merged:
            if slot_start < be and bs < slot_end:
                conflict_until = be
                break
        if conflict_until:
            cur = conflict_until
            # step_min 경계로 올림
            mins_to_next = (step_min - cur.minute % step_min) % step_min
            if mins_to_next:
                cur = cur + timedelta(minutes=mins_to_next)
        else:
            results.append((slot_start, slot_end))
            cur = slot_start + timedelta(minutes=step_min)

    return results


def handle_slot_create_meeting(slack_client, user_id: str, slot_value: str,
                                body: dict = None):
    """슬롯 버튼 콜백 — 선택한 시간으로 실제 미팅 생성"""
    try:
        parts = slot_value.split("|", 2)
        start_dt = datetime.fromisoformat(parts[0])
        end_dt = datetime.fromisoformat(parts[1])
        emails = [em for em in (parts[2] if len(parts) > 2 else "").split(",") if em]
    except Exception as e:
        _post(slack_client, user_id=user_id, text=f"⚠️ 슬롯 파싱 실패: {e}")
        return

    try:
        creds = user_store.get_credentials(user_id)
        event = cal.create_event(
            creds, summary="신규 미팅 (슬롯 추천)",
            start_dt=start_dt, end_dt=end_dt,
            attendee_emails=emails,
            description="",
        )
        meet_link = event.get("hangoutLink") or ""
        msg = (f"✅ 미팅이 생성되었습니다: "
               f"{start_dt.strftime('%m/%d %H:%M')}~{end_dt.strftime('%H:%M')}\n"
               f"참석자: {', '.join(emails) if emails else '없음'}")
        if meet_link:
            msg += f"\n🎥 Google Meet: <{meet_link}|회의 참여>"
        msg += "\n_스레드 답글로 제목·어젠다 등을 알려주시면 업데이트해드려요._"
        _post(slack_client, user_id=user_id, text=msg)
        _replace_block(slack_client, body,
                       f"✅ 선택됨: {start_dt.strftime('%m/%d %H:%M')}~{end_dt.strftime('%H:%M')}")
    except Exception as e:
        log.exception(f"슬롯 → 미팅 생성 실패: {e}")
        _post(slack_client, user_id=user_id, text=f"❌ 미팅 생성 실패: {e}")


# ═══════════════════════════════════════════════════════════════════
# I2(b): 드림플러스 회의실 자동 추천 전 예약 여부 확인
# ═══════════════════════════════════════════════════════════════════

def offer_room_booking(slack_client, *, user_id: str, start_dt: datetime,
                        end_dt: datetime, title: str, attendee_count: int,
                        channel: str = None, thread_ts: str = None,
                        event_id: str = None):
    """_create_calendar_event가 직접 auto_book_room을 호출하는 대신, 먼저 사용자에게
    '회의실을 예약할까요?'를 묻고 동의 시 auto_book_room 실행."""
    offer_id = f"{user_id}:{int(start_dt.timestamp())}"
    _pending_room_offer[offer_id] = {
        "user_id": user_id,
        "start_dt_iso": start_dt.isoformat(),
        "end_dt_iso": end_dt.isoformat(),
        "title": title,
        "attendee_count": attendee_count,
        "channel": channel,
        "thread_ts": thread_ts,
        "event_id": event_id,
    }
    date_str = start_dt.strftime("%m/%d")
    time_str = f"{start_dt.strftime('%H:%M')}~{end_dt.strftime('%H:%M')}"
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": (f"🏢 *{title}* — 드림플러스 회의실을 예약할까요?\n"
                     f"{date_str} {time_str} · {attendee_count}인 기준")}},
        {"type": "actions", "elements": [
            {"type": "button", "style": "primary",
                "text": {"type": "plain_text", "text": "🏢 추천 보기"},
                "action_id": "room_offer_show", "value": offer_id},
            {"type": "button",
                "text": {"type": "plain_text", "text": "건너뛰기"},
                "action_id": "room_offer_skip", "value": offer_id},
        ]},
    ]
    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text="드림플러스 회의실을 예약할까요?", blocks=blocks)


def handle_room_offer_show(slack_client, user_id: str, offer_id: str,
                            body: dict = None):
    """추천 보기 → auto_book_room 실제 호출"""
    payload = _pending_room_offer.pop(offer_id, None)
    if not payload:
        _post(slack_client, user_id=user_id, text="⚠️ 만료된 요청입니다.")
        return
    _replace_block(slack_client, body,
                   f"🏢 *{payload['title']}* — 회의실 추천을 조회합니다...")
    from agents import dreamplus as dreamplus_agent
    threading.Thread(
        target=dreamplus_agent.auto_book_room,
        kwargs=dict(
            slack_client=slack_client,
            user_id=user_id,
            start_dt=datetime.fromisoformat(payload["start_dt_iso"]),
            end_dt=datetime.fromisoformat(payload["end_dt_iso"]),
            title=payload["title"],
            attendee_count=payload["attendee_count"],
            channel=payload.get("channel"),
            thread_ts=payload.get("thread_ts"),
            event_id=payload.get("event_id"),
        ),
        daemon=True,
    ).start()


def handle_room_offer_skip(slack_client, user_id: str, offer_id: str,
                            body: dict = None):
    _pending_room_offer.pop(offer_id, None)
    _replace_block(slack_client, body, "⏭ 회의실 예약을 건너뛰었습니다.")


# ═══════════════════════════════════════════════════════════════════
# 미팅 소환·편집 (이미 생성된 캘린더 미팅을 다시 드래프트화)
# ═══════════════════════════════════════════════════════════════════

_MEETING_EDIT_MODAL_CALLBACK = "meeting_edit_modal"


def _build_info_from_event(event: dict, company: str | None = None) -> dict:
    """캘린더 이벤트 원본에서 _meeting_drafts info 스키마로 변환.

    cal.get_event() 가 반환하는 raw 이벤트 (attendees self 포함)를 받아
    create 흐름과 동일한 info dict 를 만든다.
    """
    summary = event.get("summary", "(제목 없음)")
    start = event.get("start", {}) or {}
    end = event.get("end", {}) or {}
    start_str = start.get("dateTime") or start.get("date") or ""
    end_str = end.get("dateTime") or end.get("date") or ""

    date_str, time_str = "", ""
    duration = 60
    try:
        if "T" in start_str:
            sdt = datetime.fromisoformat(start_str)
            date_str = sdt.strftime("%Y-%m-%d")
            time_str = sdt.strftime("%H:%M")
    except Exception:
        pass
    try:
        if start_str and end_str and "T" in start_str and "T" in end_str:
            duration = int(
                (datetime.fromisoformat(end_str) - datetime.fromisoformat(start_str)).total_seconds() / 60
            )
            if duration <= 0:
                duration = 60
    except Exception:
        duration = 60

    # self(주최자) 제외
    raw_attendees = [a for a in (event.get("attendees") or []) if not a.get("self")]
    participants = [a.get("displayName", "") for a in raw_attendees if a.get("displayName")]
    participant_emails = {
        a["displayName"]: a["email"]
        for a in raw_attendees
        if a.get("displayName") and a.get("email")
    }

    return {
        "title": summary,
        "date": date_str,
        "time": time_str,
        "duration_minutes": duration,
        "participants": participants,
        "participant_emails": participant_emails,
        "company_candidates": [company] if company else [],
        "company_confirmed": bool(company),
        "agenda": event.get("description", "") or "",
        "location": event.get("location", "") or "",
    }


def _build_summoned_message(info: dict, company: str | None,
                             event_id: str, meet_link: str) -> tuple[str, list[dict]]:
    """소환된 드래프트 메시지의 텍스트 + Block Kit 구성.

    create 흐름의 결과 메시지와 동일한 모양을 유지하되, 헤더 문구만 '편집' 으로 바꾼다.
    """
    title = info.get("title") or "미팅"
    date_str = info.get("date") or ""
    time_str = info.get("time") or ""
    when_line = f"{date_str} {time_str}".strip() or "(시간 미정)"
    attendee_emails = list(info.get("participant_emails", {}).values())
    # participant_emails 에 누락된 이메일이 있을 수 있으니 별도 인자 필요 — 호출자가 보강
    attendee_display = ", ".join(attendee_emails) if attendee_emails else "(없음)"

    msg_lines = [
        f"📌 미팅 편집 — *{title}*",
        f"⏰ {when_line}",
        f"👥 참석자: {attendee_display}",
    ]
    if company:
        msg_lines.append(f"🏢 업체: {company}")
    if info.get("location"):
        msg_lines.append(f"📍 장소: {info['location']}")
    if info.get("agenda"):
        msg_lines.append(f"📝 어젠다: {info['agenda']}")
    if meet_link:
        msg_lines.append(f"🎥 *Google Meet*: <{meet_link}|회의 참여>")
    msg_lines.append("_이 메시지에 답글로 변경 사항을 알려주세요. 또는 아래 버튼을 사용하세요._")
    msg = "\n".join(msg_lines)

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": msg}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "👥 참석자 추가"},
                    "action_id": "add_attendee_to_event",
                    "value": event_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📅 일정 편집"},
                    "action_id": "edit_meeting_schedule",
                    "value": event_id,
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ 미팅 취소"},
                    "action_id": "cancel_meeting_button",
                    "value": event_id,
                    "style": "danger",
                },
            ],
        },
    ]
    return msg, blocks


def summon_meeting_draft(slack_client, user_id: str, event_id: str,
                          channel: str | None = None,
                          thread_ts: str | None = None) -> str | None:
    """이미 생성된 캘린더 이벤트를 다시 드래프트로 소환.

    캘린더에서 이벤트를 가져와 create 흐름과 동일한 모양의 메시지를 발송하고,
    해당 메시지 ts 를 _meeting_drafts 에 등록하여 스레드 답글·버튼이 모두
    기존 인프라를 재사용하도록 한다.

    Returns: 등록된 reply_ts (실패 시 None)
    """
    try:
        creds = user_store.get_credentials(user_id)
    except Exception as e:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 인증 오류: {e}")
        return None

    try:
        event = cal.get_event(creds, event_id)
    except Exception as e:
        log.exception(f"미팅 소환 실패 — get_event: {e}")
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 미팅 정보를 불러오지 못했습니다: {e}")
        return None

    # 업체명 — extendedProperties.private.company
    company = None
    ext = (event.get("extendedProperties") or {}).get("private") or {}
    if ext.get("company"):
        company = ext["company"]

    info = _build_info_from_event(event, company)

    # self 제외 참석자 이메일
    raw_attendees = [a for a in (event.get("attendees") or []) if not a.get("self")]
    attendee_emails = [a.get("email", "") for a in raw_attendees if a.get("email")]

    # Google Meet 링크
    meet_link = event.get("hangoutLink") or ""
    if not meet_link:
        for ep in (event.get("conferenceData") or {}).get("entryPoints", []) or []:
            if ep.get("entryPointType") == "video":
                meet_link = ep.get("uri", "")
                break

    msg, blocks = _build_summoned_message(info, company, event_id, meet_link)
    resp = _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                 text=msg, blocks=blocks)
    reply_ts = resp.get("ts") if resp else None
    if not reply_ts:
        log.warning("summon_meeting_draft: 메시지 발송 실패 — ts 없음")
        return None

    # Slack 응답에 thread_ts 가 들어 있으면 그것도 키로 등록 (스레드 답글 대응)
    resp_thread_ts = (resp.get("message") or resp or {}).get("thread_ts")

    draft_data = {
        "user_id": user_id,
        "info": dict(info),
        "company": company,
        "event_id": event_id,
        "attendee_emails": list(attendee_emails),
        "channel": channel,
        "reply_ts": reply_ts,
        "source": "summon",
        "created_at": datetime.now().isoformat(),
    }
    all_ts_keys = {reply_ts, thread_ts, resp_thread_ts} - {None}
    with _drafts_lock:
        for ts_key in all_ts_keys:
            _meeting_drafts[ts_key] = draft_data if ts_key == reply_ts else dict(draft_data)
        _save_meeting_drafts()

    log.info(f"미팅 소환 완료: event_id={event_id} reply_ts={reply_ts} keys={list(all_ts_keys)}")
    return reply_ts


def list_upcoming_meetings_for_edit(slack_client, user_id: str,
                                     days: int = 7,
                                     keyword: str | None = None,
                                     channel: str | None = None,
                                     thread_ts: str | None = None) -> None:
    """편집할 미팅 목록 UI 발송.

    days 일 이내의 향후 미팅을 조회해서 각 미팅을 '✏️ 편집' 버튼과 함께 나열한다.
    keyword 가 주어지면 제목에 포함된 미팅만 필터.
    """
    try:
        creds = user_store.get_credentials(user_id)
    except Exception as e:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 인증 오류: {e}")
        return

    try:
        events = cal.get_upcoming_meetings(creds, days=days, from_now=True)
    except Exception as e:
        log.exception("미팅 목록 조회 실패")
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"❌ 캘린더 조회 실패: {e}")
        return

    # 종일 + 집/사무실 등은 cal 단계에서 이미 제외되지만 한 번 더 안전 처리
    _LOCATION_TITLES = {"집", "사무실"}
    filtered: list[dict] = []
    kw = (keyword or "").strip().lower()
    for ev in events:
        summary = (ev.get("summary") or "").strip()
        if not summary:
            continue
        if (ev.get("start", {}).get("dateTime") is None
                and summary in _LOCATION_TITLES):
            continue
        if kw and kw not in summary.lower():
            continue
        filtered.append(ev)

    if not filtered:
        hint = f" (키워드: {keyword})" if keyword else f" (향후 {days}일)"
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"📭 편집할 수 있는 미팅을 찾지 못했어요.{hint}")
        return

    capped = filtered[:25]
    capped_note = ""
    if len(filtered) > 25:
        capped_note = f"\n_({len(filtered)}건 중 최대 25건만 표시)_"

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"✏️ *편집할 미팅을 선택해주세요* ({len(capped)}건){capped_note}"},
        },
    ]
    for ev in capped:
        summary = ev.get("summary", "(제목 없음)")
        start_str = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
        try:
            time_str = format_time(start_str)
        except Exception:
            time_str = start_str
        ext = (ev.get("extendedProperties") or {}).get("private") or {}
        company = ext.get("company") or ""
        company_tag = f" · 🏢 {company}" if company else ""
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"*{summary}*\n⏰ {time_str}{company_tag}"},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "✏️ 편집"},
                "action_id": "summon_meeting_for_edit",
                "value": ev["id"],
            },
        })

    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text="편집할 미팅 목록", blocks=blocks)


# ── 일정 편집 모달 (날짜·시간·장소·어젠다·제목·소요시간) ──────────


def open_meeting_edit_modal(slack_client, *, trigger_id: str, user_id: str,
                              event_id: str, channel: str | None = None,
                              thread_ts: str | None = None) -> None:
    """소환된 미팅 메시지의 '📅 일정 편집' 버튼 → 모달 오픈.

    Pre-fill 우선순위: 기존 _meeting_drafts info → 캘린더 원본 이벤트.
    """
    # 기존 드래프트 우선 조회 (event_id 매칭)
    draft = next(
        (d for d in _meeting_drafts.values() if d.get("event_id") == event_id),
        None,
    )
    info: dict
    if draft and draft.get("info"):
        info = dict(draft["info"])
    else:
        try:
            creds = user_store.get_credentials(user_id)
            event = cal.get_event(creds, event_id)
            ext = (event.get("extendedProperties") or {}).get("private") or {}
            info = _build_info_from_event(event, ext.get("company"))
        except Exception as e:
            log.warning(f"open_meeting_edit_modal: info 로드 실패: {e}")
            info = {"title": "", "date": "", "time": "",
                    "duration_minutes": 60, "location": "", "agenda": ""}

    private_meta = json.dumps({
        "user_id": user_id,
        "event_id": event_id,
        "channel": channel,
        "thread_ts": thread_ts,
    }, ensure_ascii=False)

    def _input(block_id: str, label: str, action_id: str, *,
               initial: str = "", optional: bool = False,
               multiline: bool = False, placeholder: str = "") -> dict:
        elem = {
            "type": "plain_text_input",
            "action_id": action_id,
        }
        if initial:
            elem["initial_value"] = initial
        if multiline:
            elem["multiline"] = True
        if placeholder:
            elem["placeholder"] = {"type": "plain_text", "text": placeholder}
        block = {
            "type": "input",
            "block_id": block_id,
            "label": {"type": "plain_text", "text": label},
            "element": elem,
        }
        if optional:
            block["optional"] = True
        return block

    duration_initial = str(int(info.get("duration_minutes") or 60))

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "📅 *미팅 일정 편집*\n변경할 항목만 수정 후 저장하세요. 캘린더 초대도 함께 갱신됩니다."},
        },
        _input("title_block", "제목", "title_input",
               initial=info.get("title") or "",
               placeholder="예: 카카오 사전논의"),
        _input("date_block", "날짜 (YYYY-MM-DD)", "date_input",
               initial=info.get("date") or "",
               placeholder="예: 2026-05-01"),
        _input("time_block", "시간 (HH:MM, 24시간)", "time_input",
               initial=info.get("time") or "",
               placeholder="예: 15:00"),
        _input("duration_block", "소요시간 (분)", "duration_input",
               initial=duration_initial,
               placeholder="예: 60"),
        _input("location_block", "장소", "location_input",
               initial=info.get("location") or "",
               optional=True, placeholder="예: 드림플러스 8층 6인실"),
        _input("agenda_block", "어젠다", "agenda_input",
               initial=info.get("agenda") or "",
               optional=True, multiline=True,
               placeholder="회의 어젠다·논의 주제를 입력해주세요."),
    ]

    slack_client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": _MEETING_EDIT_MODAL_CALLBACK,
            "title": {"type": "plain_text", "text": "미팅 일정 편집"},
            "submit": {"type": "plain_text", "text": "저장"},
            "close": {"type": "plain_text", "text": "취소"},
            "private_metadata": private_meta,
            "blocks": blocks,
        },
    )


def handle_meeting_edit_modal(slack_client, user_id: str, view: dict) -> None:
    """미팅 일정 편집 모달 제출 — 변경된 필드만 patch."""
    values = view.get("state", {}).get("values", {}) or {}

    def _val(block_id: str, action_id: str) -> str:
        return ((values.get(block_id, {}).get(action_id, {}) or {}).get("value") or "").strip()

    title = _val("title_block", "title_input")
    date = _val("date_block", "date_input")
    time = _val("time_block", "time_input")
    duration_raw = _val("duration_block", "duration_input")
    location = _val("location_block", "location_input")
    agenda = _val("agenda_block", "agenda_input")

    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except Exception:
        meta = {}
    event_id = meta.get("event_id")
    channel = meta.get("channel") or user_id
    thread_ts = meta.get("thread_ts")

    if not event_id:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="⚠️ 미팅 정보를 찾을 수 없습니다 (event_id 누락).")
        return

    # 입력 검증
    if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 날짜 형식이 잘못되었습니다: `{date}` (예: 2026-05-01)")
        return
    if time and not re.match(r"^\d{2}:\d{2}$", time):
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 시간 형식이 잘못되었습니다: `{time}` (예: 15:00)")
        return
    try:
        duration_minutes = int(duration_raw) if duration_raw else 60
        if duration_minutes <= 0:
            raise ValueError("duration must be positive")
    except (ValueError, TypeError):
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 소요시간 형식이 잘못되었습니다: `{duration_raw}` (분 단위 숫자)")
        return

    # 기존 draft / 이벤트 정보 로드 (변경 비교용)
    draft = next(
        (d for d in _meeting_drafts.values() if d.get("event_id") == event_id),
        None,
    )
    try:
        creds = user_store.get_credentials(user_id)
    except Exception as e:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 인증 오류: {e}")
        return

    if draft and draft.get("info"):
        old_info = dict(draft["info"])
    else:
        try:
            event = cal.get_event(creds, event_id)
            ext = (event.get("extendedProperties") or {}).get("private") or {}
            old_info = _build_info_from_event(event, ext.get("company"))
        except Exception as e:
            log.exception("handle_meeting_edit_modal: 기존 이벤트 조회 실패")
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text=f"⚠️ 기존 미팅 정보 조회 실패: {e}")
            return

    # 변경 비교 — 빈 입력은 변경하지 않은 것으로 간주 (date/time/duration 은 모두 필수 입력으로 간주)
    patch_kwargs: dict = {}
    new_info = dict(old_info)
    change_lines: list[str] = []

    if title and title != (old_info.get("title") or ""):
        patch_kwargs["summary"] = title
        new_info["title"] = title
        change_lines.append(f"제목 → *{title}*")

    # 날짜·시간·소요시간 중 하나라도 바뀌면 start/end 모두 patch
    new_date = date or old_info.get("date") or ""
    new_time = time or old_info.get("time") or ""
    new_duration = duration_minutes
    old_duration = int(old_info.get("duration_minutes") or 60)
    if (new_date != (old_info.get("date") or "")
            or new_time != (old_info.get("time") or "")
            or new_duration != old_duration):
        if not new_date or not new_time:
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text="⚠️ 날짜와 시간을 모두 입력해주세요.")
            return
        try:
            start_dt = datetime.fromisoformat(f"{new_date}T{new_time}:00+09:00")
            end_dt = start_dt + timedelta(minutes=new_duration)
        except Exception as e:
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text=f"⚠️ 날짜/시간 파싱 실패: {e}")
            return
        patch_kwargs["start_dt"] = start_dt
        patch_kwargs["end_dt"] = end_dt
        new_info["date"] = new_date
        new_info["time"] = new_time
        new_info["duration_minutes"] = new_duration
        change_lines.append(
            f"일시 → *{new_date} {new_time}* ({new_duration}분)"
        )

    # 장소·어젠다는 빈 입력도 의도된 비우기로 간주 (modal 에 그대로 노출되었으므로)
    if location != (old_info.get("location") or ""):
        patch_kwargs["location"] = location
        new_info["location"] = location
        change_lines.append(f"장소 → *{location or '(없음)'}*")
    if agenda != (old_info.get("agenda") or ""):
        patch_kwargs["description"] = agenda
        new_info["agenda"] = agenda
        change_lines.append(f"어젠다 → _{agenda or '(없음)'}_")

    if not patch_kwargs:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="ℹ️ 변경된 항목이 없습니다.")
        return

    try:
        cal.update_event(creds, event_id, **patch_kwargs)
    except Exception as e:
        log.exception("미팅 일정 편집 실패 (cal.update_event)")
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 일정 업데이트 실패: {e}")
        return

    # draft 동기화
    with _drafts_lock:
        for d in _meeting_drafts.values():
            if d.get("event_id") == event_id:
                d["info"] = dict(new_info)
        _save_meeting_drafts()

    summary_text = "\n".join(f"• {line}" for line in change_lines)
    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text=f"✅ 일정이 업데이트되었습니다.\n{summary_text}")


def trigger_cancel_for_event(slack_client, user_id: str, event_id: str,
                              channel: str | None = None,
                              thread_ts: str | None = None) -> None:
    """소환된 미팅의 '❌ 미팅 취소' 버튼 → 기존 취소 확인 UI 재사용.

    cal.get_event 으로 이벤트를 조회한 뒤, 기존 _post_cancel_confirm 을 호출해
    동일한 '취소 확정/유지' 흐름을 진행한다.
    """
    try:
        creds = user_store.get_credentials(user_id)
    except Exception as e:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 인증 오류: {e}")
        return
    try:
        event = cal.get_event(creds, event_id)
    except Exception as e:
        log.exception("미팅 취소용 이벤트 조회 실패")
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 미팅 정보를 불러오지 못했습니다: {e}")
        return
    _post_cancel_confirm(slack_client, user_id, event, channel, thread_ts)
