"""미팅 에이전트 — Slack Bolt 앱 진입점"""
import json
import os
import logging
import threading
from dotenv import load_dotenv

# 반드시 가장 먼저 호출 (override=True: 시스템 환경변수보다 .env 우선)
load_dotenv(override=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
log = logging.getLogger(__name__)

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from apscheduler.schedulers.background import BackgroundScheduler
import uvicorn

from agents.before import (
    run_briefing,
    handle_agenda_reply,
    create_meeting_from_text,
    has_meeting_draft,
    update_meeting_from_text,
    update_company_knowledge,
    research_company,
    research_person,
    generate_text,
    _pending_agenda,
)
from agents.during import (
    start_session,
    add_note,
    end_session,
    check_transcripts,
    get_minutes_list,
)
from agents import after
from agents import room as room_agent
from agents import card as card_agent
from store import user_store
from server import oauth as oauth_server

app = App(token=os.getenv("SLACK_BOT_TOKEN"))


@app.error
def global_error_handler(error, body, logger):
    logger.exception(f"에러 발생: {error}")
    logger.error(f"요청 body: {body}")


# ── 등록 확인 헬퍼 ──────────────────────────────────────────────

def _check_registered(client, user_id: str, channel: str = None) -> bool:
    """미등록 사용자에게 안내 메시지 전송. 등록된 경우 True 반환."""
    if user_store.is_registered(user_id):
        return True
    auth_url = oauth_server.build_auth_url(user_id)
    client.chat_postMessage(
        channel=channel or user_id,
        text=f"⚠️ 먼저 Google 계정을 연결해주세요.\n아래 링크에서 인증을 완료하면 자동으로 등록됩니다.\n{auth_url}",
    )
    return False


# ── 매일 09:00 자동 브리핑 ───────────────────────────────────

def scheduled_briefing():
    log.info("자동 브리핑 시작")
    for row in user_store.all_users():
        user_id = row["slack_user_id"]
        try:
            run_briefing(app.client, user_id=user_id)
        except Exception as e:
            log.error(f"자동 브리핑 실패 ({user_id}): {e}")


def scheduled_transcript_check():
    log.info("트랜스크립트 폴링 시작")
    check_transcripts(app.client)


def scheduled_action_item_reminder():
    log.info("액션아이템 리마인더 실행")
    after.action_item_reminder(app.client)


from datetime import datetime as _dt
scheduler = BackgroundScheduler(timezone="Asia/Seoul")
scheduler.add_job(scheduled_briefing, "cron", hour=9, minute=0)
scheduler.add_job(scheduled_transcript_check, "interval", minutes=10,
                  next_run_time=_dt.now())
scheduler.add_job(scheduled_action_item_reminder, "cron", hour=8, minute=0)


# ── @멘션 처리 ───────────────────────────────────────────────

@app.event("app_mention")
def handle_mention(event, say, client):
    user_id = event.get("user")
    text = event.get("text", "")
    text = " ".join(word for word in text.split() if not word.startswith("<@")).strip()
    channel = event.get("channel")
    parent_ts = event.get("thread_ts")   # 스레드 답장이면 부모 메시지 ts
    thread_ts = parent_ts or event.get("ts")

    # 브리핑 스레드에 @멘션으로 답장한 경우 → 어젠다 등록
    log.info(f"handle_mention: parent_ts={parent_ts} pending_keys={list(_pending_agenda.keys())[:5]}")
    if parent_ts and parent_ts in _pending_agenda:
        handle_agenda_reply(client, parent_ts, text)
        return

    if not text:
        client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text="안녕하세요!\n• 브리핑: '브리핑 해줘'\n• 미팅 생성: '오늘 15시에 김민환 미팅 잡아줘'",
        )
        return

    if not _check_registered(client, user_id, channel):
        return

    _route_message(text, client, user_id=user_id, channel=channel, thread_ts=thread_ts)


# ── DM 처리 ─────────────────────────────────────────────────

@app.event("message")
def handle_message(event, client):
    if event.get("bot_id"):
        return

    subtype = event.get("subtype")
    user_id = event.get("user")

    # ── 이미지 파일 업로드 (명함 OCR) ──────────────────────────
    if subtype == "file_share" and event.get("channel_type") == "im" and user_id:
        if _check_registered(client, user_id):
            for f in event.get("files", []):
                mime = f.get("mimetype", "")
                if mime.startswith("image/"):
                    log.info(f"명함 이미지 업로드 감지: user={user_id} file={f.get('id')}")
                    card_agent.handle_image_upload(client, user_id, f)
        return

    if subtype:
        return

    thread_ts = event.get("thread_ts")
    text = event.get("text", "").strip()

    log.info(f"handle_message: channel_type={event.get('channel_type')} thread_ts={thread_ts} pending_keys={list(_pending_agenda.keys())[:5]}")
    if thread_ts and thread_ts in _pending_agenda:
        handle_agenda_reply(client, thread_ts, text)
        return

    if event.get("channel_type") == "im":
        if not _check_registered(client, user_id):
            return
        _route_message(text, client, user_id=user_id)


_INTENT_PROMPT = """사용자의 Slack 메시지를 분석해서 의도(intent)를 분류해줘.

메시지: "{text}"

가능한 intent 목록:
- briefing: 브리핑 요청 (예: "브리핑 해줘", "오늘 미팅 현황", "brief")
- create_meeting: 미팅/일정 생성 (예: "내일 3시에 KISA 미팅 잡아줘", "오늘 15시 홍길동 회의 만들어줘")
- start_session: 미팅 시작 (예: "미팅 시작", "회의 시작해줘", "지금부터 KISA 회의 시작")
- add_note: 메모 추가 — 현재 진행 중인 회의에 내용 기록 (예: "메모: 예산 협의됨", "기록해줘 다음달 계약 예정", "노트 추가")
- end_session: 미팅 종료 (예: "미팅 종료", "회의 끝났어", "미팅 마무리해줘")
- get_minutes: 저장된 회의록 목록 조회 (예: "회의록 보여줘", "지난 회의록", "minutes 목록")
- research_company: 특정 업체 정보 조사 (예: "KISA 알아봐줘", "삼성전자 정보 검색해줘", "카카오 최근 동향")
- research_person: 특정 인물 정보 조사 (예: "홍길동 인물 정보", "김민환 누구야", "이준호 카카오 담당자 조사해줘")
- update_knowledge: 내부 서비스 지식 갱신 (예: "knowledge 업데이트", "서비스 정보 갱신")
- room_status: 회의실 예약 현황 조회 (예: "회의실 현황", "오늘 회의실 어때", "내일 회의실 비었어?", "4월 1일 회의실 보여줘")
- my_reservations: 내 회의실 예약 목록 (예: "내 예약 보여줘", "내가 예약한 회의실", "예약 목록")
- reserve_room: 회의실 예약 요청 (예: "내일 오후 2시~4시 8층 회의실 예약해줘", "오늘 3시에 회의실 잡아줘", "KISA 회의용 회의실 예약")
- cancel_reservation: 예약 취소 (예: "예약 취소해줘", "회의실 취소", "예약 목록에서 취소할게")
- check_credits: 크레딧/포인트 조회 (예: "크레딧 얼마야", "포인트 남은거 확인해줘", "드림플러스 크레딧")
- unknown: 위 중 해당 없음

params 추출 규칙:
- create_meeting: params 없음 (원본 메시지 전체를 그대로 사용)
- start_session: {{"title": "미팅 제목 (없으면 빈 문자열)"}}
- add_note: {{"note": "메모 내용 ('메모:', '기록해줘' 등 트리거 단어 제거 후)"}}
- research_company: {{"company": "업체명"}}
- research_person: {{"person": "이름", "company": "소속 업체명 (없으면 빈 문자열)"}}
- room_status: {{"date": "YYYY-MM-DD (오늘이면 null)"}}
- reserve_room: {{"text": "원본 예약 요청 메시지 전체"}}

JSON으로만 반환 (설명 없이):
{{"intent": "...", "params": {{}}}}"""


def _classify_intent(text: str) -> dict:
    """LLM으로 사용자 메시지 의도 분류. 실패 시 unknown 반환."""
    try:
        result = generate_text(_INTENT_PROMPT.format(text=text.replace('"', "'")))
        # JSON 파싱 — 마크다운 코드블록 제거
        cleaned = result.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(cleaned)
    except Exception as e:
        log.warning(f"인텐트 분류 실패: {e} / 원문: {result if 'result' in dir() else '?'}")
        return {"intent": "unknown", "params": {}}


def _route_message(text: str, client, user_id: str, channel: str = None, thread_ts: str = None):
    log.info(f"메시지 라우팅 ({user_id}): {text}")

    # 진행 중인 일정 드래프트가 있으면 업데이트 시도 (intent 분류 전)
    if has_meeting_draft(user_id):
        handled = update_meeting_from_text(client, user_id=user_id, user_message=text,
                                           channel=channel, thread_ts=thread_ts)
        if handled:
            return

    intent_data = _classify_intent(text)
    intent = intent_data.get("intent", "unknown")
    params = intent_data.get("params", {})
    log.info(f"인텐트 분류: {intent} / params: {params}")

    if intent == "briefing":
        run_briefing(client, user_id=user_id, channel=channel, thread_ts=thread_ts)

    elif intent == "create_meeting":
        create_meeting_from_text(client, user_id=user_id, user_message=text,
                                 channel=channel, thread_ts=thread_ts)

    elif intent == "start_session":
        title = params.get("title", "").strip() or "미팅"
        start_session(client, user_id=user_id, title=title)

    elif intent == "add_note":
        note = params.get("note", "").strip() or text
        add_note(client, user_id=user_id, note_text=note)

    elif intent == "end_session":
        end_session(client, user_id=user_id)

    elif intent == "get_minutes":
        get_minutes_list(client, user_id=user_id)

    elif intent == "research_company":
        company = params.get("company", "").strip()
        if not company:
            client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                    text="업체명을 알려주세요. 예: 'KISA 알아봐줘'")
            return
        client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                text=f"🔍 *{company}* 기업정보 리서치 중...")
        try:
            content, _ = research_company(user_id, company, force=True)
            preview = "\n".join(
                l for l in content.splitlines()
                if l.strip() and not l.startswith("#") and "last_searched" not in l
            )[:300]
            client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                    text=f"✅ *{company}* 기업정보 갱신 완료.\n\n```{preview}```")
        except Exception as e:
            client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                    text=f"⚠️ 기업정보 리서치 실패: {e}")

    elif intent == "research_person":
        person = params.get("person", "").strip()
        company = params.get("company", "").strip()
        if not person:
            client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                    text="인물 이름을 알려주세요. 예: '홍길동 카카오 알아봐줘'")
            return
        client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                text=f"🔍 *{person}* 인물정보 리서치 중..." + (f" (소속: {company})" if company else ""))
        try:
            content, _ = research_person(user_id, person, company, force=True)
            preview = "\n".join(
                l for l in content.splitlines()
                if l.strip() and not l.startswith("#") and "last_searched" not in l
            )[:300]
            client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                    text=f"✅ *{person}* 인물정보 갱신 완료.\n\n```{preview}```")
        except Exception as e:
            client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                    text=f"⚠️ 인물정보 리서치 실패: {e}")

    elif intent == "update_knowledge":
        client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                text="🔄 company_knowledge.md 갱신 중입니다...")
        update_company_knowledge(client, user_id=user_id)

    elif intent == "room_status":
        date_str = params.get("date") or None
        room_agent.cmd_rooms(client, user_id, date_str)

    elif intent == "my_reservations":
        room_agent.cmd_my_reservations(client, user_id)

    elif intent == "reserve_room":
        reserve_text = params.get("text", "").strip() or text
        client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                text=f"📅 예약 정보 분석 중: _{reserve_text}_")
        threading.Thread(
            target=_parse_and_reserve,
            args=(client, user_id, reserve_text),
            daemon=True,
        ).start()

    elif intent == "cancel_reservation":
        room_agent.cmd_my_reservations(client, user_id)

    elif intent == "check_credits":
        room_agent.cmd_credits(client, user_id)

    else:
        client.chat_postMessage(
            channel=channel or user_id,
            thread_ts=thread_ts,
            text=(
                f"'{text[:30]}' 명령을 이해하지 못했어요.\n\n"
                "가능한 요청 예시:\n"
                "• 브리핑 해줘\n"
                "• 내일 3시에 KISA 미팅 잡아줘\n"
                "• 미팅 시작해줘 / 미팅 종료\n"
                "• 메모: 예산 협의됨\n"
                "• 회의록 보여줘\n"
                "• KISA 알아봐줘\n"
                "• 홍길동 카카오 인물 조사해줘\n"
                "• 오늘 회의실 현황 보여줘\n"
                "• 내일 오후 2시~4시 8층 회의실 예약해줘\n"
                "• 내 예약 목록 보여줘\n"
                "• 드림플러스 크레딧 확인해줘"
            ),
        )


# ── Slash Commands ───────────────────────────────────────────

def _register_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if user_store.is_registered(user_id):
        client.chat_postMessage(
            channel=user_id,
            text="✅ 이미 Google 계정이 연결되어 있습니다.\n"
                 "권한 범위를 갱신하거나 계정을 재연결하려면 `/재등록` 을 사용하세요.",
        )
        return
    auth_url = oauth_server.build_auth_url(user_id)
    client.chat_postMessage(
        channel=user_id,
        text=f"🔗 아래 링크를 클릭하여 Google 계정을 연결해주세요.\n{auth_url}",
    )

app.command("/register")(_register_handler)
app.command("/등록")(_register_handler)


def _reregister_handler(ack, body, client):
    """재인증 — 이미 등록된 사용자도 OAuth 플로우를 다시 실행 (스코프 갱신 등)"""
    ack()
    user_id = body["user_id"]
    auth_url = oauth_server.build_auth_url(user_id)
    client.chat_postMessage(
        channel=user_id,
        text=f"🔄 Google 계정 재연결 링크입니다. 클릭 후 권한을 다시 동의해주세요.\n{auth_url}",
    )

app.command("/재등록")(_reregister_handler)
app.command("/reregister")(_reregister_handler)


def _brief_handler(ack, body, client):
    log.info("/brief 명령 수신")
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    client.chat_postMessage(channel=user_id, text="📋 브리핑을 준비 중입니다...")
    run_briefing(client, user_id=user_id)

app.command("/브리핑")(_brief_handler)
app.command("/brief")(_brief_handler)


def _update_handler(ack, body, client):
    log.info("/update 명령 수신")
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    client.chat_postMessage(channel=user_id, text="🔄 company_knowledge.md 갱신 중입니다...")
    update_company_knowledge(client, user_id=user_id)

app.command("/업데이트")(_update_handler)
app.command("/update")(_update_handler)


def _meet_handler(ack, body, client):
    log.info("/meet 명령 수신")
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    text = body.get("text", "").strip()
    if not text:
        client.chat_postMessage(channel=user_id, text="예: `/미팅추가 오늘 15시에 김민환 미팅`")
        return
    client.chat_postMessage(channel=user_id, text=f"📅 미팅 생성 중입니다: _{text}_")
    create_meeting_from_text(client, user_id=user_id, user_message=text)

app.command("/미팅추가")(_meet_handler)
app.command("/meet")(_meet_handler)


def _company_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    company_name = body.get("text", "").strip()
    if not company_name:
        client.chat_postMessage(channel=user_id, text="예: `/company 카카오`")
        return
    client.chat_postMessage(channel=user_id, text=f"🔍 *{company_name}* 기업정보 리서치 중...")
    try:
        content, _ = research_company(user_id, company_name, force=True)
        preview = "\n".join(
            line for line in content.splitlines()
            if line.strip() and not line.startswith("#") and "last_searched" not in line
        )[:300]
        client.chat_postMessage(
            channel=user_id,
            text=f"✅ *{company_name}* 기업정보가 갱신되었습니다.\n\n```{preview}```",
        )
    except Exception as e:
        client.chat_postMessage(channel=user_id, text=f"⚠️ 기업정보 리서치 실패: {e}")

app.command("/company")(_company_handler)
app.command("/기업")(_company_handler)


def _person_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    args = body.get("text", "").strip().split(maxsplit=1)
    if not args:
        client.chat_postMessage(channel=user_id, text="예: `/person 김민환 카카오`")
        return
    person_name = args[0]
    company_name = args[1] if len(args) > 1 else ""
    client.chat_postMessage(
        channel=user_id,
        text=f"🔍 *{person_name}* 인물정보 리서치 중..." + (f" (소속: {company_name})" if company_name else ""),
    )
    try:
        content, _ = research_person(user_id, person_name, company_name, force=True)
        preview = "\n".join(
            line for line in content.splitlines()
            if line.strip() and not line.startswith("#") and "last_searched" not in line
        )[:300]
        msg = f"✅ *{person_name}* 인물정보가 갱신되었습니다.\n\n```{preview}```"
        if company_name:
            msg += f"\n_(연관 기업정보 *{company_name}* 도 함께 갱신되었습니다)_"
        client.chat_postMessage(channel=user_id, text=msg)
    except Exception as e:
        client.chat_postMessage(channel=user_id, text=f"⚠️ 인물정보 리서치 실패: {e}")

app.command("/person")(_person_handler)
app.command("/인물")(_person_handler)


def _meeting_start_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    title = body.get("text", "").strip() or "미팅"
    start_session(client, user_id=user_id, title=title)

app.command("/미팅시작")(_meeting_start_handler)


def _note_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    note_text = body.get("text", "").strip()
    add_note(client, user_id=user_id, note_text=note_text)

app.command("/메모")(_note_handler)


def _meeting_end_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    end_session(client, user_id=user_id)

app.command("/미팅종료")(_meeting_end_handler)


def _minutes_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    get_minutes_list(client, user_id=user_id)

app.command("/회의록")(_minutes_handler)


# ── Dreamplus 회의실 커맨드 ──────────────────────────────────

def _dreamplus_register_handler(ack, body, client):
    """Dreamplus 계정 등록 — Slack Modal 오픈"""
    ack()
    room_agent.open_register_modal(client, body["trigger_id"])

app.command("/dreamplus")(_dreamplus_register_handler)


def _rooms_handler(ack, body, client):
    """회의실 예약 현황 조회. 날짜 인자 없으면 오늘."""
    ack()
    user_id = body["user_id"]
    date_str = body.get("text", "").strip() or None
    room_agent.cmd_rooms(client, user_id, date_str)

app.command("/회의실")(_rooms_handler)


def _my_reservations_handler(ack, body, client):
    """내 예약 목록 조회"""
    ack()
    user_id = body["user_id"]
    room_agent.cmd_my_reservations(client, user_id)

app.command("/내예약")(_my_reservations_handler)


def _reserve_handler(ack, body, client):
    """회의실 예약. 자연어 파싱 후 가용 회의실 선택 UI 표시.
    예: /회의실예약 내일 오후 2시~4시 2층 KISA 미팅
    """
    ack()
    user_id = body["user_id"]
    text = body.get("text", "").strip()
    if not text:
        client.chat_postMessage(
            channel=user_id,
            text="예: `/회의실예약 내일 오후 2시~4시 8층 KISA 회의`",
        )
        return
    # LLM으로 예약 파라미터 파싱
    client.chat_postMessage(channel=user_id, text=f"📅 예약 정보 분석 중: _{text}_")
    threading.Thread(
        target=_parse_and_reserve,
        args=(client, user_id, text),
        daemon=True,
    ).start()

app.command("/회의실예약")(_reserve_handler)


def _parse_and_reserve(client, user_id: str, text: str):
    """LLM으로 예약 자연어 파싱 후 room_agent.cmd_reserve 호출"""
    import json as _json
    from datetime import datetime as _dt
    import pytz as _pytz
    kst = _pytz.timezone("Asia/Seoul")
    today = _dt.now(kst).strftime("%Y-%m-%d")

    prompt = f"""다음 회의실 예약 요청에서 정보를 추출해줘. 오늘 날짜는 {today}이야.

요청: "{text}"

JSON으로만 반환:
{{
  "date": "YYYY-MM-DD",
  "start_time": "HH:MM",
  "end_time": "HH:MM",
  "floor": "층 번호 (숫자만, 없으면 null)",
  "capacity": "최소 수용 인원 (숫자, 없으면 0)",
  "title": "회의 제목 (없으면 '회의')"
}}

규칙:
- "내일" → 오늘+1일, "모레" → 오늘+2일
- "오후 2시~4시" → start_time: "14:00", end_time: "16:00"
- "1시간" → end_time = start_time + 1시간
- 층 언급 없으면 floor: null"""

    try:
        result = generate_text(prompt)
        cleaned = result.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        params = _json.loads(cleaned)
    except Exception as e:
        log.warning(f"예약 파싱 실패: {e}")
        params = {"date": today, "start_time": "09:00", "end_time": "10:00",
                  "title": "회의", "capacity": 0}

    room_agent.cmd_reserve(client, user_id, params)


def _credits_handler(ack, body, client):
    """남은 크레딧 조회"""
    ack()
    user_id = body["user_id"]
    room_agent.cmd_credits(client, user_id)

app.command("/크레딧")(_credits_handler)


# ── Block Actions ────────────────────────────────────────────

@app.action("confirm_external")
def handle_confirm_external(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    client.chat_postMessage(channel=user_id, text="✅ 외부 미팅으로 확인. 브리핑을 준비합니다.")

@app.action("confirm_internal")
def handle_confirm_internal(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    client.chat_postMessage(channel=user_id, text="⏭️ 내부 미팅으로 처리합니다.")

@app.action("save_contact")
def handle_save_contact(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    client.chat_postMessage(channel=user_id, text="✅ Contacts에 저장되었습니다.")

@app.action("skip_contact")
def handle_skip_contact(ack):
    ack()


@app.action("after_send_minutes")
def handle_after_send_minutes(ack, body, client):
    ack()
    after.handle_send_draft(client, body)


@app.action("after_cancel_minutes")
def handle_after_cancel_minutes(ack, body, client):
    ack()
    after.handle_cancel_draft(client, body)


@app.action("after_done_action_item")
def handle_after_done_action_item(ack, body, client):
    ack()
    after.handle_complete_action_item(client, body)


@app.view("dreamplus_register_modal")
def handle_dreamplus_register_modal(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    view = body["view"]
    room_agent.handle_register_modal(client, user_id, view)


@app.action("room_confirm_reservation")
def handle_room_confirm_reservation(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    value = body["actions"][0]["value"]
    room_agent.handle_confirm_reservation(client, user_id, value)


@app.action("room_cancel_reservation")
def handle_room_cancel_reservation(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    reservation_id = body["actions"][0]["value"]
    room_agent.handle_cancel_reservation(client, user_id, reservation_id)


# ── 명함 OCR 액션 핸들러 ────────────────────────────────────

@app.action("card_confirm_save")
def handle_card_confirm_save(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    card_agent.handle_confirm_save(client, user_id)


@app.action("card_open_edit")
def handle_card_open_edit(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    trigger_id = body["trigger_id"]
    card_agent.open_edit_modal(client, trigger_id, user_id)


@app.action("card_cancel")
def handle_card_cancel(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    card_agent._pending_cards.pop(user_id, None)
    client.chat_postMessage(channel=user_id, text="❌ 명함 저장을 취소했습니다.")


@app.view("card_edit_modal")
def handle_card_edit_modal(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    view = body["view"]
    card_agent.handle_edit_modal_submit(client, user_id, view)


@app.action("select_attendee_email")
def handle_select_attendee_email(ack, body, client):
    ack()
    before.handle_email_selection(client, body)


@app.action("suggest_followup_meeting")
def handle_suggest_followup_meeting(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    title = body["actions"][0].get("value", "후속 미팅")
    client.chat_postMessage(
        channel=user_id,
        text=f"📅 후속 미팅을 생성하려면 `/미팅추가` 명령어를 사용해주세요.\n예: `/미팅추가 {title} 후속 미팅 다음주 월요일 오후 2시`",
    )


# ── FastAPI OAuth 서버 (백그라운드) ──────────────────────────

def _start_oauth_server():
    uvicorn.run(
        oauth_server.app,
        host="0.0.0.0",
        port=int(os.getenv("OAUTH_PORT", "8000")),
        log_level="warning",
    )


# ── 실행 ─────────────────────────────────────────────────────

if __name__ == "__main__":
    # DB 초기화
    user_store.init_db()

    # Slack client를 OAuth 서버에 주입 (Drive 셋업 완료 후 DM 전송용)
    oauth_server.set_slack_client(app.client)

    # FastAPI OAuth 콜백 서버 시작
    oauth_thread = threading.Thread(target=_start_oauth_server, daemon=True)
    oauth_thread.start()
    log.info(f"OAuth 서버 시작 (포트 {os.getenv('OAUTH_PORT', '8000')})")

    # 스케줄러 시작
    scheduler.start()
    log.info("스케줄러 시작 (매일 09:00 KST 자동 브리핑)")

    log.info("Slack 봇 시작 중...")
    SocketModeHandler(app, os.getenv("SLACK_APP_TOKEN")).start()
