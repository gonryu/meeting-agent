"""미팅 에이전트 — Slack Bolt 앱 진입점"""
import json
import os
import logging
import re
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
    create_meeting_from_text,
    update_meeting_from_text,
    update_company_knowledge,
    research_company,
    research_person,
    generate_text,
    handle_company_confirmation,
    _pending_agenda,
    _meeting_drafts,
)
from agents.during import (
    start_session,
    add_note,
    end_session,
    generate_minutes_now,
    check_transcripts,
    get_minutes_list,
    finalize_minutes,
    cancel_minutes,
    request_minutes_edit,
    handle_minutes_edit_reply,
    handle_event_selection,
    handle_event_title_reply,
    _pending_minutes,
    _pending_inputs,
)
from agents import after
from agents import card as card_agent
from agents import dreamplus as dreamplus_agent
from agents import feedback as feedback_agent
from store import user_store
from server import oauth as oauth_server
from tools import stt
from tools import text_extract

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


def scheduled_feedback_digest():
    log.info("피드백 다이제스트 실행")
    feedback_agent.send_feedback_digest(app.client)


from datetime import datetime as _dt
scheduler = BackgroundScheduler(timezone="Asia/Seoul")
scheduler.add_job(scheduled_briefing, "cron", hour=9, minute=0)
scheduler.add_job(scheduled_transcript_check, "interval", minutes=10,
                  next_run_time=_dt.now())
scheduler.add_job(scheduled_action_item_reminder, "cron", hour=8, minute=0)
scheduler.add_job(scheduled_feedback_digest, "cron", hour=8, minute=0)


# ── @멘션 처리 ───────────────────────────────────────────────

@app.event("app_mention")
def handle_mention(event, say, client):
    user_id = event.get("user")
    text = event.get("text", "")
    text = " ".join(word for word in text.split() if not word.startswith("<@")).strip()
    channel = event.get("channel")
    parent_ts = event.get("thread_ts")   # 스레드 답장이면 부모 메시지 ts
    thread_ts = parent_ts or event.get("ts")

    # 스레드 답장 → 일정 업데이트 (브리핑·일정생성 공통)
    log.info(f"handle_mention: parent_ts={parent_ts} draft_keys={list(_meeting_drafts.keys())[:5]}")
    if parent_ts and parent_ts in _meeting_drafts:
        if _check_registered(client, user_id, channel):
            threading.Thread(
                target=update_meeting_from_text,
                args=(client,),
                kwargs=dict(user_id=user_id, user_message=text,
                            channel=channel, thread_ts=parent_ts),
                daemon=True,
            ).start()
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

    # ── 파일 업로드 (이미지: 명함 OCR / 음성: STT 메모 / 텍스트: 회의 메모) ──
    if subtype == "file_share" and event.get("channel_type") == "im" and user_id:
        if _check_registered(client, user_id):
            for f in event.get("files", []):
                mime = f.get("mimetype", "")
                if mime.startswith("image/"):
                    log.info(f"명함 이미지 업로드 감지: user={user_id} file={f.get('id')}")
                    card_agent.handle_image_upload(client, user_id, f)
                elif stt.is_audio(mime):
                    log.info(f"음성 파일 업로드 감지: user={user_id} file={f.get('name')} mime={mime}")
                    threading.Thread(
                        target=_handle_audio_upload,
                        args=(client, user_id, f),
                        daemon=True,
                    ).start()
                elif text_extract.is_text_document(mime):
                    log.info(f"텍스트 문서 업로드 감지: user={user_id} file={f.get('name')} mime={mime}")
                    threading.Thread(
                        target=_handle_text_upload,
                        args=(client, user_id, f),
                        daemon=True,
                    ).start()
        return

    if subtype:
        return

    thread_ts = event.get("thread_ts")
    text = event.get("text", "").strip()
    channel = event.get("channel")
    channel_type = event.get("channel_type")

    log.info(f"handle_message: channel_type={channel_type} thread_ts={thread_ts} draft_keys={list(_meeting_drafts.keys())[:5]}")

    # 스레드 답글 → 일정 업데이트 (브리핑·일정생성 공통)
    if thread_ts and thread_ts in _meeting_drafts:
        if not _check_registered(client, user_id):
            return
        threading.Thread(
            target=update_meeting_from_text,
            args=(client,),
            kwargs=dict(user_id=user_id, user_message=text,
                        channel=channel, thread_ts=thread_ts),
            daemon=True,
        ).start()
        return

    if channel_type == "im":
        if not _check_registered(client, user_id):
            return

        # 회의록 수정 요청 스레드 답글 감지
        if thread_ts and user_id:
            draft = _pending_minutes.get(user_id)
            if draft and draft.get("draft_ts") == thread_ts:
                threading.Thread(
                    target=handle_minutes_edit_reply,
                    args=(client, user_id, text),
                    daemon=True,
                ).start()
                return

        # 이벤트 선택 대기 중 제목 입력 스레드 답글 감지
        if thread_ts and user_id:
            pending = _pending_inputs.get(user_id)
            if pending and pending.get("prompt_ts") == thread_ts and text:
                handle_event_title_reply(client, user_id, text)
                return

        # Trello 토큰 입력 대기 중이면 토큰으로 처리 (return_url 실패 시 폴백)
        if text and oauth_server.is_pending_trello_token(user_id):
            token = text.strip()
            if len(token) > 30 and " " not in token:
                if oauth_server.save_trello_token_from_dm(user_id, token):
                    client.chat_postMessage(
                        channel=user_id,
                        text="✅ Trello 계정이 연결되었습니다! 이제 브리핑에서 Trello 카드 정보를 볼 수 있습니다.",
                    )
                else:
                    client.chat_postMessage(
                        channel=user_id,
                        text="❌ Trello 토큰 저장에 실패했습니다. `/trello` 로 다시 시도해주세요.",
                    )
                return

        _route_message(text, client, user_id=user_id, user_msg_ts=event.get("ts"))


_HELP_TEXT = """*🤖 ParaMee 사용 가이드*

*📅 일정 관리*
• `내일 3시에 한국은행 미팅 잡아줘` — 일정 생성
  └ 업체 지정: _"업체는 한국은행이야"_ 처럼 명시 (없으면 내부 회의)
  └ 생성 후 스레드 답글로 제목·참석자·시간·장소·어젠다 수정 가능
• `/브리핑` or `브리핑 해줘` — 오늘 미팅 브리핑

*🎙️ 회의 진행*
• `/미팅시작 [제목]` or `미팅 시작해줘` — 회의 시작 (메모 세션 시작)
• `/메모 [내용]` or `메모: [내용]` — 회의 중 메모 추가 (세션 자동 시작)
  └ 캘린더 일정 자동 감지, 일정이 여러 개면 선택 UI 제공
• 🎙️ 음성 파일 업로드 — STT 변환 후 메모로 자동 등록
• 📄 텍스트 문서 업로드 — 텍스트 추출 후 메모로 자동 등록
• `/미팅종료` or `미팅 종료` — 회의 종료 및 회의록 자동 생성
• `/회의록작성` or `회의록 작성해줘` — 현재 세션 기반 회의록 즉시 생성
• `/회의록` or `회의록 보여줘` — 저장된 회의록 목록 조회

*🏢 드림플러스 회의실*
• `/회의실예약 [시간]` or `내일 2시에 회의실 잡아줘` — 회의실 예약
  └ 층수·수용인원·시간 지정 가능: _"오늘 3시 2시간 8층 6인실"_
• `/회의실조회` or `내 회의실 예약 현황` — 예약 목록 조회
• `/회의실취소` or `회의실 예약 취소해줘` — 예약 취소
• `/크레딧조회` — 드림플러스 잔여 포인트 조회
• `/드림플러스설정` — 드림플러스 계정 등록/변경

*🔍 리서치*
• `한국은행 알아봐줘` — 업체 정보 및 최근 동향 조사
• `홍길동 한국은행 인물 조사해줘` — 담당자 정보 조사

*⚙️ 설정*
• `/등록` — Google 계정 연결 (최초 1회)
• `/재등록` — Google 계정 재연결 (스코프 갱신)
• `/업데이트` — 내부 서비스 지식 갱신

*📝 피드백*
• `~기능 추가해줘` / `~개선해줘` / `~버그 같아` — 피드백 접수 (매일 아침 관리자에게 전달)

*💡 도움말*
• `/도움말` or `도움말` or `help` — 이 메시지 표시"""


_INTENT_PROMPT = """사용자의 Slack 메시지를 분석해서 의도(intent)를 분류해줘.

메시지: "{text}"

가능한 intent 목록:
- briefing: 브리핑 요청 (예: "브리핑 해줘", "오늘 미팅 현황", "brief", "이번주 일정 브리핑", "앞으로 3일 일정")
- create_meeting: 미팅/일정 생성 (예: "내일 3시에 KISA 미팅 잡아줘", "오늘 15시 홍길동 회의 만들어줘")
- start_session: 미팅 시작 (예: "미팅 시작", "회의 시작해줘", "지금부터 KISA 회의 시작")
- add_note: 메모 추가 — 현재 진행 중인 회의에 내용 기록 (예: "메모: 예산 협의됨", "기록해줘 다음달 계약 예정", "노트 추가")
- end_session: 미팅 종료 (예: "미팅 종료", "회의 끝났어", "미팅 마무리해줘")
- generate_minutes: 회의록 작성 요청 (예: "회의록 작성해줘", "회의록 만들어줘", "회의록 생성")
- get_minutes: 저장된 회의록 목록 조회 (예: "회의록 보여줘", "지난 회의록", "minutes 목록")
- research_company: 특정 업체 정보 조사 (예: "KISA 알아봐줘", "삼성전자 정보 검색해줘", "카카오 최근 동향")
- research_person: 특정 인물 정보 조사 (예: "홍길동 인물 정보", "김민환 누구야", "이준호 카카오 담당자 조사해줘")
- update_knowledge: 내부 서비스 지식 갱신 (예: "knowledge 업데이트", "서비스 정보 갱신")
- dreamplus_book: 드림플러스 회의실 예약 (예: "회의실 예약해줘", "내일 2시에 회의실 잡아줘", "드림플러스 3시간 예약", "회의실 오늘 오후 3시부터 5시 2명")
- dreamplus_list: 드림플러스 예약 현황 조회 (예: "예약 현황 보여줘", "회의실 예약 목록", "드림플러스 예약 확인", "내 회의실 예약")
- dreamplus_cancel: 드림플러스 예약 취소 (예: "회의실 예약 취소", "드림플러스 예약 취소해줘")
- dreamplus_credits: 드림플러스 크레딧/포인트 조회 (예: "크레딧 얼마나 남았어", "포인트 확인", "드림플러스 크레딧 조회")
- dreamplus_settings: 드림플러스 계정 설정 (예: "드림플러스 설정", "드림플러스 로그인 정보 등록", "드림플러스 계정 등록")
- feedback: 기능 요청·개선 제안·버그 리포트 (예: "~기능 추가해줘", "~이렇게 개선해줘", "~가 안 돼 버그 같아", "~기능 넣어줘", "~가 불편해", "~해줬으면 좋겠어", "~도 지원해줘")
- help: 도움말/사용법 요청 (예: "도움말", "help", "사용법", "뭘 할 수 있어", "어떻게 써")
- unknown: 위 중 해당 없음

params 추출 규칙:
- briefing: {{"days": N}} — 기간 추출
  - "브리핑 해줘" / "오늘 미팅 현황" → {{"days": 1}}
  - "이번주 일정 브리핑" → {{"days": 7}}
  - "앞으로 3일 일정" → {{"days": 3}}
  - "이번달 일정" → {{"days": 30}}
  - 기간 언급 없으면 {{"days": 1}}
- create_meeting: params 없음 (원본 메시지 전체를 그대로 사용)
- start_session: {{"title": "미팅 제목 (없으면 빈 문자열)"}}
- add_note: {{"note": "메모 내용 ('메모:', '기록해줘' 등 트리거 단어 제거 후)"}}
- research_company: {{"company": "업체명"}}
- research_person: {{"person": "이름", "company": "소속 업체명 (없으면 빈 문자열)"}}
- dreamplus_book: {{"text": "원본 메시지 그대로"}}
- dreamplus_cancel: {{"text": "원본 메시지 그대로"}}

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


def _route_message(text: str, client, user_id: str, channel: str = None,
                   thread_ts: str = None, user_msg_ts: str = None):
    log.info(f"메시지 라우팅 ({user_id}): {text}")

    intent_data = _classify_intent(text)
    intent = intent_data.get("intent", "unknown")
    params = intent_data.get("params", {})
    log.info(f"인텐트 분류: {intent} / params: {params}")


    if intent == "briefing":
        try:
            days = max(1, min(int(params.get("days", 1)), 30))
        except (ValueError, TypeError):
            days = 1
        run_briefing(client, user_id=user_id, channel=channel, thread_ts=thread_ts, days=days)

    elif intent == "create_meeting":
        create_meeting_from_text(client, user_id=user_id, user_message=text,
                                 channel=channel, thread_ts=thread_ts,
                                 user_msg_ts=user_msg_ts)

    elif intent == "start_session":
        title = params.get("title", "").strip() or "미팅"
        start_session(client, user_id=user_id, title=title)

    elif intent == "add_note":
        note = params.get("note", "").strip() or text
        add_note(client, user_id=user_id, note_text=note)

    elif intent == "end_session":
        end_session(client, user_id=user_id)

    elif intent == "generate_minutes":
        threading.Thread(
            target=generate_minutes_now,
            args=(client, user_id),
            daemon=True,
        ).start()

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

    elif intent == "dreamplus_book":
        dreamplus_agent.book_room(client, user_id=user_id, text=params.get("text", text),
                                  channel=channel, thread_ts=thread_ts)

    elif intent == "dreamplus_list":
        dreamplus_agent.list_reservations(client, user_id=user_id,
                                          channel=channel, thread_ts=thread_ts)

    elif intent == "dreamplus_cancel":
        dreamplus_agent.cancel_room(client, user_id=user_id, text=params.get("text", text),
                                    channel=channel, thread_ts=thread_ts)

    elif intent == "dreamplus_credits":
        dreamplus_agent.show_credits(client, user_id=user_id,
                                     channel=channel, thread_ts=thread_ts)

    elif intent == "dreamplus_settings":
        # 자연어로 설정 요청 시 — trigger_id가 없으므로 안내 메시지
        client.chat_postMessage(
            channel=channel or user_id,
            thread_ts=thread_ts,
            text="드림플러스 계정 설정은 `/드림플러스설정` 명령어를 사용해주세요.",
        )

    elif intent == "feedback":
        feedback_agent.handle_feedback(client, user_id=user_id, text=text,
                                       channel=channel, thread_ts=thread_ts)

    elif intent == "help":
        client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts, text=_HELP_TEXT)

    else:
        client.chat_postMessage(
            channel=channel or user_id,
            thread_ts=thread_ts,
            text=f"'{text[:30]}' 명령을 이해하지 못했어요. `도움말` 또는 `/도움말`로 사용 가능한 명령어를 확인하세요.",
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


def _handle_audio_upload(client, user_id: str, file_info: dict):
    """음성 파일 업로드 처리 — STT 변환 후 메모로 등록 (세션 자동 시작)"""
    filename = file_info.get("name", "audio")
    mime = file_info.get("mimetype", "audio/mpeg")
    file_url = file_info.get("url_private_download") or file_info.get("url_private")

    client.chat_postMessage(
        channel=user_id,
        text=f"🎙️ *{filename}* 음성 변환 중...",
    )

    try:
        slack_token = os.getenv("SLACK_BOT_TOKEN", "")
        text = stt.transcribe(file_url, slack_token, mime_type=mime, filename=filename)
    except Exception as e:
        log.error(f"STT 실패 ({user_id}): {e}")
        client.chat_postMessage(channel=user_id, text=f"⚠️ 음성 변환 실패: {e}")
        return

    if not text:
        client.chat_postMessage(channel=user_id, text="⚠️ 음성에서 텍스트를 추출하지 못했습니다.")
        return

    # 세션 자동 시작 + 메모 등록
    add_note(client, user_id=user_id, note_text=text, input_type="audio")


def _handle_text_upload(client, user_id: str, file_info: dict):
    """텍스트 문서 업로드 처리 — 텍스트 추출 후 메모로 등록 (세션 자동 시작)"""
    filename = file_info.get("name", "document")

    client.chat_postMessage(
        channel=user_id,
        text=f"📄 *{filename}* 텍스트 추출 중...",
    )

    try:
        slack_token = os.getenv("SLACK_BOT_TOKEN", "")
        text = text_extract.extract_text(file_info, slack_token)
    except Exception as e:
        log.error(f"텍스트 추출 실패 ({user_id}): {e}")
        client.chat_postMessage(channel=user_id, text=f"⚠️ 텍스트 추출 실패: {e}")
        return

    if not text:
        client.chat_postMessage(channel=user_id,
                                text=f"⚠️ *{filename}*에서 텍스트를 추출하지 못했습니다.")
        return

    # 길이 제한 안내
    if len(text) > 50000:
        client.chat_postMessage(channel=user_id,
                                text=f"📄 *{filename}* 텍스트가 길어 ({len(text):,}자) 앞부분 50,000자만 사용합니다.")
        text = text[:50000]

    note_prefix = f"[문서: {filename}] "
    add_note(client, user_id=user_id, note_text=note_prefix + text, input_type="document")


def _meeting_end_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    end_session(client, user_id=user_id)

app.command("/미팅종료")(_meeting_end_handler)


def _generate_minutes_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    threading.Thread(
        target=generate_minutes_now,
        args=(client, user_id),
        daemon=True,
    ).start()

app.command("/회의록작성")(_generate_minutes_handler)


def _minutes_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    get_minutes_list(client, user_id=user_id)

app.command("/회의록")(_minutes_handler)


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


@app.action("trello_register")
def handle_trello_register(ack, body, client):
    ack()
    threading.Thread(
        target=after.handle_trello_register, args=(client, body), daemon=True
    ).start()


@app.action("trello_skip")
def handle_trello_skip(ack, body, client):
    ack()
    after.handle_trello_skip(client, body)


# ── 미팅 이벤트 선택 액션 핸들러 ─────────────────────────────

def _handle_meeting_event_select(ack, body, client):
    """캘린더 이벤트 선택 버튼 콜백"""
    ack()
    user_id = body["user"]["id"]
    action = body.get("actions", [{}])[0]
    action_id = action.get("action_id", "")

    if action_id == "select_meeting_event_new":
        # "새 미팅으로 기록" — 제목 입력 안내
        pending = _pending_inputs.get(user_id)
        if pending:
            client.chat_postMessage(
                channel=user_id,
                thread_ts=pending.get("prompt_ts"),
                text="📝 미팅 제목을 이 스레드에 답글로 입력해주세요. (예: 'KISA 보안 미팅')",
            )
        else:
            handle_event_selection(client, user_id, selected_event_id=None)
    else:
        # 특정 이벤트 선택
        selected_event_id = action.get("value", "")
        handle_event_selection(client, user_id, selected_event_id=selected_event_id)

for i in range(5):
    app.action(f"select_meeting_event_{i}")(_handle_meeting_event_select)
app.action("select_meeting_event_new")(_handle_meeting_event_select)


# ── 회의록 검토 액션 핸들러 ─────────────────────────────────

@app.action("minutes_confirm")
def handle_minutes_confirm(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    threading.Thread(
        target=finalize_minutes,
        args=(client, user_id),
        daemon=True,
    ).start()


@app.action("minutes_edit_request")
def handle_minutes_edit_request(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    request_minutes_edit(client, user_id)


@app.action("minutes_cancel")
def handle_minutes_cancel(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    cancel_minutes(client, user_id)


@app.action("minutes_open_doc")
def handle_minutes_open_doc(ack, body, client):
    ack()  # URL 버튼 — 브라우저에서 열림, 별도 처리 불필요


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


@app.action(re.compile(r"^confirm_company_|^company_checkboxes$"))
def handle_confirm_company_name(ack, body, client):
    ack()
    handle_company_confirmation(client, body)


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


# ── 드림플러스 커맨드 ────────────────────────────────────────

def _dp_settings_handler(ack, body, client):
    ack()
    if not _check_registered(client, body["user_id"]):
        return
    dreamplus_agent.open_settings_modal(client, body["trigger_id"], body["user_id"])

app.command("/드림플러스설정")(_dp_settings_handler)
app.command("/dreamplus")(_dp_settings_handler)


@app.view(dreamplus_agent._MODAL_CALLBACK)
def handle_dp_settings_modal(ack, body, client):
    ack()
    dreamplus_agent.handle_settings_modal(client, body)


# ── Trello 설정 ─────────────────────────────────────────────

def _trello_setup_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    try:
        auth_url = oauth_server.build_trello_auth_url(user_id)
        client.chat_postMessage(
            channel=user_id,
            text=(
                f"🔗 *Trello 계정 연결*\n\n"
                f"아래 링크를 클릭하여 Trello 접근을 허용하세요.\n{auth_url}\n\n"
                f"승인 후 표시되는 토큰을 복사하여 이 DM에 붙여넣어 주세요."
            ),
        )
    except Exception as e:
        client.chat_postMessage(
            channel=user_id,
            text=f"❌ Trello 인증 URL 생성 실패: {e}",
        )

app.command("/trello")(_trello_setup_handler)


def _trello_disconnect_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    from store import user_store
    from tools import trello
    user_store.clear_trello_token(user_id)
    trello.clear_user_cache(user_id)
    client.chat_postMessage(
        channel=user_id,
        text="✅ Trello 연결이 해제되었습니다.",
    )

app.command("/trello-disconnect")(_trello_disconnect_handler)


def _dp_book_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    threading.Thread(
        target=dreamplus_agent.book_room,
        args=(client, user_id, body.get("text", "")),
        daemon=True,
    ).start()

app.command("/회의실예약")(_dp_book_handler)


def _dp_list_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    threading.Thread(
        target=dreamplus_agent.list_reservations,
        args=(client, user_id),
        daemon=True,
    ).start()

app.command("/회의실조회")(_dp_list_handler)


def _dp_cancel_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    threading.Thread(
        target=dreamplus_agent.cancel_room,
        args=(client, user_id, body.get("text", "")),
        daemon=True,
    ).start()

app.command("/회의실취소")(_dp_cancel_handler)


def _dp_credits_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    threading.Thread(
        target=dreamplus_agent.show_credits,
        args=(client, user_id),
        daemon=True,
    ).start()

app.command("/크레딧조회")(_dp_credits_handler)


# ── /도움말 ──────────────────────────────────────────────────

def _help_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    client.chat_postMessage(channel=user_id, text=_HELP_TEXT)


app.command("/도움말")(_help_handler)
app.command("/help")(_help_handler)


@app.action("dreamplus_book_room")
def handle_dp_book_room(ack, body, client):
    ack()
    threading.Thread(
        target=dreamplus_agent.confirm_room_booking,
        args=(client, body),
        daemon=True,
    ).start()


@app.action("dreamplus_cancel_confirm")
def handle_dp_cancel_confirm(ack, body, client):
    ack()
    threading.Thread(
        target=dreamplus_agent.confirm_cancel,
        args=(client, body),
        daemon=True,
    ).start()


@app.action("dreamplus_next_rooms")
def handle_dp_next_rooms(ack, body, client):
    ack()
    dreamplus_agent.next_rooms(client, body)


@app.action("dreamplus_prev_rooms")
def handle_dp_prev_rooms(ack, body, client):
    ack()
    dreamplus_agent.prev_rooms(client, body)


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
