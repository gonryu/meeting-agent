"""Before 에이전트 — 미팅 준비 오케스트레이터"""
import json
import os
import re
import threading
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
from tools import drive, gmail, trello
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

    # Trello 카드 컨텍스트 조회 (미완료 체크리스트 항목 + 최근 코멘트)
    trello_items = []
    trello_context = {}
    try:
        trello_context = trello.get_card_context(user_id, company_name, limit_comments=3)
        trello_items = trello_context.get("incomplete_items", [])
    except Exception as e:
        log.warning(f"Trello 조회 실패: {e}")

    return {"trello": trello_items, "emails": emails[:3], "minutes": minutes}


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

    # 1단계: 모든 미팅 헤더 즉시 발송 (리서치 없이)
    for ev in events:
        meeting = cal.parse_event(ev)

        # extendedProperties에 명시된 업체명 (쉼표 구분 복수 업체 가능)
        company_raw = (
            ev.get("extendedProperties", {}).get("private", {}).get("company")
        )
        company_names = [c.strip() for c in company_raw.split(",") if c.strip()] if company_raw else []

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


def _extract_company_content_sections(company_content: str) -> tuple[list[str], list[str], list[str], list[dict]]:
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

    return news_lines, parascope_lines, connection_lines, drive_emails


def _run_briefing_research(
    slack_client, user_id: str, meeting: dict, company_name: str,
    channel: str | None, thread_ts: str | None,
) -> None:
    """백그라운드 스레드: 업체·인물 리서치 후 순차적으로 Slack에 발송."""
    try:
        # 1. 업체 리서치
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"🔍 *{company_name}* 업체 리서치 중...")
        company_content = ""
        try:
            company_content, _ = research_company(user_id, company_name)
        except Exception as e:
            err = str(e)
            msg = ("⚠️ Gemini API 할당량 초과. 잠시 후 다시 시도해주세요."
                   if "429" in err else f"⚠️ 업체 리서치 오류: {err[:200]}")
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts, text=msg)

        news_lines, parascope_lines, connection_lines, drive_emails = \
            _extract_company_content_sections(company_content)
        log.info(f"news_lines ({company_name}): {news_lines}")

        company_blocks = build_company_research_block(
            company_name, news_lines, parascope_lines, connection_lines
        )
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              blocks=company_blocks, text=f"🏢 {company_name} 업체 정보")

        # 2. 인물 리서치 (순차적으로 각 인물 완료 시 발송)
        person_names = [a.get("name") or a.get("email", "").split("@")[0]
                        for a in meeting.get("attendees", [])]
        persons_info: list[dict] = []
        for name in person_names[:3]:
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text=f"👤 *{name}* 인물 리서치 중...")
            try:
                info, _ = research_person(user_id, name, company_name)
            except Exception:
                info = ""
            persons_info.append({"name": name, "raw": info})

        if persons_info:
            person_blocks = build_persons_block([{"name": p["name"]} for p in persons_info])
            if person_blocks:
                _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                      blocks=person_blocks, text="👤 담당자 정보")

        # 3. 이전 맥락 조회
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="📨 이전 커뮤니케이션 맥락 조회 중...")
        context = get_previous_context(user_id, company_name, person_names)

        if not context.get("emails") and drive_emails:
            context = {**context, "emails": drive_emails}

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
            lines.append(f"📝  *어젠다*: {agenda}")
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
            "user_msg_ts": user_msg_ts,
            "attendee_emails": attendee_emails,
            "missing_names": missing_names,
            "pending_selections": pending_selections,
            "stage": "email_select",
        }
        _post_email_selection(slack_client, user_id, pending_selections[0], channel, thread_ts)
        return

    if missing_names:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ *{', '.join(missing_names)}*의 이메일을 찾지 못했습니다. 해당 참석자 없이 일정을 생성합니다.")

    # 일정 먼저 생성 (업체명 미확정이어도 진행)
    created_event_id = _create_calendar_event(
        slack_client, user_id, info, company, attendee_emails,
        channel, thread_ts, user_msg_ts=user_msg_ts,
    )

    # 업체명 후보가 있지만 확정 안 됨 → 일정 생성 후 별도로 확인 요청
    if company_candidates and not company_confirmed and created_event_id:
        _post_company_confirmation(
            slack_client, user_id, company_candidates,
            event_id=created_event_id, channel=channel, thread_ts=thread_ts,
        )


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

        time_str = format_time(event["start"]["dateTime"])
        attendee_display = ", ".join(attendee_emails) if attendee_emails else "없음"
        msg = f"✅ 미팅이 생성되었습니다.\n*{info.get('title', '미팅')}* — {time_str}\n참석자: {attendee_display}"
        if company:
            msg += f"\n업체: {company}"
        msg += "\n_이 메시지에 스레드 답글로 제목, 참석자, 어젠다를 알려주시면 업데이트해드릴게요._"
        resp = _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts, text=msg)
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
                event_id=event_id,
            ),
            daemon=True,
        ).start()

        return event_id

    except Exception as e:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 미팅 생성 실패: {e}")
        return None


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
