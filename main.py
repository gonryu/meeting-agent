"""미팅 에이전트 — Slack Bolt 앱 진입점"""
import json
import os
import logging
import re
import threading
from datetime import datetime
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
    handle_email_selection,
    cancel_meeting_from_text,
    suggest_meeting_slots,
    handle_meeting_cancel_confirm,
    handle_meeting_cancel_abort,
    handle_meeting_cancel_with_room,
    handle_meeting_cancel_event_only,
    handle_meeting_cancel_abort_both,
    handle_slot_create_meeting,
    handle_create_confirm,
    handle_create_abort,
    handle_room_offer_show,
    handle_room_offer_skip,
    _pending_agenda,
    _meeting_drafts,
    _pending_create_confirm,
    _pending_room_offer,
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
    handle_minutes_source_select,
    handle_event_selection,
    handle_event_title_reply,
    _pending_minutes,
    _pending_inputs,
    _find_draft_for_user,
    find_draft_by_thread_ts,
    get_session_thread,
)
from agents import after
from agents import card as card_agent
from agents import dreamplus as dreamplus_agent
from agents import feedback as feedback_agent
from agents import proposal as proposal_agent
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
        text=f"⚠️ 먼저 Google 계정을 연결해주세요. 아래 링크에서 인증을 완료하면 자동으로 등록됩니다.",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"⚠️ 먼저 Google 계정을 연결해주세요.\n<{auth_url}|🔗 인증을 완료하면 자동으로 등록됩니다.>"}}],
    )
    return False


# ── 버튼 권한 검증 헬퍼 (I5) ─────────────────────────────────

def _ensure_creator(client, body: dict, expected_user_id: str | None) -> bool:
    """채널/스레드에 노출된 버튼을 요청자(생성자) 본인만 누를 수 있도록 가드.
    expected_user_id가 None이거나 클릭한 사용자와 일치하면 True.
    불일치 시 ephemeral 안내 후 False."""
    clicker = body.get("user", {}).get("id")
    if not expected_user_id or expected_user_id == clicker:
        return True
    try:
        client.chat_postEphemeral(
            channel=body.get("container", {}).get("channel_id") or clicker,
            user=clicker,
            text="⚠️ 이 작업은 요청자 본인만 진행할 수 있습니다.",
        )
    except Exception as e:
        log.warning(f"권한 거부 ephemeral 발송 실패: {e}")
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
scheduler.add_job(scheduled_feedback_digest, "cron", hour=22, minute=0)


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

    # 스레드 답장 → 미팅 세션 (미팅종료 외 모든 입력은 메모로 처리)
    if parent_ts:
        session_thread = get_session_thread(user_id)
        if session_thread and session_thread == (channel, parent_ts):
            if _check_registered(client, user_id, channel):
                _end_keywords = {"미팅종료", "미팅 종료", "회의 끝", "회의 종료", "미팅 마무리"}
                if any(text.strip().startswith(kw) for kw in _end_keywords):
                    end_session(client, user_id=user_id,
                                channel=channel, thread_ts=parent_ts)
                else:
                    add_note(client, user_id=user_id, note_text=text.strip(),
                             channel=channel, thread_ts=parent_ts)
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

        # 회의록 수정 요청 스레드 답글 감지 — thread_ts로 정확한 초안 매칭 (B3)
        if thread_ts and user_id:
            found = find_draft_by_thread_ts(user_id, thread_ts)
            if found:
                threading.Thread(
                    target=handle_minutes_edit_reply,
                    args=(client, user_id, text),
                    kwargs=dict(thread_ts=thread_ts),
                    daemon=True,
                ).start()
                return

        # 제안서 개요/초안 수정 스레드 답글 감지 (Phase 2.4)
        if thread_ts and user_id:
            proposal_state = proposal_agent.get_pending_proposal(user_id)
            if proposal_state:
                # 개요 수정 스레드
                if proposal_state.get("outline_ts") == thread_ts:
                    threading.Thread(
                        target=proposal_agent.handle_proposal_outline_edit_reply,
                        args=(client, user_id, text),
                        daemon=True,
                    ).start()
                    return
                # 제안서 초안 수정 스레드
                if proposal_state.get("draft_ts") == thread_ts:
                    threading.Thread(
                        target=proposal_agent.handle_proposal_edit_reply,
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
  └ `/회의록 카카오` — 업체 기반 검색  |  `/회의록 2026-03` — 기간 기반 검색
  └ `카카오 지난달 회의록 찾아줘` — 자연어 검색

*🏢 드림플러스 회의실*
• `/회의실예약 [시간]` or `내일 2시에 회의실 잡아줘` — 회의실 예약
  └ 층수·수용인원·시간 지정 가능: _"오늘 3시 2시간 8층 6인실"_
• `/회의실조회` or `내 회의실 예약 현황` — 예약 목록 조회
• `/회의실취소` or `회의실 예약 취소해줘` — 예약 취소
• `/크레딧조회` — 드림플러스 잔여 포인트 조회
• `/드림플러스` — 드림플러스 계정 등록/변경

*🔍 리서치*
• `한국은행 알아봐줘` — 업체 정보 및 최근 동향 조사
• `홍길동 한국은행 인물 조사해줘` — 담당자 정보 조사

*📋 Trello 연동*
• `/trello` — Trello 계정 연결
  └ 브리핑 시 업체 카드의 미완료 액션아이템 표시
  └ 회의록 완료 후 액션아이템 + 회의록 요약을 카드에 자동 등록 제안
• `/트렐로조회` or `트렐로 카드 보여줘` — Trello 카드 목록 조회
  └ `/트렐로조회 삼성` or `삼성 트렐로 카드` — 업체명으로 카드 검색

*📝 업체 메모*
• `카카오 메모 — PoC 예산 확보` — 업체 파일에 메모 추가
  └ 업체명 + "메모" 키워드 + 내용으로 자동 분류

*⚙️ 설정*
• `/등록` — Google 계정 연결 (최초 1회)
• `/재등록` — Google 계정 재연결 (스코프 갱신)
• `/trello` — Trello 계정 연결
• `/드림플러스` — 드림플러스 계정 등록/변경
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
- cancel_meeting: 캘린더 일정 취소 — 드림플러스 회의실 예약 취소가 아니라 *Google Calendar 미팅*을 취소 (예: "내일 3시 카카오 미팅 취소해줘", "오늘 KISA 회의 삭제", "4/18 회의 지워줘")
- suggest_slots: 여러 참석자의 빈 시간대 추천 (예: "김민환, 홍길동이랑 다음주에 1시간 미팅 가능한 시간 찾아줘", "이번주 중에 팀 전체 2시간 비는 시간 알려줘")
- start_session: 미팅 시작 (예: "미팅 시작", "회의 시작해줘", "지금부터 KISA 회의 시작")
- add_note: 메모 추가 — 현재 진행 중인 회의에 내용 기록 (예: "메모: 예산 협의됨", "기록해줘 다음달 계약 예정", "노트 추가")
- end_session: 미팅 종료 (예: "미팅 종료", "회의 끝났어", "미팅 마무리해줘")
- generate_minutes: 회의록 작성 요청 (예: "회의록 작성해줘", "회의록 만들어줘", "회의록 생성")
- get_minutes: 회의록 조회·검색 (예: "회의록 보여줘", "회의록 목록", "지난 목요일 회의록", "4월 13일 회의록", "카카오 회의록 찾아줘", "지난달 회의록", "삼성전자 3월 회의록", "이번주 회의록")
- research_company: 특정 업체 정보 조사 (예: "KISA 알아봐줘", "삼성전자 정보 검색해줘", "카카오 최근 동향")
- research_person: 특정 인물 정보 조사 (예: "홍길동 인물 정보", "김민환 누구야", "이준호 카카오 담당자 조사해줘")
- update_knowledge: 내부 서비스 지식 갱신 (예: "knowledge 업데이트", "서비스 정보 갱신")
- dreamplus_book: 드림플러스 회의실 예약 (예: "회의실 예약해줘", "내일 2시에 회의실 잡아줘", "드림플러스 3시간 예약", "회의실 오늘 오후 3시부터 5시 2명")
- dreamplus_list: 드림플러스 예약 현황 조회 (예: "예약 현황 보여줘", "회의실 예약 목록", "드림플러스 예약 확인", "내 회의실 예약")
- dreamplus_cancel: 드림플러스 예약 취소 (예: "회의실 예약 취소", "드림플러스 예약 취소해줘")
- dreamplus_credits: 드림플러스 크레딧/포인트 조회 (예: "크레딧 얼마나 남았어", "포인트 확인", "드림플러스 크레딧 조회")
- dreamplus_settings: 드림플러스 계정 설정 (예: "드림플러스 설정", "드림플러스 로그인 정보 등록", "드림플러스 계정 등록")
- trello_search: Trello 카드 조회/검색 (예: "트렐로 카드 보여줘", "트렐로 조회", "KISA 트렐로 카드", "트렐로에서 삼성 찾아줘", "Trello 카드 목록")
- search_minutes: (사용하지 않음 — get_minutes로 통합됨)
- company_memo: 업체 관련 메모 저장 (예: "카카오 메모 — PoC 예산 확보", "삼성 메모: 담당자 변경됨", "KISA 관련 메모 저장: 내달 계약 예정")
- feedback: 기능 요청·개선 제안·버그 리포트 (예: "~기능 추가해줘", "~이렇게 개선해줘", "~가 안 돼 버그 같아", "~기능 넣어줘", "~가 불편해", "~해줬으면 좋겠어", "~도 지원해줘")
- help: 도움말/사용법 요청 (예: "도움말", "help", "사용법", "뭘 할 수 있어", "어떻게 써")
- unknown: 위 중 해당 없음

params 추출 규칙:
- briefing: {{"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "period_text": "표시용 기간 텍스트"}} — 자연어 기간을 날짜 범위로 변환
  - 오늘 날짜: {today}  (요일: {weekday})
  - 어떤 자연어 기간 표현이든 start_date/end_date 날짜 범위로 변환
  - 주(week)의 기준: 일요일~토요일
  - "이번주"는 이번주 토요일까지, "일주일"/"7일간"은 오늘 기준 7일간 (다름!)
  - 기간 언급 없으면 → start_date와 end_date 모두 null, period_text="향후 24시간" (기본값)
- create_meeting: params 없음 (원본 메시지 전체를 그대로 사용)
- cancel_meeting: params 없음 (원본 메시지 전체를 그대로 사용)
- suggest_slots: params 없음 (원본 메시지 전체를 그대로 사용)
- start_session: {{"title": "미팅 제목 (없으면 빈 문자열)"}}
- add_note: {{"note": "메모 내용 ('메모:', '기록해줘' 등 트리거 단어 제거 후)"}}
- research_company: {{"company": "업체명"}}
- research_person: {{"person": "이름", "company": "소속 업체명 (없으면 빈 문자열)"}}
- trello_search: {{"query": "검색할 업체명 키워드 (전체 목록 조회 시 빈 문자열)"}}
- get_minutes: {{"query": "검색 키워드 (업체명, 날짜, 기간 등 원본 그대로 — 단순 목록 조회면 빈 문자열)"}}
- company_memo: {{"company": "업체명", "memo": "메모 내용"}}
- dreamplus_book: {{"text": "원본 메시지 그대로"}}
- dreamplus_cancel: {{"text": "원본 메시지 그대로"}}

JSON으로만 반환 (설명 없이):
{{"intent": "...", "params": {{}}}}"""


def _classify_intent(text: str) -> dict:
    """LLM으로 사용자 메시지 의도 분류. 실패 시 unknown 반환."""
    try:
        from zoneinfo import ZoneInfo
        _now = datetime.now(ZoneInfo("Asia/Seoul"))
        _weekday_names = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
        result = generate_text(_INTENT_PROMPT.format(
            text=text.replace('"', "'"),
            today=_now.strftime("%Y-%m-%d"),
            weekday=_weekday_names[_now.weekday()],
        ))
        # JSON 파싱 — 마크다운 코드블록 제거
        cleaned = result.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(cleaned)
    except Exception as e:
        log.warning(f"인텐트 분류 실패: {e} / 원문: {result if 'result' in dir() else '?'}")
        return {"intent": "unknown", "params": {}}


# ── 회의록 검색 헬퍼 (FR-D11/D12) ─────────────────────────────

def _search_minutes(client, *, user_id: str, query: str,
                    channel: str = None, thread_ts: str = None):
    """업체명·회의명·기간 기반 회의록 검색 (Drive 파일명 기반)"""
    from tools import drive as _drive

    creds = user_store.get_credentials(user_id)
    if not creds:
        client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text="⚠️ Google 인증이 필요합니다. `/등록`으로 먼저 인증해주세요.")
        return

    user = user_store.get_user(user_id)
    minutes_folder_id = user.get("minutes_folder_id") if user else None
    if not minutes_folder_id:
        client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text="⚠️ Minutes 폴더가 설정되지 않았습니다. `/재등록`으로 재인증해주세요.")
        return

    # 기간 파싱
    date_from = date_to = None
    keyword = query
    from zoneinfo import ZoneInfo
    _now = datetime.now(ZoneInfo("Asia/Seoul"))

    def _weekday_kr(name: str) -> int:
        """요일 이름 → 0=월 ... 6=일"""
        return {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}.get(name, -1)

    def _last_weekday(weekday: int) -> str:
        """가장 최근 해당 요일의 날짜 (YYYY-MM-DD)"""
        days_ago = (_now.weekday() - weekday) % 7
        if days_ago == 0:
            days_ago = 7  # "지난 월요일"이면 오늘이 월요일이라도 지난주
        from datetime import timedelta
        return (_now - timedelta(days=days_ago)).strftime("%Y-%m-%d")

    # YYYY-MM-DD ~ YYYY-MM-DD 범위
    range_match = re.search(r'(\d{4}-\d{2}-\d{2})\s*[~\-]\s*(\d{4}-\d{2}-\d{2})', query)
    if range_match:
        date_from = range_match.group(1)
        date_to = range_match.group(2)
        keyword = query[:range_match.start()].strip() + " " + query[range_match.end():].strip()

    # YYYY-MM 월 단위
    elif month_match := re.search(r'(\d{4})-(\d{2})(?!\d)', query):
        year, month = month_match.group(1), month_match.group(2)
        import calendar as _cal_mod
        last_day = _cal_mod.monthrange(int(year), int(month))[1]
        date_from = f"{year}-{month}-01"
        date_to = f"{year}-{month}-{last_day:02d}"
        keyword = query[:month_match.start()].strip() + " " + query[month_match.end():].strip()

    # M/D 또는 M월 D일 (올해 기준)
    elif md_match := re.search(r'(\d{1,2})/(\d{1,2})', query):
        m, d = int(md_match.group(1)), int(md_match.group(2))
        if 1 <= m <= 12 and 1 <= d <= 31:
            target = f"{_now.year}-{m:02d}-{d:02d}"
            date_from = date_to = target
            keyword = query[:md_match.start()].strip() + " " + query[md_match.end():].strip()
    elif md_match2 := re.search(r'(\d{1,2})월\s*(\d{1,2})일', query):
        m, d = int(md_match2.group(1)), int(md_match2.group(2))
        if 1 <= m <= 12 and 1 <= d <= 31:
            target = f"{_now.year}-{m:02d}-{d:02d}"
            date_from = date_to = target
            keyword = query[:md_match2.start()].strip() + " " + query[md_match2.end():].strip()

    # "지난 월요일", "지난 화요일" 등
    elif wd_match := re.search(r'지난\s*(월|화|수|목|금|토|일)요일', query):
        wd = _weekday_kr(wd_match.group(1))
        if wd >= 0:
            target = _last_weekday(wd)
            date_from = date_to = target
            keyword = query[:wd_match.start()].strip() + " " + query[wd_match.end():].strip()

    # "오늘", "어제"
    elif "어제" in query:
        from datetime import timedelta
        target = (_now - timedelta(days=1)).strftime("%Y-%m-%d")
        date_from = date_to = target
        keyword = query.replace("어제", "").strip()
    elif "오늘" in query:
        target = _now.strftime("%Y-%m-%d")
        date_from = date_to = target
        keyword = query.replace("오늘", "").strip()

    # "지난주"
    elif "지난주" in query or "저번주" in query:
        from datetime import timedelta
        # 지난주 월~일
        last_mon = _now - timedelta(days=_now.weekday() + 7)
        last_sun = last_mon + timedelta(days=6)
        date_from = last_mon.strftime("%Y-%m-%d")
        date_to = last_sun.strftime("%Y-%m-%d")
        keyword = query.replace("지난주", "").replace("저번주", "").strip()
    elif "이번주" in query:
        from datetime import timedelta
        this_mon = _now - timedelta(days=_now.weekday())
        this_sun = this_mon + timedelta(days=6)
        date_from = this_mon.strftime("%Y-%m-%d")
        date_to = this_sun.strftime("%Y-%m-%d")
        keyword = query.replace("이번주", "").strip()

    # "지난달", "이번달"
    elif "지난달" in query or "저번달" in query:
        from dateutil.relativedelta import relativedelta
        prev = _now - relativedelta(months=1)
        import calendar as _cal_mod
        last_day = _cal_mod.monthrange(prev.year, prev.month)[1]
        date_from = prev.strftime("%Y-%m-01")
        date_to = prev.strftime(f"%Y-%m-{last_day:02d}")
        keyword = query.replace("지난달", "").replace("저번달", "").strip()
    elif "이번달" in query or "이번 달" in query:
        import calendar as _cal_mod
        last_day = _cal_mod.monthrange(_now.year, _now.month)[1]
        date_from = _now.strftime("%Y-%m-01")
        date_to = _now.strftime(f"%Y-%m-{last_day:02d}")
        keyword = query.replace("이번달", "").replace("이번 달", "").strip()

    # 불필요한 토큰 제거 → 검색 키워드만 남김
    for token in ["회의록", "검색", "찾아줘", "보여줘", "조회", "찾아", "검색해줘"]:
        keyword = keyword.replace(token, "")
    keyword = keyword.strip()

    # Drive에서 회의록 파일 목록 조회
    try:
        files = _drive.list_minutes(creds, minutes_folder_id)
    except Exception as e:
        client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text=f"⚠️ 회의록 조회 실패: {e}")
        return

    # 파일명 패턴: {YYYY-MM-DD}_{제목}_내부용.md / 외부용.md
    results = []
    for f in files:
        name = f.get("name", "")
        # 날짜 추출 (파일명 앞 10자)
        file_date = name[:10] if len(name) >= 10 and re.match(r'\d{4}-\d{2}-\d{2}', name) else ""
        # 제목 추출 (날짜_ 이후, _내부용/_외부용 이전)
        title_part = re.sub(r'^\d{4}-\d{2}-\d{2}_', '', name)
        title_part = re.sub(r'_(내부용|외부용)\.md$', '', title_part)

        # 기간 필터
        if date_from and file_date and file_date < date_from:
            continue
        if date_to and file_date and file_date > date_to:
            continue

        # 키워드 필터 (업체명·회의명 — 파일명에서 검색)
        if keyword and keyword.lower() not in name.lower():
            continue

        file_id = f.get("id", "")
        link = f"https://drive.google.com/file/d/{file_id}/view" if file_id else ""
        suffix = ""
        if "_내부용.md" in name:
            suffix = " (내부용)"
        elif "_외부용.md" in name:
            suffix = " (외부용)"

        results.append({
            "date": file_date,
            "title": title_part,
            "suffix": suffix,
            "link": link,
        })

    if not results:
        client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text=f"🔍 '{query}' 검색 결과가 없습니다.")
        return

    lines = [f"🔍 *'{query}' 검색 결과* ({len(results)}건)\n"]
    for r in results[:20]:
        date_label = f"*{r['date']}*  " if r["date"] else ""
        if r["link"]:
            lines.append(f"• {date_label}<{r['link']}|{r['title']}>{r['suffix']}")
        else:
            lines.append(f"• {date_label}{r['title']}{r['suffix']}")
    if len(results) > 20:
        lines.append(f"_...외 {len(results) - 20}건_")

    client.chat_postMessage(
        channel=channel or user_id, thread_ts=thread_ts,
        text="\n".join(lines),
    )


# ── 업체 메모 헬퍼 (CM-11) ──────────────────────────────────

def _add_company_memo(client, *, user_id: str, company: str, memo: str,
                      channel: str = None, thread_ts: str = None):
    """업체 Wiki 파일의 내부 메모 섹션에 메모 추가"""
    from tools import drive
    creds = user_store.get_credentials(user_id)
    if not creds:
        client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text="⚠️ Google 인증이 필요합니다. `/등록`으로 먼저 인증해주세요.",
        )
        return

    user = user_store.get_user(user_id)
    contacts_folder_id = user.get("contacts_folder_id") if user else None
    if not contacts_folder_id:
        client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text="⚠️ Contacts 폴더가 설정되지 않았습니다.",
        )
        return

    content, file_id, _ = drive.get_company_info(creds, contacts_folder_id, company)

    from zoneinfo import ZoneInfo
    now_str = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M")
    memo_entry = f"- [{now_str}] {memo}"

    if content:
        # 기존 파일에 메모 섹션 추가/갱신
        memo_header = "## 내부 메모"
        if memo_header in content:
            # 기존 메모 섹션에 append
            content = content.replace(memo_header, f"{memo_header}\n{memo_entry}")
        else:
            content = content.rstrip() + f"\n\n{memo_header}\n{memo_entry}\n"
        drive.save_company_info(creds, contacts_folder_id, company, content, file_id)
    else:
        # 새 파일 생성
        content = f"# {company}\n\n## 내부 메모\n{memo_entry}\n"
        drive.save_company_info(creds, contacts_folder_id, company, content)

    client.chat_postMessage(
        channel=channel or user_id, thread_ts=thread_ts,
        text=f"📝 *{company}* 메모가 저장되었습니다.\n> {memo}",
    )


def _route_message(text: str, client, user_id: str, channel: str = None,
                   thread_ts: str = None, user_msg_ts: str = None):
    log.info(f"메시지 라우팅 ({user_id}): {text}")

    intent_data = _classify_intent(text)
    intent = intent_data.get("intent", "unknown")
    params = intent_data.get("params", {})
    log.info(f"인텐트 분류: {intent} / params: {params}")


    if intent == "briefing":
        start_date = params.get("start_date")
        end_date = params.get("end_date")
        period_text = params.get("period_text")
        run_briefing(client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                     start_date=start_date, end_date=end_date, period_text=period_text)

    elif intent == "create_meeting":
        create_meeting_from_text(client, user_id=user_id, user_message=text,
                                 channel=channel, thread_ts=thread_ts,
                                 user_msg_ts=user_msg_ts)

    elif intent == "cancel_meeting":
        threading.Thread(
            target=cancel_meeting_from_text,
            args=(client,),
            kwargs=dict(user_id=user_id, user_message=text,
                        channel=channel, thread_ts=thread_ts),
            daemon=True,
        ).start()

    elif intent == "suggest_slots":
        threading.Thread(
            target=suggest_meeting_slots,
            args=(client,),
            kwargs=dict(user_id=user_id, user_message=text,
                        channel=channel, thread_ts=thread_ts),
            daemon=True,
        ).start()

    elif intent == "start_session":
        title = params.get("title", "").strip() or "미팅"
        start_session(client, user_id=user_id, title=title,
                      channel=channel, thread_ts=thread_ts)

    elif intent == "add_note":
        note = params.get("note", "").strip() or text
        add_note(client, user_id=user_id, note_text=note,
                 channel=channel, thread_ts=thread_ts)

    elif intent == "end_session":
        end_session(client, user_id=user_id,
                    channel=channel, thread_ts=thread_ts)

    elif intent == "generate_minutes":
        threading.Thread(
            target=generate_minutes_now,
            args=(client, user_id),
            kwargs=dict(channel=channel, thread_ts=thread_ts),
            daemon=True,
        ).start()

    elif intent == "get_minutes":
        q = params.get("query", "").strip()
        if q:
            _search_minutes(client, user_id=user_id, query=q,
                            channel=channel, thread_ts=thread_ts)
        else:
            get_minutes_list(client, user_id=user_id,
                             channel=channel, thread_ts=thread_ts)

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
            text="드림플러스 계정 설정은 `/드림플러스` 명령어를 사용해주세요.",
        )

    elif intent == "trello_search":
        query = params.get("query", "").strip()
        after.handle_trello_search(client, user_id=user_id, query=query,
                                   channel=channel, thread_ts=thread_ts)

    elif intent == "search_minutes":
        query = params.get("query", "").strip()
        _search_minutes(client, user_id=user_id, query=query,
                        channel=channel, thread_ts=thread_ts)

    elif intent == "company_memo":
        company = params.get("company", "").strip()
        memo = params.get("memo", "").strip()
        if not company or not memo:
            client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                    text="업체명과 메모 내용을 알려주세요. 예: '카카오 관련 메모 — PoC 예산 확보'")
        else:
            _add_company_memo(client, user_id=user_id, company=company, memo=memo,
                              channel=channel, thread_ts=thread_ts)

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
        text="Google 계정을 연결해주세요.",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"🔗 <{auth_url}|Google 계정 연결하기>를 클릭해주세요."}}],
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
        text="Google 계정 재연결 링크입니다.",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"🔄 <{auth_url}|Google 계정 재연결하기> — 클릭 후 권한을 다시 동의해주세요."}}],
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
    query = body.get("text", "").strip()
    if query:
        # FR-D11/D12: 업체명 또는 기간 기반 검색
        _search_minutes(client, user_id=user_id, query=query)
    else:
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


@app.action("trello_new_card")
def handle_trello_new_card(ack, body, client):
    ack()
    threading.Thread(
        target=after.handle_trello_new_card, args=(client, body), daemon=True
    ).start()


@app.action("trello_confirm_new_card")
def handle_trello_confirm_new_card(ack, body, client):
    ack()
    threading.Thread(
        target=after.handle_trello_confirm_new_card, args=(client, body), daemon=True
    ).start()


@app.action("trello_cancel_new_card")
def handle_trello_cancel_new_card(ack, body, client):
    ack()
    after.handle_trello_cancel_new_card(client, body)


@app.action(re.compile(r"^trello_select_card_.+"))
def handle_trello_select_card(ack, body, client):
    ack()
    threading.Thread(
        target=after.handle_trello_select_card, args=(client, body), daemon=True
    ).start()


# ── 제안서 액션 핸들러 (Phase 2.4) ────────────────────────────

@app.action("proposal_start")
def handle_proposal_start(ack, body, client):
    ack()
    threading.Thread(
        target=proposal_agent.handle_proposal_start,
        args=(client, body), daemon=True,
    ).start()


@app.action("proposal_skip")
def handle_proposal_skip(ack, body, client):
    ack()
    proposal_agent.handle_proposal_skip(client, body)


@app.action("proposal_confirm_outline")
def handle_proposal_confirm_outline(ack, body, client):
    ack()
    threading.Thread(
        target=proposal_agent.handle_proposal_confirm_outline,
        args=(client, body), daemon=True,
    ).start()


@app.action("proposal_edit_outline")
def handle_proposal_edit_outline(ack, body, client):
    ack()
    proposal_agent.handle_proposal_edit_outline(client, body)


@app.action("proposal_cancel")
def handle_proposal_cancel(ack, body, client):
    ack()
    proposal_agent.handle_proposal_cancel(client, body)


@app.action("proposal_done")
def handle_proposal_done(ack, body, client):
    ack()
    proposal_agent.handle_proposal_done(client, body)


@app.action("proposal_edit")
def handle_proposal_edit(ack, body, client):
    ack()
    proposal_agent.handle_proposal_edit(client, body)


@app.action("proposal_open_doc")
def handle_proposal_open_doc(ack, body, client):
    ack()  # URL 버튼 — 브라우저에서 열림


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

def _draft_owner(draft_key: str | None) -> str | None:
    """draft_key 소유자 user_id 조회 (없으면 None)"""
    if not draft_key:
        return None
    d = _pending_minutes.get(draft_key)
    return d.get("user_id") if d else None


@app.action("minutes_confirm")
def handle_minutes_confirm(ack, body, client):
    ack()
    draft_key = body.get("actions", [{}])[0].get("value", "") or None
    if not _ensure_creator(client, body, _draft_owner(draft_key)):
        return
    # user_id는 draft 소유자로 설정 (클릭한 사람이 아닌)
    user_id = _draft_owner(draft_key) or body["user"]["id"]
    threading.Thread(
        target=finalize_minutes,
        args=(client, user_id),
        kwargs=dict(draft_key=draft_key),
        daemon=True,
    ).start()


@app.action("minutes_edit_request")
def handle_minutes_edit_request(ack, body, client):
    ack()
    draft_key = body.get("actions", [{}])[0].get("value", "") or None
    if not _ensure_creator(client, body, _draft_owner(draft_key)):
        return
    user_id = _draft_owner(draft_key) or body["user"]["id"]
    request_minutes_edit(client, user_id, draft_key=draft_key)


@app.action("minutes_cancel")
def handle_minutes_cancel(ack, body, client):
    ack()
    draft_key = body.get("actions", [{}])[0].get("value", "") or None
    if not _ensure_creator(client, body, _draft_owner(draft_key)):
        return
    user_id = _draft_owner(draft_key) or body["user"]["id"]
    cancel_minutes(client, user_id, draft_key=draft_key)


@app.action("minutes_open_doc")
def handle_minutes_open_doc(ack, body, client):
    ack()  # URL 버튼 — 브라우저에서 열림, 별도 처리 불필요


# ── 회의록 소스 선택 액션 핸들러 (I1) ────────────────────────

def _handle_minutes_src(ack, body, client):
    """I1: /미팅종료 후 회의록 소스 선택 버튼 콜백 (I5: 본인만 허용)"""
    ack()
    action = body.get("actions", [{}])[0]
    action_id = action.get("action_id", "")
    source = action_id.removeprefix("minutes_src_")  # transcript | notes | wait | cancel
    event_id = action.get("value", "")
    if not event_id:
        return
    # I5: 소스 선택 대기 payload의 원 소유자만 클릭 가능
    from agents.during import _pending_source_select
    payload = _pending_source_select.get(event_id)
    expected = payload.get("user_id") if payload else None
    if not _ensure_creator(client, body, expected):
        return
    user_id = expected or body["user"]["id"]
    threading.Thread(
        target=handle_minutes_source_select,
        args=(client, user_id, event_id, source),
        kwargs=dict(body=body),
        daemon=True,
    ).start()

for _src in ("transcript", "notes", "wait", "cancel"):
    app.action(f"minutes_src_{_src}")(_handle_minutes_src)


# ── F2: 일정 취소 액션 핸들러 ────────────────────────────────

def _handle_meeting_cancel_confirm(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    event_id = body.get("actions", [{}])[0].get("value", "")
    if not event_id:
        return
    threading.Thread(
        target=handle_meeting_cancel_confirm,
        args=(client, user_id, event_id),
        kwargs=dict(body=body),
        daemon=True,
    ).start()

# 단일 후보(확인 블록) + 복수 후보(선택 블록, 인덱스 접미사) 모두 대응
app.action("meeting_cancel_confirm")(_handle_meeting_cancel_confirm)
for _i in range(5):
    app.action(f"meeting_cancel_confirm_{_i}")(_handle_meeting_cancel_confirm)


@app.action("meeting_cancel_abort")
def _handle_meeting_cancel_abort(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    event_id = body.get("actions", [{}])[0].get("value", "")
    handle_meeting_cancel_abort(client, user_id, event_id, body=body)


@app.action("meeting_cancel_with_room")
def _handle_meeting_cancel_with_room(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    event_id = body.get("actions", [{}])[0].get("value", "")
    threading.Thread(
        target=handle_meeting_cancel_with_room,
        args=(client, user_id, event_id),
        kwargs=dict(body=body),
        daemon=True,
    ).start()


@app.action("meeting_cancel_event_only")
def _handle_meeting_cancel_event_only(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    event_id = body.get("actions", [{}])[0].get("value", "")
    threading.Thread(
        target=handle_meeting_cancel_event_only,
        args=(client, user_id, event_id),
        kwargs=dict(body=body),
        daemon=True,
    ).start()


@app.action("meeting_cancel_abort_both")
def _handle_meeting_cancel_abort_both(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    event_id = body.get("actions", [{}])[0].get("value", "")
    handle_meeting_cancel_abort_both(client, user_id, event_id, body=body)


# ── F1: 슬롯 추천 → 미팅 생성 ───────────────────────────────

def _handle_slot_create(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    slot_value = body.get("actions", [{}])[0].get("value", "")
    if not slot_value:
        return
    threading.Thread(
        target=handle_slot_create_meeting,
        args=(client, user_id, slot_value),
        kwargs=dict(body=body),
        daemon=True,
    ).start()

for _i in range(5):
    app.action(f"slot_create_meeting_{_i}")(_handle_slot_create)


# ── I2(a): 미팅 생성 확인 ────────────────────────────────────

@app.action("create_confirm")
def _handle_create_confirm(ack, body, client):
    ack()
    draft_id = body.get("actions", [{}])[0].get("value", "")
    # I5: 생성 확인은 요청자 본인만
    payload = _pending_create_confirm.get(draft_id)
    expected = payload.get("user_id") if payload else None
    if not _ensure_creator(client, body, expected):
        return
    user_id = expected or body["user"]["id"]
    threading.Thread(
        target=handle_create_confirm,
        args=(client, user_id, draft_id),
        kwargs=dict(body=body),
        daemon=True,
    ).start()


@app.action("create_abort")
def _handle_create_abort(ack, body, client):
    ack()
    draft_id = body.get("actions", [{}])[0].get("value", "")
    payload = _pending_create_confirm.get(draft_id)
    expected = payload.get("user_id") if payload else None
    if not _ensure_creator(client, body, expected):
        return
    user_id = expected or body["user"]["id"]
    handle_create_abort(client, user_id, draft_id, body=body)


# ── I2(b): 회의실 예약 여부 확인 ────────────────────────────

@app.action("room_offer_show")
def _handle_room_offer_show(ack, body, client):
    ack()
    offer_id = body.get("actions", [{}])[0].get("value", "")
    payload = _pending_room_offer.get(offer_id)
    expected = payload.get("user_id") if payload else None
    if not _ensure_creator(client, body, expected):
        return
    user_id = expected or body["user"]["id"]
    handle_room_offer_show(client, user_id, offer_id, body=body)


@app.action("room_offer_skip")
def _handle_room_offer_skip(ack, body, client):
    ack()
    offer_id = body.get("actions", [{}])[0].get("value", "")
    payload = _pending_room_offer.get(offer_id)
    expected = payload.get("user_id") if payload else None
    if not _ensure_creator(client, body, expected):
        return
    user_id = expected or body["user"]["id"]
    handle_room_offer_skip(client, user_id, offer_id, body=body)


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


@app.action(re.compile(r"^select_attendee_email"))
def handle_select_attendee_email(ack, body, client):
    ack()
    handle_email_selection(client, body)


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

app.command("/드림플러스")(_dp_settings_handler)
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
            text="Trello 계정 연결",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": f"🔗 <{auth_url}|Trello 계정 연결하기>를 클릭하여 접근을 허용하세요."}}],
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


def _trello_search_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    query = body.get("text", "").strip()
    threading.Thread(
        target=after.handle_trello_search,
        args=(client,),
        kwargs={"user_id": user_id, "query": query},
        daemon=True,
    ).start()

app.command("/트렐로조회")(_trello_search_handler)
app.command("/trello-search")(_trello_search_handler)


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
