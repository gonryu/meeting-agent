"""Before 에이전트 — 미팅 준비 오케스트레이터"""
import functools
import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta

import logging
import anthropic
from google import genai
from google.genai import types as genai_types
from dotenv import load_dotenv
from slack_sdk import WebClient

log = logging.getLogger(__name__)

from tools import calendar as cal
from tools import drive, gmail
from tools.slack_tools import (
    build_briefing_message,
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

_gemini = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
_GEMINI_MODEL = "gemini-2.0-flash"
_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_CLAUDE_MODEL = "claude-haiku-4-5"

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


_pending_agenda: dict[str, list] = _load_pending_agenda()


def _post(slack_client, *, user_id: str, channel=None, thread_ts=None,
          text=None, blocks=None) -> dict:
    """channel 기본값을 user_id(DM)로 적용한 chat_postMessage 헬퍼"""
    kwargs = {"channel": channel or user_id}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    if blocks is not None:
        kwargs["blocks"] = blocks
    if text is not None:
        kwargs["text"] = text
    return slack_client.chat_postMessage(**kwargs)


# ── LLM 호출 헬퍼 (Gemini 우선, 실패 시 Claude 폴백) ────────

def _search(prompt: str) -> str:
    """웹 검색 포함 LLM 호출 — Gemini 우선, 실패 시 Claude"""
    try:
        resp = _gemini.models.generate_content(
            model=_GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())]
            ),
        )
        return resp.text.strip()
    except Exception as e:
        log.warning(f"Gemini _search 실패, Claude로 폴백: {e}")
        resp = _claude.beta.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=2048,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
            betas=["web-search-2025-03-05"],
        )
        return "\n".join(block.text for block in resp.content if hasattr(block, "text")).strip()


def generate_text(prompt: str) -> str:
    """일반 LLM 호출 (public) — Gemini 우선, 실패 시 Claude"""
    return _generate(prompt)


def _generate(prompt: str) -> str:
    """일반 LLM 호출 — Gemini 우선, 실패 시 Claude"""
    try:
        resp = _gemini.models.generate_content(model=_GEMINI_MODEL, contents=prompt)
        return resp.text.strip()
    except Exception as e:
        log.warning(f"Gemini _generate 실패, Claude로 폴백: {e}")
        resp = _claude.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()


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


def research_company(user_id: str, company_name: str, force: bool = False) -> tuple[str, str | None]:
    """업체 정보 수집. Returns: (content, file_id)
    force=True 이면 신선도 체크 없이 강제 재검색.
    """
    creds, contacts_folder_id, knowledge_file_id = _get_creds_and_config(user_id)
    content, file_id, is_fresh = drive.get_company_info(creds, contacts_folder_id, company_name)

    if is_fresh and content and not force:
        return content, file_id

    today = datetime.now().strftime("%Y-%m-%d")

    # 1단계: ParaScope 봇 조회
    parascope_section = ""
    parascope_text = _query_parascope(company_name)
    if parascope_text:
        parascope_body = _to_bullet_lines(parascope_text)
        parascope_section = f"## ParaScope 브리핑\n- last_searched: {today}\n{parascope_body}\n"
        log.info(f"ParaScope 브리핑 수집 완료: {company_name}")

    # 2단계: Gmail 이메일 맥락 수집
    email_section = ""
    try:
        emails = gmail.search_recent_emails(creds, company_name, company_name)
        if emails:
            lines = [
                f"- {e['date']} | {e['subject']} | "
                f"{e.get('snippet', '').replace(chr(10), ' ').replace(chr(13), ' ')[:100]}"
                for e in emails[:5]
            ]
            email_section = f"## 이메일 맥락\n- last_searched: {today}\n" + "\n".join(lines) + "\n"
    except Exception as e:
        log.warning(f"Gmail 검색 실패 ({company_name}): {e}")

    # 3단계: 웹 검색
    news_text = _to_bullet_lines(_search(company_news_prompt(company_name)))
    knowledge = drive.get_company_knowledge(creds, knowledge_file_id)
    connections = _to_bullet_lines(_generate(service_connection_prompt(news_text, knowledge)))

    # 섹션 순서: 최근 동향 → 이메일 맥락 → 파라메타 서비스 연결점 → ParaScope 브리핑
    new_content = (
        f"# {company_name}\n\n"
        f"## 최근 동향\n- last_searched: {today}\n{news_text}\n\n"
        f"{email_section}\n"
        f"## 파라메타 서비스 연결점\n{connections}\n\n"
        f"{parascope_section}"
    )
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

    # 2단계: 웹 검색
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

    new_content = (
        f"# {person_name}\n\n"
        f"## 기본 정보\n"
        f"- 소속: {company_name}\n"
        f"- last_searched: {today}\n"
        f"{email_line}"
        f"{card_section}"
        f"{email_section}\n"
        f"## 공개 정보\n{info_text}\n"
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

    return {"trello": [], "emails": emails[:3], "minutes": minutes}


# ── 브리핑 생성 ──────────────────────────────────────────────

def run_briefing(slack_client, user_id: str, event: dict = None,
                 channel: str = None, thread_ts: str = None) -> list[str]:
    """
    브리핑 실행. event가 None이면 오늘 캘린더 전체 조회.
    Returns: 발송된 메시지 ts 목록
    """
    creds = user_store.get_credentials(user_id)
    user = user_store.get_user(user_id)
    contacts_folder_id = user["contacts_folder_id"]

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
              text=f"📅 {display_name}님의 향후 24시간 일정을 보여드리겠습니다.")

        events = cal.get_upcoming_meetings(creds, days=1, from_now=True)
        import sys
        from datetime import datetime
        from zoneinfo import ZoneInfo
        _now = datetime.now(ZoneInfo("Asia/Seoul"))
        print(f"[DEBUG] get_upcoming_meetings 결과: {len(events)}개, now={_now.isoformat()}", flush=True, file=sys.stderr)
        for _ev in events:
            _s = _ev.get("start", {}).get("dateTime") or _ev.get("start", {}).get("date", "")
            print(f"[DEBUG] 이벤트: '{_ev.get('summary','')}' start={_s}", flush=True, file=sys.stderr)

    if not events:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="📅 앞으로 24시간 내 미팅이 없습니다.")
        return []

    try:
        company_names = drive.get_company_names(creds, contacts_folder_id)
    except Exception:
        company_names = []

    # company_knowledge.md에서 자사 제품/서비스명 추출 (LRU 캐시 적용)
    internal_products: frozenset = frozenset()
    try:
        knowledge_file_id = user.get("knowledge_file_id")
        if knowledge_file_id:
            knowledge_text = drive.get_company_knowledge(creds, knowledge_file_id)
            if knowledge_text:
                khash = hashlib.md5(knowledge_text.encode()).hexdigest()
                internal_products = _get_internal_products_from_knowledge(khash, knowledge_text)
    except Exception as e:
        log.warning(f"내부 제품명 사전 로드 실패: {e}")

    sent_threads = []
    for ev in events:
        meeting = cal.parse_event(ev)

        # 미팅 생성 시 LLM이 추출한 company가 extendedProperties에 주입된 경우 우선 사용
        injected_company = (
            ev.get("extendedProperties", {}).get("private", {}).get("company")
        )
        if injected_company:
            company_name = injected_company
            log.info(f"extendedProperties에서 company 사용: {company_name}")
        else:
            company_name = _extract_company_name(meeting, company_names, internal_products)

        if company_name:
            ts = _send_briefing(slack_client, user_id, meeting, company_name,
                                channel=channel, thread_ts=thread_ts)
        else:
            ts = _send_internal_briefing(slack_client, user_id, meeting,
                                         channel=channel, thread_ts=thread_ts)
        if ts:
            sent_threads.append(ts)

    user_store.update_last_active(user_id)
    return sent_threads


_PUBLIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com",
    "naver.com", "daum.net", "hanmail.net",
    "yahoo.com", "yahoo.co.kr", "hotmail.com", "outlook.com",
    "icloud.com", "me.com", "mac.com",
    "nate.com", "empas.com",
}

@functools.lru_cache(maxsize=8)
def _get_internal_products_from_knowledge(knowledge_hash: str, knowledge_text: str) -> frozenset:
    """company_knowledge.md에서 자사 제품/서비스/기술명 추출 (내용 해시 기준 LRU 캐시).

    knowledge_hash는 캐시 키로만 사용되며, 실제 추출은 knowledge_text로 수행.
    """
    prompt = f"""아래는 우리 회사의 서비스/제품 소개 문서입니다.
이 문서에서 언급된 우리 회사의 **제품명, 서비스명, 브랜드명, 기술명**을 모두 추출해줘.

문서:
{knowledge_text[:2000]}

규칙:
- 자사 제품/서비스/기술 이름만 추출 (예: ParaSta, ParametaChain 등)
- 한 줄에 하나씩 이름만 반환
- 없으면 "없음" 반환"""
    try:
        result = _generate(prompt).strip()
        names = {
            line.strip().lower()
            for line in result.splitlines()
            if line.strip() and line.strip().lower() not in ("없음", "none", "")
        }
        log.info(f"내부 제품/서비스명 추출: {names}")
        return frozenset(names)
    except Exception as e:
        log.warning(f"내부 제품명 추출 실패: {e}")
        return frozenset()


def _extract_company_name(meeting: dict, known_companies: list[str] = None,
                          internal_products: frozenset = frozenset()) -> str | None:
    """업체명 추출 (참석자 도메인 → Contacts 제목 매칭 → Gemini NLP)

    1순위: 외부 도메인 참석자 확인 (외부 미팅 여부 판별)
      - 단, 제목에서 Contacts 업체명이 발견되면 그 이름을 우선 반환 (한국어 정식명 사용)
    2순위: 제목 ∋ known_companies
    3순위: LLM 추출
    """
    summary = meeting.get("summary", "")
    is_external = False

    for attendee in meeting.get("attendees", []):
        email = attendee.get("email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        if domain and domain not in cal.INTERNAL_DOMAINS and domain not in _PUBLIC_EMAIL_DOMAINS:
            is_external = True
            break

    if is_external:
        # 제목에 Contacts 업체명이 있으면 정식명(한국어) 우선 반환
        if known_companies:
            summary_lower = summary.lower()
            for company in known_companies:
                if company.lower() in summary_lower:
                    return company
        # 없으면 도메인 기반 이름 반환
        for attendee in meeting.get("attendees", []):
            email = attendee.get("email", "")
            domain = email.split("@")[-1] if "@" in email else ""
            if domain and domain not in cal.INTERNAL_DOMAINS and domain not in _PUBLIC_EMAIL_DOMAINS:
                company = domain.split(".")[0]
                if company and company.lower() not in internal_products:
                    return company

    if known_companies:
        summary_lower = summary.lower()
        for company in known_companies:
            if company.lower() in summary_lower:
                return company

    if summary:
        return _extract_company_with_llm(summary, internal_products)

    return None


def _extract_company_with_llm(summary: str, internal_products: frozenset = frozenset()) -> str | None:
    """LLM으로 미팅 제목에서 외부 업체명 추출"""
    exclude_hint = ""
    if internal_products:
        names = ", ".join(sorted(internal_products))
        exclude_hint = f"\n- 자사 제품/서비스/기술명({names})은 업체명이 아니므로 null 반환"

    prompt = f"""캘린더 이벤트 제목에서 외부 업체명만 추출해줘.

제목: "{summary}"

규칙:
- 외부 업체명이 있으면 그 이름만 반환 (예: 삼성전자, 카카오, 네이버)
- 사내 일정(팀 회의, 스탠드업, 외근, 점심, 사무실 등)이면 null 반환{exclude_hint}
- 불확실하면 null 반환
- 업체명 또는 null 만 반환, 설명 없이"""
    try:
        result = _generate(prompt).strip()
        if result.lower() in ("null", "없음", "none", ""):
            return None
        if len(result) > 30 or "\n" in result:
            return None
        # 자사 제품/서비스명이면 null 처리
        if result.lower() in internal_products:
            return None
        return result
    except Exception:
        return None


def _send_briefing(slack_client, user_id: str, meeting: dict, company_name: str,
                   channel: str = None, thread_ts: str = None) -> str | None:
    """브리핑 생성 및 Slack 발송. Returns: 발송된 메시지 ts"""
    try:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"🔍 *{company_name}* 업체 리서치 중...")
        try:
            company_content, _ = research_company(user_id, company_name)
        except Exception as e:
            err = str(e)
            msg = ("⚠️ Gemini API 할당량 초과. 잠시 후 다시 시도해주세요."
                   if "429" in err else f"⚠️ 리서치 오류: {err[:200]}")
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts, text=msg)
            return None

        person_names = [a.get("name") or a.get("email", "").split("@")[0]
                        for a in meeting.get("attendees", [])]
        persons_info = []
        for name in person_names[:3]:
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text=f"👤 *{name}* 인물 리서치 중...")
            info, _ = research_person(user_id, name, company_name)
            persons_info.append({"name": name, "raw": info})

        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"📨 이전 커뮤니케이션 맥락 조회 중...")
        context = get_previous_context(user_id, company_name, person_names)

        # ## 최근 동향 섹션 추출
        news_lines = []

        # 1) 섹션 텍스트 추출
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

        # 2) 블록 분리: `---` 구분자 우선 시도, 없으면 단독 `-`/`•` 사용
        import re as _re
        raw_blocks = _re.split(r'\n\s*-{3,}\s*\n', news_section)
        if len(raw_blocks) <= 1:
            # fallback: 단독 `-` 또는 `•` 줄 기준 분리
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

        # 3) 각 블록에서 제목 + URL 한 줄로 요약
        for block in raw_blocks:
            if len(news_lines) >= 3:
                break
            block = block.strip()
            if not block:
                continue

            block_lines = [l.strip() for l in block.splitlines() if l.strip()]

            # 도입 문장(preamble) 블록 스킵: bold/URL 없이 길기만 한 경우
            has_bold = any("**" in l for l in block_lines)
            has_url  = any("http" in l or "출처" in l for l in block_lines)
            if not has_bold and not has_url and len(block) > 120:
                continue

            # 제목 추출: bold(**...**) 줄 우선, 없으면 첫 줄
            title = ""
            for l in block_lines:
                m = _re.search(r'\*\*(.+?)\*\*', l)
                if m:
                    title = m.group(1).strip()
                    # "N. 제목" 형식이면 번호 제거
                    title = _re.sub(r'^\d+\.\s*', '', title)
                    break
            if not title:
                title = block_lines[0][:120] if block_lines else ""

            # URL 추출
            url = ""
            for l in block_lines:
                url_m = _re.search(r'https?://\S+', l)
                if url_m:
                    url = url_m.group(0).rstrip(')').rstrip('.')
                    break

            if not title:
                continue
            news_lines.append(f"{title} ({url})" if url else title)

        log.info(f"news_lines ({company_name}): {news_lines}")

        # ## ParaScope 브리핑 섹션 추출
        parascope_lines = []
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

        in_connections = False
        connection_lines = []
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

        # Gmail 이메일 없으면 Drive 파일의 ## 이메일 맥락 섹션으로 보완
        if not context.get("emails"):
            drive_emails = []
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
                        # "날짜 | 제목 | snippet" 형식 파싱
                        parts = cleaned.split("|", 2)
                        drive_emails.append({
                            "date": parts[0].strip() if len(parts) > 0 else "",
                            "subject": parts[1].strip() if len(parts) > 1 else cleaned,
                            "snippet": parts[2].strip() if len(parts) > 2 else "",
                        })
                        if len(drive_emails) >= 3:
                            break
            if drive_emails:
                context = {**context, "emails": drive_emails}

        blocks = build_briefing_message(
            meeting=meeting,
            company_name=company_name,
            company_news=news_lines,
            persons=[{"name": p["name"]} for p in persons_info],
            service_connections=connection_lines,
            previous_context=context,
            parascope_content=parascope_lines,
        )

        resp = _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                     blocks=blocks, text=f"{company_name} 미팅 브리핑")
        msg_ts = resp["ts"]
        _pending_agenda[msg_ts] = [meeting["id"], user_id]
        _save_pending_agenda(_pending_agenda)
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
        attendees = [a.get("name") or a.get("email", "") for a in meeting.get("attendees", [])]
        agenda = meeting.get("description", "").strip()

        location_str = f" · 📍{location}" if location else ""
        lines = [
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"*📋 {meeting['summary']} — {time_str} ({link_text}){location_str}*",
        ]
        if attendees:
            lines.append(f"👥  *참석자*: {', '.join(attendees)}")
        if agenda:
            lines.append(f"📝  *어젠다*: {agenda}")
        else:
            lines.append("📝  *어젠다 등록하려면 이 스레드에 답장하세요*")

        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
        resp = _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                     blocks=blocks, text=f"{meeting['summary']} 미팅 브리핑")
        msg_ts = resp["ts"]
        _pending_agenda[msg_ts] = [meeting["id"], user_id]
        _save_pending_agenda(_pending_agenda)
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
                             channel: str = None, thread_ts: str = None):
    """자연어 요청으로 Calendar 미팅 생성"""
    # 새 일정 생성 시 기존 드래프트 초기화
    _meeting_drafts.pop(user_id, None)

    try:
        raw = _generate(parse_meeting_prompt(user_message))
    except Exception as e:
        err = str(e)
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text="⚠️ Gemini API 할당량 초과입니다. 잠시 후 다시 시도해주세요.")
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

    # 외부 업체/기관명 (company 필드) — 개인 담당자와 분리된 필드
    company = info.get("company") or None
    if isinstance(company, str) and company.lower() in ("null", "none", ""):
        company = None
    log.info(f"미팅 파싱 결과 — company={company}, participants={info.get('participants')}")

    attendee_emails = []
    missing_names = []
    pending_selections = []  # 이메일 후보가 여러 개인 참석자
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

    # 이메일 후보가 여러 개인 참석자가 있으면 사용자 선택 대기
    if pending_selections:
        _pending_meetings[user_id] = {
            "info": info,
            "company": company,
            "channel": channel,
            "thread_ts": thread_ts,
            "attendee_emails": attendee_emails,
            "missing_names": missing_names,
            "pending_selections": pending_selections,
        }
        _post_email_selection(slack_client, user_id, pending_selections[0], channel, thread_ts)
        return

    if missing_names:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ *{', '.join(missing_names)}*의 이메일을 찾지 못했습니다. 해당 참석자 없이 일정을 생성합니다.")

    _create_calendar_event(slack_client, user_id, info, company, attendee_emails, channel, thread_ts)


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
# user_id → {info, company, event_id, channel, thread_ts, created_at}
_meeting_drafts: dict[str, dict] = {}
_DRAFT_TTL_SECONDS = 7200  # 2시간 후 만료


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
            "action_id": "select_attendee_email",
        }
        for email in candidates
    ] + [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "이 참석자 제외"},
            "value": f"{user_id}|__skip__",
            "action_id": "select_attendee_email",
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
                           attendee_emails: list[str], channel: str = None, thread_ts: str = None):
    """Calendar 이벤트 생성, 브리핑 실행, 드림플러스 회의실 예약 제안"""
    import threading
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
        )
        event_id = event["id"]

        # Google Meet 트랜스크립트 자동 활성화
        conference_id = (event.get("conferenceData") or {}).get("conferenceId")
        if conference_id:
            cal.enable_meet_transcription(creds, conference_id)

        time_str = format_time(event["start"]["dateTime"])
        attendee_display = ", ".join(attendee_emails) if attendee_emails else "없음"
        msg = f"✅ 미팅이 생성되었습니다.\n*{info.get('title', '미팅')}* — {time_str}\n참석자: {attendee_display}"
        if company:
            msg += f"\n업체: {company}"
        msg += "\n_이 메시지에 스레드 답글로 제목, 참석자, 어젠다를 알려주시면 업데이트해드릴게요._"
        resp = _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts, text=msg)
        reply_ts = resp.get("ts") if resp else None

        # 드래프트 저장 — reply_ts 스레드 답글만 업데이트 허용
        _meeting_drafts[user_id] = {
            "info": dict(info),
            "company": company,
            "event_id": event_id,
            "attendee_emails": list(attendee_emails),
            "channel": channel,
            "thread_ts": thread_ts,
            "reply_ts": reply_ts,
            "created_at": datetime.now().isoformat(),
        }

        if company:
            event.setdefault("extendedProperties", {}).setdefault("private", {})["company"] = company

        # 드림플러스 회의실 자동 추천 먼저 (계정 미설정 시 스킵)
        attendee_count = len(attendee_emails) + 1  # 참석자 + 주최자
        threading.Thread(
            target=dreamplus_agent.auto_book_room,
            kwargs=dict(
                slack_client=slack_client,
                user_id=user_id,
                start_dt=start_dt,
                end_dt=end_dt,
                title=info.get("title", "미팅"),
                attendee_count=attendee_count,
                channel=channel,
                thread_ts=thread_ts,
            ),
            daemon=True,
        ).start()

        # 업체 리서치/브리핑은 백그라운드에서 나중에 실행
        threading.Thread(
            target=run_briefing,
            args=(slack_client, user_id, event),
            kwargs=dict(channel=channel, thread_ts=thread_ts),
            daemon=True,
        ).start()

    except Exception as e:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 미팅 생성 실패: {e}")


def handle_email_selection(slack_client, body: dict):
    """select_attendee_email 버튼 클릭 처리"""
    user_id = body["user"]["id"]
    value = body["actions"][0]["value"]
    pending = _pending_meetings.get(user_id)
    if not pending:
        return

    _, email = value.split("|", 1)
    if email != "__skip__":
        pending["attendee_emails"].append(email)
    pending["pending_selections"].pop(0)

    if pending["pending_selections"]:
        # 아직 선택이 남아있으면 다음 항목 표시
        _post_email_selection(slack_client, user_id,
                              pending["pending_selections"][0],
                              pending["channel"], pending["thread_ts"])
    else:
        # 모든 선택 완료 → 이벤트 생성
        del _pending_meetings[user_id]
        if pending.get("missing_names"):
            _post(slack_client, user_id=user_id,
                  channel=pending["channel"], thread_ts=pending["thread_ts"],
                  text=f"⚠️ *{', '.join(pending['missing_names'])}*의 이메일을 찾지 못했습니다. 해당 참석자 없이 일정을 생성합니다.")
        _create_calendar_event(slack_client, user_id,
                               pending["info"], pending["company"],
                               pending["attendee_emails"],
                               pending["channel"], pending["thread_ts"])


# ── 일정 드래프트 업데이트 ──────────────────────────────────────

def has_meeting_draft(user_id: str) -> bool:
    """유효한(만료 안 된) 일정 드래프트가 있는지 확인"""
    draft = _meeting_drafts.get(user_id)
    if not draft:
        return False
    created = datetime.fromisoformat(draft["created_at"])
    if (datetime.now() - created).total_seconds() > _DRAFT_TTL_SECONDS:
        del _meeting_drafts[user_id]
        return False
    return True


def update_meeting_from_text(slack_client, user_id: str, user_message: str,
                              channel: str = None, thread_ts: str = None) -> bool:
    """기존 드래프트에 새 메시지를 병합하여 캘린더 이벤트 업데이트.
    Returns: True if the message was handled as a meeting update, False otherwise.
    """
    draft = _meeting_drafts.get(user_id)
    if not draft:
        return False

    # 채널 응답은 항상 원래 일정 생성 스레드에 달기
    reply_channel = draft.get("channel") or channel
    reply_thread_ts = draft.get("thread_ts") or thread_ts

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
    draft["created_at"] = datetime.now().isoformat()  # TTL 리셋

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

    if "participants" in changed_fields:
        new_names = updated_info.get("participants", [])
        new_emails = list(attendee_emails)  # 기존 유지 후 추가
        missing = []
        for name in new_names:
            if name not in [n for n in draft["info"].get("participants", [])]:
                candidates = _find_email_candidates(user_id, name, slack_client)
                if candidates:
                    new_emails.append(candidates[0])
                else:
                    missing.append(name)
        draft["attendee_emails"] = new_emails
        patch_kwargs["attendee_emails"] = new_emails
        names_str = ", ".join(new_names) if new_names else "없음"
        change_summary_lines.append(f"참석자 → {names_str}")
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
        msg = ("⚠️ Gemini API 할당량 초과. 잠시 후 다시 시도해주세요."
               if "429" in err or "RESOURCE_EXHAUSTED" in err else f"⚠️ 갱신 실패: {err[:200]}")
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts, text=msg)
