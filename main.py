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

from agents import before as before_agent
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
    handle_recover_meeting_minutes_button,
    handle_event_selection,
    handle_event_title_reply,
    start_document_based_minutes,
    post_pending_drafts,
    handle_pending_view_button,
    handle_pending_review_button,
    handle_pending_discard_button,
    handle_pending_cleanup_all_button,
    handle_pending_cleanup_confirm_button,
    handle_pending_cleanup_cancel_button,
    _active_sessions,
    _pending_minutes,
    _pending_inputs,
    _find_draft_for_user,
    find_draft_by_thread_ts,
    get_session_thread,
)
from agents import during as during_agent
from agents import after
from agents import minutes_normalizer
from agents import card as card_agent
from agents import dreamplus as dreamplus_agent
from agents import feedback as feedback_agent
from agents import proposal as proposal_agent
from agents import todo as todo_agent
from agents import trello_report as trello_report_agent
from store import user_store
from server import oauth as oauth_server
from tools import stt
from tools import text_extract
from tools import calendar as cal_tools

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
        # briefing_enabled가 0인 사용자는 건너뜀 (NULL/1은 수신)
        enabled = row.get("briefing_enabled")
        if enabled is not None and not enabled:
            log.info(f"자동 브리핑 비활성화로 건너뜀: {user_id}")
            continue
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


def scheduled_trello_weekly():
    log.info("Trello 주간 보고서 실행")
    try:
        trello_report_agent.send_weekly_report(app.client)
    except Exception as e:
        log.exception(f"Trello 주간 보고서 실패: {e}")


def scheduled_meeting_alarm():
    """매분 실행 — 약 5분 뒤 시작하는 미팅을 찾아 알람 + 자동 세션 시작."""
    try:
        _check_and_send_meeting_alarms(app.client)
    except Exception as e:
        log.exception(f"미팅 시작 알람 폴링 실패: {e}")


def scheduled_fast_transcript_check():
    """2분 주기 — 캘린더 종료시각이 지난 활성 세션이 있으면 즉시 트랜스크립트 탐색.

    `check_transcripts`(10분 주기)와 별개로, 자동 바인딩된 세션이 끝나자마자
    회의록을 만들 수 있도록 빠른 경로 제공.
    """
    try:
        _fast_transcript_check_for_ended_sessions(app.client)
    except Exception as e:
        log.exception(f"빠른 트랜스크립트 폴링 실패: {e}")


def _fast_transcript_check_for_ended_sessions(slack_client):
    from zoneinfo import ZoneInfo
    kst = ZoneInfo("Asia/Seoul")
    now_kst = datetime.now(kst)

    # 종료시각이 지난 활성 세션을 가진 사용자만 추림 — 불필요한 캘린더 호출 회피
    target_users: set[str] = set()
    for user_id, sess in list(during_agent._active_sessions.items()):
        end_iso = sess.get("event_end_iso") or ""
        if not end_iso:
            continue
        try:
            end_dt = datetime.fromisoformat(end_iso)
        except Exception:
            continue
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=kst)
        if now_kst >= end_dt:
            target_users.add(user_id)

    if not target_users:
        return

    for user_id in target_users:
        try:
            during_agent._check_transcripts_for_user(
                slack_client, user_id, min_minutes_ago=0,
            )
        except Exception as e:
            if user_store.is_token_expired_error(e):
                log.info(f"빠른 폴링 — 토큰 만료, 건너뜀: {user_id}")
            else:
                log.exception(f"빠른 폴링 실패 ({user_id}): {e}")


# 알람 발송 윈도우 — 매분 폴링이라 ±30초 여유를 둠 (4.5~5.5분 후 시작)
_ALARM_WINDOW_MIN_S = 4 * 60 + 30
_ALARM_WINDOW_MAX_S = 5 * 60 + 30


def _check_and_send_meeting_alarms(slack_client):
    """전체 사용자 대상 미팅 시작 알람 발송 + 자동 세션 바인딩."""
    from zoneinfo import ZoneInfo
    kst = ZoneInfo("Asia/Seoul")
    now_kst = datetime.now(kst)

    # 24시간에 한 번 수준으로 오래된 알람 기록 정리 (분당 폴링 부하 회피)
    if now_kst.hour == 0 and now_kst.minute == 0:
        try:
            removed = user_store.cleanup_old_meeting_alarms(days=14)
            if removed:
                log.info(f"오래된 미팅 알람 기록 {removed}건 정리")
        except Exception as e:
            log.warning(f"미팅 알람 기록 정리 실패: {e}")

    for row in user_store.all_users():
        user_id = row["slack_user_id"]
        # 알람 비활성 사용자는 건너뜀 (NULL/1은 수신)
        enabled = row.get("meeting_start_alarm_enabled")
        if enabled is not None and not enabled:
            continue
        try:
            _check_user_meeting_alarm(slack_client, user_id, now_kst)
        except Exception as e:
            if user_store.is_token_expired_error(e):
                log.info(f"미팅 알람 — 토큰 만료, 건너뜀: {user_id}")
            else:
                log.exception(f"미팅 알람 실패 ({user_id}): {e}")


def _check_user_meeting_alarm(slack_client, user_id: str, now_kst: datetime):
    """사용자 1명 — 5분 뒤 시작 이벤트 알람 후보 검사."""
    creds = user_store.get_credentials(user_id)
    events = cal_tools.get_upcoming_meetings(creds, days=1, from_now=True)
    for ev in events:
        ev_id = ev.get("id")
        start_str = (ev.get("start") or {}).get("dateTime")
        if not ev_id or not start_str:
            continue  # 종일 이벤트 등은 스킵
        try:
            start_dt = datetime.fromisoformat(start_str)
        except Exception:
            continue
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=now_kst.tzinfo)
        delta_s = (start_dt - now_kst).total_seconds()
        if not (_ALARM_WINDOW_MIN_S <= delta_s <= _ALARM_WINDOW_MAX_S):
            continue
        if user_store.was_meeting_alarm_sent(user_id, ev_id):
            continue
        try:
            _send_meeting_start_alarm(slack_client, user_id, ev, start_dt)
            user_store.mark_meeting_alarm_sent(user_id, ev_id)
        except Exception as e:
            log.exception(f"미팅 알람 발송 실패 ({user_id}, {ev_id}): {e}")


def _send_meeting_start_alarm(slack_client, user_id: str, event_raw: dict,
                              start_dt: datetime):
    """단일 미팅 알람 DM 발송 + 활성 세션 없으면 자동 바인딩."""
    parsed = cal_tools.parse_event(event_raw)
    title = parsed.get("summary") or "(제목 없음)"
    meet_link = parsed.get("meet_link") or ""
    location = (parsed.get("location") or "").strip()
    end_str = (event_raw.get("end") or {}).get("dateTime", "")

    time_line = start_dt.strftime("%H:%M")
    if end_str:
        try:
            end_dt = datetime.fromisoformat(end_str)
            time_line = f"{start_dt.strftime('%H:%M')} ~ {end_dt.strftime('%H:%M')}"
        except Exception:
            pass

    lines = [f"🔔 *5분 뒤 미팅 시작*", f"\n*{title}*", f"🕐 {time_line}"]
    if location:
        lines.append(f"📍 {location}")
    if meet_link:
        lines.append(f"🎥 <{meet_link}|Google Meet 참여>")
    lines.append("")  # 빈 줄

    # 자동 세션 바인딩 (이미 활성 세션 있으면 건너뜀)
    bound = False
    try:
        bound = during_agent.bind_event_session(user_id, event_raw)
    except Exception as e:
        log.exception(f"미팅 알람 — 세션 자동 바인딩 실패 ({user_id}): {e}")

    if bound:
        lines.append("회의록 자동 생성을 위해 세션을 시작했어요. 미팅이 끝나면 아래 버튼을 누르거나, 트랜스크립트 도착 시 자동 처리됩니다.")
    else:
        lines.append("_이미 진행 중인 세션이 있어 자동 바인딩은 건너뛰었어요._")

    fallback = f"🔔 5분 뒤 미팅 시작 — {title} ({time_line})"
    blocks: list[dict] = [{"type": "section",
                           "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]
    # "지금 미팅 끝남" 버튼 — 사용자가 미팅 끝나자마자 누르면 즉시 회의록 흐름 진입
    blocks.append({
        "type": "actions",
        "elements": [{
            "type": "button",
            "action_id": "meeting_end_now",
            "text": {"type": "plain_text", "text": "🛑 지금 미팅 끝남", "emoji": True},
            "style": "primary",
            "value": parsed.get("id", ""),
        }],
    })
    slack_client.chat_postMessage(channel=user_id, text=fallback, blocks=blocks,
                                  unfurl_links=False, unfurl_media=False)


from datetime import datetime as _dt
scheduler = BackgroundScheduler(timezone="Asia/Seoul")
scheduler.add_job(scheduled_briefing, "cron", hour=9, minute=0)
scheduler.add_job(scheduled_transcript_check, "interval", minutes=10,
                  next_run_time=_dt.now())
scheduler.add_job(scheduled_action_item_reminder, "cron", hour=8, minute=0)
scheduler.add_job(scheduled_feedback_digest, "cron", hour=22, minute=0)
scheduler.add_job(scheduled_trello_weekly, "cron",
                  day_of_week="fri", hour=21, minute=0)
# 미팅 시작 5분 전 알람 — 매분 폴링
scheduler.add_job(scheduled_meeting_alarm, "interval", minutes=1)
# 캘린더 종료 후 빠른 회의록 생성 — 2분 주기로 종료된 세션만 탐색
scheduler.add_job(scheduled_fast_transcript_check, "interval", minutes=2)


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
  └ 생성 결과 메시지의 `[👥 참석자 추가]` 버튼으로 참석자만 빠르게 추가
• `/미팅편집` or `/미팅수정` or `/미팅변경` — 향후 미팅 편집 UI
  └ 자연어도 가능: _"카카오 미팅 편집해줘"_, _"내일 KISA 회의 시간 변경"_
  └ 브리핑 헤더의 `[✏️ 편집]` 버튼으로 해당 미팅 바로 편집
• `/브리핑` or `브리핑 해줘` — 오늘 미팅 브리핑

*🎙️ 회의 진행*
• `/미팅시작 [제목]` or `미팅 시작해줘` — 회의 시작 (메모 세션 시작)
  └ 후보 일정 있으면 선택 UI 표시 — `📝 새 미팅 추가` 버튼으로 캘린더 밖 미팅도 시작 가능
• `/메모 [내용]` or `메모: [내용]` — 회의 중 메모 추가 (세션 자동 시작)
  └ 캘린더 일정 자동 감지, 여러 개면 선택 UI 제공
• 🎙️ 음성 파일 업로드 — STT 변환 후 메모로 자동 등록
• 📄 텍스트 문서 업로드 — 세션 중엔 메모로 추가, 세션 없이 업로드 시 **트랜스크립트로 간주해 회의록 초안 즉시 생성** (저장 시 업체 Wiki 자동 갱신)
• `/미팅종료` or `미팅 종료` — 회의 종료 및 회의록 자동 생성
  └ 5가지 소스 중 선택: 🎙️ 트랜스크립트 탐색 / 📎 트랜스크립트 첨부 / 📝 노트만 / 🕐 트랜스크립트 대기 / ❌ 취소
  └ `📎 트랜스크립트 첨부` — 개인 녹음·외부 STT 결과 텍스트 파일을 그대로 트랜스크립트로 사용 (한글 cp949·euc-kr 자동 디코딩)
  └ Google Meet 트랜스크립트는 *원문 Transcript* 를 우선 사용 — Gemini 요약본은 원문이 없을 때만 폴백
• `/회의록작성` or `회의록 작성해줘` — 현재 세션 기반 회의록 즉시 생성
• 📝 *회의록 초안에서 수정 요청 했을 때* — 답글 입력 → 새 초안 카드 재발송. 이전 카드는 무시하고 *새 카드에서* 저장/편집
• 🔄 *회의록을 처음부터 다시 만들기* — 초안 카드의 ❌ 취소 → `/미팅종료` 재실행 또는 `📎 트랜스크립트 첨부`로 텍스트 직접 업로드
• `/회의록` or `회의록 보여줘` — 저장된 회의록 목록 조회
  └ `/회의록 카카오` — 업체 기반 검색  |  `/회의록 2026-03` — 기간 기반 검색
  └ `카카오 지난달 회의록 찾아줘` — 자연어 검색
  └ 양식 깨진 파일 옆에 `[🔧 양식 보정]` 버튼이 자동 노출됨
• `/회의록정리` or `/회의록보정` — 저장된 회의록 양식·구조 보정
  └ 자연어도 가능: _"회의록 양식 깨진 거 고쳐줘"_, _"지난 회의록 정리해줘"_
• `/대기회의록` or `대기 회의록` — 검토 대기(저장 전 초안) 목록 + 항목별 검토/버리기 버튼
  └ 새 회의록 생성 시 `[📋 대기 목록 자세히] [🗑️ 모두 정리]` 버튼이 함께 안내됨

*📝 할 일 (Todo)*
• `/할일추가 [내용]` or `할 일 추가 [내용]` — 개인 Todo 추가 (DM·@멘션 모두 지원)
  └ 자연어 마감일: _"내일까지 AIA 제안서 이슈 작성"_, _"다음주 금요일까지 …"_
  └ 카테고리: 해시태그(#업무/#개인/#AI) 또는 LLM 자동 추론 (기본 업무)
• `/할일` or `할 일` or `투두 보여줘` — 활성 목록 + 최근 완료 5건
• `[제목] 완료` / `[제목] 취소` / `[제목] 삭제` — 자연어 종료
  └ 또는 조회 결과의 `[✅ 완료] [🚫 취소] [🗑️ 삭제]` 버튼
  └ 09:00 브리핑에 마감 임박 항목 색상 강조하여 자동 노출

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


_QUESTION_ANSWER_PROMPT = """당신은 ParaMee(파라메타 AI 미팅 어시스턴트) Slack 봇입니다.
사용자가 봇 사용법이나 기능에 대해 자연어로 질문했습니다. 아래 기능 가이드만 근거로, 한국어로 간결하고 구체적으로 답해주세요.

규칙:
- 핵심 답변을 먼저 1~2문장으로 제시하고, 필요하면 명령어 예시(백틱 포함)를 bullet로 덧붙이세요.
- 기능 가이드에 없는 내용은 추측하지 말고 "해당 기능은 제가 지원하지 않거나 아직 모릅니다."라고 답하세요.
- 질문과 관계없는 기능은 언급하지 마세요.
- 전체 명령 목록을 길게 나열하지 마세요 (그건 /도움말 전용).

--- 기능 가이드 ---
{help_text}

--- 사용자 질문 ---
{question}

답변:"""


def _handle_question(client, text: str, user_id: str,
                     channel: str | None, thread_ts: str | None):
    """사용법·기능에 대한 자연어 질문에 LLM으로 답변."""
    from agents.before import generate_text
    prompt = _QUESTION_ANSWER_PROMPT.format(help_text=_HELP_TEXT, question=text)
    try:
        answer = generate_text(prompt)
    except Exception:
        log.exception("question 인텐트 답변 생성 실패")
        answer = ("답변을 생성하지 못했어요. `/도움말`에서 사용 가능한 명령어를 확인해주세요.")
    client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts, text=answer)


def _send_trello_setup_link(client, user_id: str, *,
                            channel: str = None, thread_ts: str = None) -> None:
    """Slash command 미등록 환경에서도 자연어로 Trello 연결 링크를 발송."""
    if not _check_registered(client, user_id):
        return
    try:
        auth_url = oauth_server.build_trello_auth_url(user_id)
        client.chat_postMessage(
            channel=channel or user_id,
            thread_ts=thread_ts,
            text="Trello 계정 연결",
            blocks=[{
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🔗 <{auth_url}|Trello 계정 연결하기>를 클릭하여 접근을 허용하세요.",
                },
            }],
        )
    except Exception as e:
        client.chat_postMessage(
            channel=channel or user_id,
            thread_ts=thread_ts,
            text=f"❌ Trello 인증 URL 생성 실패: {e}",
        )


def _is_trello_setup_text(text: str) -> bool:
    normalized = re.sub(r"\s+", "", (text or "").lower())
    return normalized in {
        "trello",
        "트렐로",
        "트렐로연동",
        "트렐로연결",
        "trello연동",
        "trello연결",
        "trello연결하기",
        "트렐로연결하기",
    }


_INTENT_PROMPT = """사용자의 Slack 메시지를 분석해서 의도(intent)를 분류해줘.

메시지: "{text}"

가능한 intent 목록:
- briefing: 브리핑 요청 (예: "브리핑 해줘", "오늘 미팅 현황", "brief", "이번주 일정 브리핑", "앞으로 3일 일정")
- create_meeting: 미팅/일정 생성 (예: "내일 3시에 KISA 미팅 잡아줘", "오늘 15시 홍길동 회의 만들어줘")
- cancel_meeting: 캘린더 일정 취소 — 드림플러스 회의실 예약 취소가 아니라 *Google Calendar 미팅*을 취소 (예: "내일 3시 카카오 미팅 취소해줘", "오늘 KISA 회의 삭제", "4/18 회의 지워줘")
- edit_meeting: 이미 생성된 캘린더 미팅을 편집·수정·변경 (예: "카카오 미팅 편집", "내일 미팅 수정할래", "내일 KISA 회의 시간 변경", "미팅 변경하고 싶어"). 새 미팅을 잡는 것이 아니라 *기존 미팅* 을 고칠 때만. 단순 답글로 처리되는 변경 메시지가 아니라 명시적인 편집/수정/변경 의도일 때.
- suggest_slots: 여러 참석자의 빈 시간대 추천 (예: "김민환, 홍길동이랑 다음주에 1시간 미팅 가능한 시간 찾아줘", "이번주 중에 팀 전체 2시간 비는 시간 알려줘")
- start_session: 미팅 시작 (예: "미팅 시작", "회의 시작해줘", "지금부터 KISA 회의 시작")
- add_note: 메모 추가 — 현재 진행 중인 회의에 내용 기록 (예: "메모: 예산 협의됨", "기록해줘 다음달 계약 예정", "노트 추가")
- end_session: 미팅 종료 (예: "미팅 종료", "회의 끝났어", "미팅 마무리해줘")
- generate_minutes: 회의록 작성 요청 (예: "회의록 작성해줘", "회의록 만들어줘", "회의록 생성")
- get_minutes: 회의록 조회·검색 (예: "회의록 보여줘", "회의록 목록", "지난 목요일 회의록", "4월 13일 회의록", "카카오 회의록 찾아줘", "지난달 회의록", "삼성전자 3월 회의록", "이번주 회의록")
- pending_minutes_list: 검토 대기 회의록(아직 저장 안 한 초안) 목록 조회 (예: "대기 회의록", "검토 대기 목록", "초안 보여줘", "회의록 대기열", "검토 대기 회의록 보여줘", "대기 중인 회의록", "회의록 초안 목록")
- normalize_minutes: 저장된 회의록 양식·구조 보정 (예: "회의록 정리해줘", "회의록 양식 보정", "회의록 깨진 거 고쳐줘", "지난 회의록 양식 정리", "회의록 프론트매터 다시 만들어줘")
- research_company: 특정 업체 정보 조사 (예: "KISA 알아봐줘", "삼성전자 정보 검색해줘", "카카오 최근 동향")
- research_person: 특정 인물 정보 조사 (예: "홍길동 인물 정보", "김민환 누구야", "이준호 카카오 담당자 조사해줘")
- update_knowledge: 내부 서비스 지식 갱신 (예: "knowledge 업데이트", "서비스 정보 갱신")
- dreamplus_book: 드림플러스 회의실 예약 (예: "회의실 예약해줘", "내일 2시에 회의실 잡아줘", "드림플러스 3시간 예약", "회의실 오늘 오후 3시부터 5시 2명")
- dreamplus_list: 드림플러스 예약 현황 조회 (예: "예약 현황 보여줘", "회의실 예약 목록", "드림플러스 예약 확인", "내 회의실 예약")
- dreamplus_cancel: 드림플러스 예약 취소 (예: "회의실 예약 취소", "드림플러스 예약 취소해줘")
- dreamplus_credits: 드림플러스 크레딧/포인트 조회 (예: "크레딧 얼마나 남았어", "포인트 확인", "드림플러스 크레딧 조회")
- dreamplus_settings: 드림플러스 계정 설정 (예: "드림플러스 설정", "드림플러스 로그인 정보 등록", "드림플러스 계정 등록")
- trello_search: Trello 카드 조회/검색 (예: "트렐로 카드 보여줘", "트렐로 조회", "KISA 트렐로 카드", "트렐로에서 삼성 찾아줘", "Trello 카드 목록")
- trello_weekly_report: Trello 워크스페이스 주간 보고서 생성 (예: "주간보고서", "주간 보고", "트렐로 주간 보고", "이번주 트렐로 요약", "주간 트렐로 업데이트", "지난 2주 트렐로 보고서", "트렐로 주간보고 생성해줘", "weekly trello report")
- search_minutes: (사용하지 않음 — get_minutes로 통합됨)
- company_memo: 업체 관련 메모 저장 (예: "카카오 메모 — PoC 예산 확보", "삼성 메모: 담당자 변경됨", "KISA 관련 메모 저장: 내달 계약 예정")
- todo_add: 개인 할 일 추가 (예: "할 일 추가 AIA 제안서 이슈 작성", "할일 추가 내일까지 병원 예약", "todo: meeting-agent Todo 기능 설계 #AI", "투두 추가 다음주 금요일까지 카카오 PoC 운영안 검토")
- todo_list: 활성 할 일 목록 조회 (예: "할 일", "할 일 보여줘", "todo 목록", "투두", "내 할일", "오늘 할 일 뭐야")
- todo_complete: 할 일 완료 처리 (예: "AIA 제안서 이슈 완료", "병원 예약 끝냈어", "1번 완료", "#3 완료")
- todo_cancel: 할 일 취소 처리 (예: "워드프레스 이전 취소", "그 항목 취소해줘")
- todo_delete: 할 일 삭제 (예: "병원 예약 삭제", "1번 삭제해줘", "그 todo 지워줘")
- todo_update: 할 일 수정 — 마감일·카테고리·제목 (예: "AIA 제안서 마감 5/3로 변경", "병원 예약 마감을 다음주 월요일로", "그 항목 카테고리 개인으로")
- settings: 사용자 설정 화면 — 알람 on/off 토글 (예: "설정", "내 설정", "브리핑 알람 켜줘", "브리핑 알람 꺼줘", "9시 브리핑 받기 싫어", "미팅 시작 알람 꺼줘", "5분전 알람 끄기", "미팅 알람 받을래", "settings")
  * params: {{"target": "briefing" | "start_alarm" | "show", "action": "show" | "enable" | "disable"}}
    - target="briefing": 매일 09:00 브리핑 알람 (예: "브리핑 알람", "9시 브리핑", "아침 브리핑")
    - target="start_alarm": 미팅 시작 5분 전 알람 (예: "미팅 시작 알람", "5분 전 알람", "미팅 알람")
    - target="show": 설정 화면만 보여주기 (예: "설정", "내 설정 보여줘")
    - action: "켜줘"·"받을래"는 enable, "꺼줘"·"받기 싫어"·"끄기"는 disable, 단순 화면 요청이면 show. target="show"면 action도 "show".
- feedback: 기능 요청·개선 제안·버그 리포트 (예: "~기능 추가해줘", "~이렇게 개선해줘", "~가 안 돼 버그 같아", "~기능 넣어줘", "~가 불편해", "~해줬으면 좋겠어", "~도 지원해줘")
  * 주의: 질문 형태(~어떻게 해?, ~방법 있어?, ~가능해?, ~하려면 뭘 하면 돼?)는 feedback이 아니라 question. 요구/불만/제안의 단정형일 때만 feedback.
  * 주의: 위 todo_* 인텐트(특히 todo_add/todo_complete)와 혼동 금지. 사용자가 자기 할 일을 추가·종료·수정하는 메시지면 todo_*.
  * 주의: settings 인텐트와도 구분 — "브리핑 알람 꺼줘"는 직접 토글 가능한 설정이므로 settings.
- question: 봇 사용법·기능·방법에 대한 자연어 질문 (예: "구글 밋 회의록은 자동 생성하고 싶으면 어떻게 하면 됨?", "업체 메모는 어떻게 추가해?", "트렐로 주간 보고는 어떻게 봐?", "회의실 예약 방법 알려줘", "이거 어떻게 써?", "~가능해?")
- help: 명시적 도움말/전체 명령어 안내 요청 (예: "도움말", "help", "사용법 보여줘", "뭘 할 수 있어", "명령어 목록")
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
- edit_meeting: {{"keyword": "미팅 제목 키워드 (없으면 빈 문자열)"}} — 예: "카카오 미팅 편집" → "카카오"
- suggest_slots: params 없음 (원본 메시지 전체를 그대로 사용)
- start_session: {{"title": "미팅 제목 (없으면 빈 문자열)"}}
- add_note: {{"note": "메모 내용 ('메모:', '기록해줘' 등 트리거 단어 제거 후)"}}
- research_company: {{"company": "업체명"}}
- research_person: {{"person": "이름", "company": "소속 업체명 (없으면 빈 문자열)"}}
- trello_search: {{"query": "검색할 업체명 키워드 (전체 목록 조회 시 빈 문자열)"}}
- trello_weekly_report: {{"days": 정수 (수집 기간 일수; 명시 없으면 7)}}
- get_minutes: {{"query": "검색 키워드 (업체명, 날짜, 기간 등 원본 그대로 — 단순 목록 조회면 빈 문자열)"}}
- normalize_minutes: {{"keyword": "필터 키워드 (업체명·회의명; 없으면 빈 문자열)"}}
- company_memo: {{"company": "업체명", "memo": "메모 내용"}}
- dreamplus_book: {{"text": "원본 메시지 그대로"}}
- dreamplus_cancel: {{"text": "원본 메시지 그대로"}}
- todo_add: {{"raw": "트리거 단어('할 일 추가', '할일 추가', 'todo:', '투두 추가') 제거 후의 본문 — 날짜·해시태그·카테고리 키워드는 포함 유지"}}
- todo_list: params 없음
- todo_complete: {{"target": "완료 대상 — 번호('1', '#3') 또는 제목 부분"}}
- todo_cancel: {{"target": "취소 대상 (위와 동일)", "reason": "사유 (없으면 빈 문자열)"}}
- todo_delete: {{"target": "삭제 대상 (위와 동일)"}}
- todo_update: {{"target": "수정 대상", "field": "task|due_date|category", "value": "새 값 (due_date는 YYYY-MM-DD)"}}

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

def _post_token_expired_message(client, *, user_id: str,
                                 channel: str = None, thread_ts: str = None) -> None:
    """OAuth 토큰 만료 시 친화적 안내 — `/재등록` 유도."""
    client.chat_postMessage(
        channel=channel or user_id, thread_ts=thread_ts,
        text="🔐 Google 인증이 만료되었어요.\n`/재등록` 명령으로 다시 인증해주세요.",
    )


def _handle_credentials_error(e: BaseException, client, *, user_id: str,
                               channel: str = None, thread_ts: str = None) -> bool:
    """OAuth 토큰 만료 예외라면 친화적 안내 후 True 반환. 아니면 False.

    호출자는 True가 반환되면 즉시 return 해야 한다.
    """
    if user_store.is_token_expired_error(e):
        _post_token_expired_message(client, user_id=user_id,
                                     channel=channel, thread_ts=thread_ts)
        return True
    return False


def _search_minutes(client, *, user_id: str, query: str,
                    channel: str = None, thread_ts: str = None):
    """업체명·회의명·기간 기반 회의록 검색 (Drive 파일명 기반)"""
    from tools import drive as _drive

    try:
        creds = user_store.get_credentials(user_id)
    except Exception as e:
        if _handle_credentials_error(e, client, user_id=user_id,
                                      channel=channel, thread_ts=thread_ts):
            return
        client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text=f"⚠️ 인증 오류: {e}")
        return
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
        if _handle_credentials_error(e, client, user_id=user_id,
                                      channel=channel, thread_ts=thread_ts):
            return
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


def _post_company_research_result(client, *, user_id: str, company: str,
                                  content: str, channel: str = None,
                                  thread_ts: str = None):
    """기업정보 리서치 결과를 raw Wiki가 아니라 브리핑용 요약 블록으로 발송."""
    news_lines, parascope_lines, connection_lines, _emails, update_lines = (
        before_agent._extract_company_content_sections(content)
    )
    trello_summary: list[str] = []
    trello_card_name = ""
    trello_url = ""
    try:
        trello_context = before_agent.trello.get_card_context(
            user_id, company, limit_comments=3
        )
        if trello_context:
            trello_summary = before_agent._build_trello_summary(trello_context)
            trello_card_name = trello_context.get("card_name", "")
            trello_url = trello_context.get("url", "")
        else:
            diagnostic = before_agent.trello.get_lookup_diagnostic(user_id, company)
            message = diagnostic.get("message", "Trello 카드 미발견")
            trello_summary = [f"조회 안 됨: {message}"]
    except Exception as e:
        log.warning(f"Trello 기업정보 컨텍스트 조회 실패 ({company}): {e}")
        trello_summary = [f"조회 실패: {str(e)[:120]}"]

    blocks = before_agent.build_company_research_block(
        company,
        news_lines,
        parascope_lines,
        connection_lines,
        update_lines,
        trello_summary,
        trello_card_name,
        trello_url,
    )
    client.chat_postMessage(
        channel=channel or user_id,
        thread_ts=thread_ts,
        text=f"✅ *{company}* 기업정보 갱신 완료.",
        blocks=blocks,
    )


def _try_direct_todo_route(text: str) -> tuple[str, dict] | None:
    """Todo 트리거 단어로 시작하면 LLM 분류 없이 직접 라우팅.

    LLM 분류기가 멀티라인이나 복잡한 입력에서 실패하는 경우를 우회.
    Returns (intent, params) or None.
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None

    # 첫 줄을 기준으로 트리거 매칭 (멀티라인이면 본문은 나머지)
    first_line = stripped.split("\n", 1)[0].strip()
    rest = stripped[len(first_line):].lstrip("\n").rstrip()

    # 추가 트리거 (본문 필요)
    add_triggers = ["할 일 추가", "할일 추가", "할일추가", "투두 추가", "todo add", "todo:"]
    for trig in add_triggers:
        if first_line.lower().startswith(trig.lower()):
            head_body = first_line[len(trig):].strip(" :,-")
            full_body = head_body + ("\n" + rest if rest else "")
            full_body = full_body.strip()
            if full_body:
                return ("todo_add", {"raw": full_body})
            # 첫 줄이 트리거뿐이고 나머지도 비었으면 안내
            return ("todo_add_empty", {})

    # 조회 트리거 — 자연어 다양한 변형 모두 지원
    list_triggers = [
        # 명시 트리거
        "할 일 목록", "할일 목록", "투두 목록",
        "할 일 리스트", "할일 리스트", "투두 리스트",
        "todo list", "todo 목록", "todo 리스트",
        # 보여줘 류
        "할 일 보여줘", "할일 보여줘", "투두 보여줘",
        # 내/오늘 등 한정사
        "내 할 일", "내 할일", "내 투두",
        "오늘 할 일", "오늘 할일", "오늘 투두",
        "할 일 확인", "할일 확인",
    ]
    for trig in list_triggers:
        if stripped.lower().startswith(trig.lower()):
            return ("todo_list", {})
    # 단독 키워드
    if stripped.lower() in {"할 일", "할일", "todo", "투두"}:
        return ("todo_list", {})
    # 폴백 휴리스틱: "할일"/"할 일"/"투두"/"todo"로 시작 + 명시 명령어 없음 + 뒷 단어가 짧으면 목록으로 해석
    # 예: "할일 뭐 있어", "투두 뭐", "todo 보여줘" 등
    todo_prefixes = ("할 일 ", "할일 ", "투두 ", "todo ")
    for kw in todo_prefixes:
        if stripped.lower().startswith(kw):
            tail = stripped[len(kw):].strip().lower()
            # 명시적 다른 동작 키워드면 LLM에 위임
            if any(t in tail for t in ("추가", "등록", "완료", "취소", "삭제", "지워",
                                          "수정", "변경", "마감", "바꿔", "고쳐")):
                break
            # 그 외는 목록 의도로 간주
            return ("todo_list", {})

    return None


def _route_message(text: str, client, user_id: str, channel: str = None,
                   thread_ts: str = None, user_msg_ts: str = None):
    log.info(f"메시지 라우팅 ({user_id}): {text!r}")

    if _is_trello_setup_text(text):
        _send_trello_setup_link(
            client, user_id, channel=channel, thread_ts=thread_ts
        )
        return

    # LLM 우회 직접 라우팅 — Todo 트리거 단어로 시작하면 인텐트 분류 없이 처리
    direct = _try_direct_todo_route(text)
    if direct is not None:
        intent, params = direct
        log.info(f"직접 라우팅: intent={intent} / params keys={list(params.keys())}")
        if intent == "todo_add_empty":
            client.chat_postMessage(
                channel=channel or user_id, thread_ts=thread_ts,
                text="⚠️ 할 일 본문이 비어있어요. 예: `할일 추가 내일까지 AIA 제안서 이슈 작성`",
            )
            return
        if intent == "todo_add":
            threading.Thread(
                target=todo_agent.handle_add,
                args=(client, user_id, params["raw"]),
                kwargs=dict(channel=channel, thread_ts=thread_ts),
                daemon=True,
            ).start()
            return
        if intent == "todo_list":
            threading.Thread(
                target=todo_agent.handle_list,
                args=(client, user_id),
                kwargs=dict(channel=channel, thread_ts=thread_ts),
                daemon=True,
            ).start()
            return

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

    elif intent == "edit_meeting":
        keyword = (params.get("keyword") or "").strip() or None
        threading.Thread(
            target=before_agent.list_upcoming_meetings_for_edit,
            args=(client, user_id),
            kwargs=dict(keyword=keyword, channel=channel, thread_ts=thread_ts),
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

    elif intent == "pending_minutes_list":
        # 검토 대기 회의록 (저장 전 초안) 목록
        threading.Thread(
            target=post_pending_drafts,
            args=(client,),
            kwargs=dict(user_id=user_id,
                        channel=channel or user_id,
                        thread_ts=thread_ts),
            daemon=True,
        ).start()

    elif intent == "normalize_minutes":
        keyword = (params.get("keyword") or "").strip() or None
        threading.Thread(
            target=minutes_normalizer.list_minutes_for_normalize,
            args=(client, user_id),
            kwargs=dict(keyword=keyword, channel=channel, thread_ts=thread_ts),
            daemon=True,
        ).start()

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
            _post_company_research_result(
                client, user_id=user_id, company=company, content=content,
                channel=channel, thread_ts=thread_ts,
            )
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
            content, fid = research_person(user_id, person, company, force=True)
            # 프라이버시 가드 — 내부 직원 차단 시 file_id 가 None
            if fid is None:
                client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                        text=content)
            else:
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

    elif intent == "trello_weekly_report":
        try:
            days = int(params.get("days") or 7)
        except (TypeError, ValueError):
            days = 7
        # 호출자에게만 응답 — 채널이면 스레드, DM이면 DM
        target_channel = channel or user_id
        client.chat_postMessage(
            channel=target_channel, thread_ts=thread_ts,
            text=f"📊 주간 Trello 보고서 생성 중… (최근 {days}일)",
        )

        def _run_weekly():
            try:
                result = trello_report_agent.send_weekly_report(
                    client, days=days, channel=target_channel,
                    thread_ts=thread_ts,
                )
                if not result.get("posted"):
                    client.chat_postMessage(
                        channel=target_channel, thread_ts=thread_ts,
                        text="⚠️ 보고서 발송 실패. 로그를 확인해주세요.",
                    )
            except Exception as e:
                log.exception(f"자연어 주간보고 실행 실패: {e}")
                client.chat_postMessage(
                    channel=target_channel, thread_ts=thread_ts,
                    text=f"❌ 주간 Trello 보고서 생성 실패: {e}",
                )

        threading.Thread(target=_run_weekly, daemon=True).start()

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

    elif intent == "todo_add":
        raw = (params.get("raw") or text).strip() or text
        threading.Thread(
            target=todo_agent.handle_add,
            args=(client, user_id, raw),
            kwargs=dict(channel=channel, thread_ts=thread_ts),
            daemon=True,
        ).start()

    elif intent == "todo_list":
        threading.Thread(
            target=todo_agent.handle_list,
            args=(client, user_id),
            kwargs=dict(channel=channel, thread_ts=thread_ts),
            daemon=True,
        ).start()

    elif intent == "todo_complete":
        target = (params.get("target") or "").strip().lstrip("#")
        if not target:
            client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                    text="대상 할 일을 알려주세요. 예: `1번 완료` 또는 `[제목] 완료`")
        else:
            threading.Thread(
                target=todo_agent.handle_complete,
                args=(client, user_id, target),
                kwargs=dict(channel=channel, thread_ts=thread_ts),
                daemon=True,
            ).start()

    elif intent == "todo_cancel":
        target = (params.get("target") or "").strip().lstrip("#")
        reason = (params.get("reason") or "").strip() or None
        if not target:
            client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                    text="취소할 할 일을 알려주세요. 예: `[제목] 취소`")
        else:
            threading.Thread(
                target=todo_agent.handle_cancel,
                args=(client, user_id, target),
                kwargs=dict(reason=reason, channel=channel, thread_ts=thread_ts),
                daemon=True,
            ).start()

    elif intent == "todo_delete":
        target = (params.get("target") or "").strip().lstrip("#")
        if not target:
            client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                    text="삭제할 할 일을 알려주세요. 예: `[제목] 삭제`")
        else:
            threading.Thread(
                target=todo_agent.handle_delete,
                args=(client, user_id, target),
                kwargs=dict(channel=channel, thread_ts=thread_ts),
                daemon=True,
            ).start()

    elif intent == "todo_update":
        target = (params.get("target") or "").strip().lstrip("#")
        field = (params.get("field") or "").strip()
        value = (params.get("value") or "").strip()
        if not (target and field and value):
            client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts,
                                    text="수정 대상·필드·값을 모두 알려주세요. 예: `AIA 제안서 마감 5/3로 변경`")
        else:
            threading.Thread(
                target=todo_agent.handle_update,
                args=(client, user_id, target, field, value),
                kwargs=dict(channel=channel, thread_ts=thread_ts),
                daemon=True,
            ).start()

    elif intent == "settings":
        target = (params.get("target") or "show").strip().lower()
        action = (params.get("action") or "show").strip().lower()
        setters = {
            "briefing": user_store.set_briefing_enabled,
            "start_alarm": user_store.set_meeting_start_alarm_enabled,
        }
        if action in ("enable", "disable") and target in setters:
            try:
                setters[target](user_id, action == "enable")
            except Exception as e:
                log.exception(f"설정 변경 실패 ({target}, {user_id}): {e}")
                client.chat_postMessage(
                    channel=channel or user_id, thread_ts=thread_ts,
                    text="⚠️ 설정 변경에 실패했어요. 잠시 후 다시 시도해주세요.")
                return
        _post_settings(client, user_id=user_id,
                       channel=channel, thread_ts=thread_ts)

    elif intent == "feedback":
        feedback_agent.handle_feedback(client, user_id=user_id, text=text,
                                       channel=channel, thread_ts=thread_ts)

    elif intent == "question":
        _handle_question(client, text=text, user_id=user_id,
                         channel=channel, thread_ts=thread_ts)

    elif intent == "help":
        client.chat_postMessage(channel=channel or user_id, thread_ts=thread_ts, text=_HELP_TEXT)

    else:
        # 모호 입력 추천 — LLM에게 가장 가까운 슬래시 명령 1~2개를 추천받아 안내
        suggestions: list[dict] = []
        try:
            from agents.before import suggest_commands
            suggestions = suggest_commands(text)
        except Exception:
            log.exception("suggest_commands 호출 실패")
            suggestions = []

        if suggestions:
            lines = [
                f"'{text[:30]}' 명령을 정확히 이해하지 못했어요.",
                "혹시 이런 명령을 의도하셨나요?",
            ]
            for s in suggestions:
                lines.append(f"• `{s['command']}` — {s['reason']}")
            lines.append("")
            lines.append("전체 도움말은 `/도움말`")
            client.chat_postMessage(
                channel=channel or user_id,
                thread_ts=thread_ts,
                text="\n".join(lines),
            )
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


# ── 사용자 설정 ──────────────────────────────────────────────

def _toggle_block(*, label: str, description: str, action_id: str,
                  enabled: bool) -> list[dict]:
    """단일 토글(섹션 + 액션) 블록 생성."""
    status_label = "🔔 켜짐" if enabled else "🔕 꺼짐"
    button_text = "끄기" if enabled else "켜기"
    button_style = "danger" if enabled else "primary"
    next_value = "off" if enabled else "on"
    return [
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"*{label}*\n{description}\n현재 상태: *{status_label}*"}},
        {"type": "actions",
         "elements": [{
             "type": "button",
             "action_id": action_id,
             "text": {"type": "plain_text", "text": button_text, "emoji": True},
             "style": button_style,
             "value": next_value,
         }]},
    ]


def _settings_blocks(user_id: str) -> tuple[str, list[dict]]:
    """현재 설정 상태 + 토글 버튼들 (브리핑 알람·미팅 시작 알람)."""
    briefing_on = user_store.is_briefing_enabled(user_id)
    alarm_on = user_store.is_meeting_start_alarm_enabled(user_id)
    fallback = (f"⚙️ 설정 — 브리핑: {'켜짐' if briefing_on else '꺼짐'} / "
                f"미팅 시작 알람: {'켜짐' if alarm_on else '꺼짐'}")
    blocks: list[dict] = [
        {"type": "header",
         "text": {"type": "plain_text", "text": "⚙️ 설정", "emoji": True}},
    ]
    blocks.extend(_toggle_block(
        label="매일 09:00 브리핑 알람",
        description="아침 9시에 그날 미팅·업체 브리핑을 DM으로 받습니다.",
        action_id="toggle_briefing",
        enabled=briefing_on,
    ))
    blocks.append({"type": "divider"})
    blocks.extend(_toggle_block(
        label="미팅 시작 5분 전 알람",
        description="캘린더 미팅 5분 전에 Google Meet 링크를 DM으로 받고, "
                    "회의록 자동 생성을 위한 세션이 자동 시작됩니다.",
        action_id="toggle_start_alarm",
        enabled=alarm_on,
    ))
    return fallback, blocks


def _post_settings(client, *, user_id: str, channel: str = None,
                   thread_ts: str = None):
    """현재 설정 상태 메시지 발송 (슬래시 커맨드·자연어 공통 경로)."""
    fallback, blocks = _settings_blocks(user_id)
    client.chat_postMessage(
        channel=channel or user_id,
        text=fallback,
        blocks=blocks,
        thread_ts=thread_ts,
    )


def _settings_handler(ack, body, client):
    log.info("/설정 명령 수신")
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    _post_settings(client, user_id=user_id)

app.command("/설정")(_settings_handler)
app.command("/settings")(_settings_handler)


def _apply_setting_toggle(client, body, setter, label: str):
    """공통 토글 처리 — DB 갱신 후 설정 카드 in-place 업데이트."""
    user_id = body["user"]["id"]
    next_value = (body.get("actions") or [{}])[0].get("value", "")
    enable = next_value == "on"
    try:
        setter(user_id, enable)
    except Exception as e:
        log.exception(f"설정 토글 실패 ({label}, {user_id}): {e}")
        try:
            client.chat_postMessage(channel=user_id,
                                    text="⚠️ 설정 변경에 실패했어요. 잠시 후 다시 시도해주세요.")
        except Exception:
            pass
        return
    fallback, blocks = _settings_blocks(user_id)
    container = body.get("container") or {}
    channel_id = container.get("channel_id") or body.get("channel", {}).get("id")
    message_ts = container.get("message_ts")
    if channel_id and message_ts:
        try:
            client.chat_update(channel=channel_id, ts=message_ts,
                               text=fallback, blocks=blocks)
            return
        except Exception as e:
            log.warning(f"설정 메시지 업데이트 실패: {e}")
    # 업데이트 실패 시 새 메시지로 폴백
    client.chat_postMessage(channel=user_id, text=fallback, blocks=blocks)


@app.action("toggle_briefing")
def handle_toggle_briefing(ack, body, client):
    ack()
    _apply_setting_toggle(client, body, user_store.set_briefing_enabled, "briefing")


@app.action("toggle_start_alarm")
def handle_toggle_start_alarm(ack, body, client):
    ack()
    _apply_setting_toggle(client, body,
                          user_store.set_meeting_start_alarm_enabled,
                          "meeting_start_alarm")


@app.action("meeting_end_now")
def handle_meeting_end_now(ack, body, client):
    """미팅 시작 알람 DM의 '🛑 지금 미팅 끝남' 버튼 — 즉시 end_session 흐름 진입."""
    ack()
    user_id = body["user"]["id"]
    event_id = (body.get("actions") or [{}])[0].get("value", "")
    container = body.get("container") or {}
    msg_ch = container.get("channel_id")
    msg_ts = container.get("message_ts")

    # 활성 세션이 이 이벤트에 바인딩되어 있는지 확인 — 다른 세션 종료 방지
    sess = during_agent._active_sessions.get(user_id)
    if not sess or (event_id and sess.get("event_id") != event_id):
        try:
            client.chat_postMessage(
                channel=user_id,
                text="⚠️ 이 미팅 세션은 이미 종료되었거나 활성 상태가 아닙니다.",
            )
        except Exception:
            pass
        return

    # 중복 클릭 방지 — 버튼 제거하고 진행 중 표시로 갱신
    if msg_ch and msg_ts:
        try:
            client.chat_update(
                channel=msg_ch, ts=msg_ts,
                text="🛑 미팅 종료 처리 중...",
                blocks=[{
                    "type": "section",
                    "text": {"type": "mrkdwn",
                             "text": "🛑 *미팅 종료 처리 중...* 트랜스크립트가 준비되면 회의록 초안을 보내드릴게요."},
                }],
            )
        except Exception:
            pass

    # 백그라운드로 종료 흐름 (Slack 3초 응답 제한 대응)
    threading.Thread(
        target=during_agent.end_session,
        args=(client,),
        kwargs=dict(user_id=user_id),
        daemon=True,
    ).start()


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


def _meeting_edit_handler(ack, body, client):
    """/미팅편집·/미팅수정·/미팅변경 — 향후 미팅 목록 발송 후 사용자가 선택."""
    log.info("/미팅편집 명령 수신")
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    keyword = (body.get("text") or "").strip() or None
    threading.Thread(
        target=before_agent.list_upcoming_meetings_for_edit,
        args=(client, user_id),
        kwargs=dict(keyword=keyword),
        daemon=True,
    ).start()

app.command("/미팅편집")(_meeting_edit_handler)
app.command("/미팅수정")(_meeting_edit_handler)
app.command("/미팅변경")(_meeting_edit_handler)


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
        _post_company_research_result(
            client, user_id=user_id, company=company_name, content=content,
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
        content, fid = research_person(user_id, person_name, company_name, force=True)
        # 프라이버시 가드 — 내부 직원 차단 시 file_id 가 None
        if fid is None:
            client.chat_postMessage(channel=user_id, text=content)
        else:
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
    """텍스트 문서 업로드 처리.

    - I1+: '📎 트랜스크립트 첨부' 대기 중이면 해당 페이로드로 회의록 생성
    - 활성 세션이 있으면: 기존 동작 — 텍스트 추출 후 세션 노트로 추가
    - 활성 세션이 없으면(F4): 문서를 트랜스크립트로 한 회의록 생성 경로 진입
      캘린더 이벤트 없이도 회의록 저장 + 업체/인물 Wiki 갱신 가능
    """
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

    # I1+: '📎 트랜스크립트 첨부' 선택 후 대기 중이면 해당 세션으로 회의록 생성
    from agents.during import (
        consume_pending_uploaded_transcript,
        apply_uploaded_transcript,
    )
    pending_upload = consume_pending_uploaded_transcript(user_id)
    if pending_upload:
        threading.Thread(
            target=apply_uploaded_transcript,
            args=(client, user_id, pending_upload, filename, text),
            daemon=True,
        ).start()
        return

    # F4: 활성 세션이 없으면 문서 기반 회의록 생성 경로로 진입
    if user_id not in _active_sessions and user_id not in _pending_inputs:
        threading.Thread(
            target=start_document_based_minutes,
            args=(client, user_id, filename, text),
            daemon=True,
        ).start()
        return

    # 활성 세션 있음 — 기존 동작 (노트로 추가)
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


# ── 검토 대기 회의록 목록 (`/대기회의록`) ──────────────────────

def _pending_minutes_list_handler(ack, body, client):
    """`/대기회의록` / `/회의록대기` — 검토 대기 회의록 목록 발송."""
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    channel_id = body.get("channel_id") or user_id
    threading.Thread(
        target=post_pending_drafts,
        args=(client,),
        kwargs=dict(user_id=user_id, channel=channel_id),
        daemon=True,
    ).start()

app.command("/대기회의록")(_pending_minutes_list_handler)
app.command("/회의록대기")(_pending_minutes_list_handler)


# ── 회의록 양식 보정 (정리) ─────────────────────────────────

def _minutes_normalize_handler(ack, body, client):
    """`/회의록정리` / `/회의록보정` — 보정 대상 회의록 목록 발송."""
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    keyword = (body.get("text") or "").strip() or None
    threading.Thread(
        target=minutes_normalizer.list_minutes_for_normalize,
        args=(client, user_id),
        kwargs=dict(keyword=keyword),
        daemon=True,
    ).start()

app.command("/회의록정리")(_minutes_normalize_handler)
app.command("/회의록보정")(_minutes_normalize_handler)


# ── Todo Slash Commands (FR-T1, T2) ──────────────────────────

def _todo_list_handler(ack, body, client):
    """`/할일` — 활성 Todo + 최근 완료 5건 출력."""
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    threading.Thread(
        target=todo_agent.handle_list,
        args=(client, user_id),
        daemon=True,
    ).start()

app.command("/할일")(_todo_list_handler)
app.command("/todo")(_todo_list_handler)


def _todo_add_handler(ack, body, client):
    """`/할일추가 [내용]` — 자연어 Todo 추가."""
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    raw = (body.get("text") or "").strip()
    if not raw:
        client.chat_postMessage(
            channel=user_id,
            text="예: `/할일추가 내일까지 AIA 제안서 이슈 작성 #업무`",
        )
        return
    channel_id = body.get("channel_id") or user_id
    threading.Thread(
        target=todo_agent.handle_add,
        args=(client, user_id, raw),
        kwargs=dict(channel=channel_id),
        daemon=True,
    ).start()

app.command("/할일추가")(_todo_add_handler)
app.command("/todo-add")(_todo_add_handler)


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
        # "새 미팅으로 기록" — 모달로 제목·업체·참석자 입력받기 (옵션 A)
        pending = _pending_inputs.get(user_id) or {}
        custom_title = pending.get("custom_title") or ""
        trigger_id = body.get("trigger_id")
        if not trigger_id:
            # trigger_id 없으면 (드물지만 안전장치) 기존 흐름으로 폴백
            handle_event_selection(client, user_id, selected_event_id=None,
                                    custom_title=custom_title or None)
            return
        try:
            during_agent.open_meeting_start_modal(
                client,
                trigger_id=trigger_id,
                user_id=user_id,
                custom_title=custom_title,
                channel=pending.get("session_channel"),
                thread_ts=pending.get("session_thread_ts"),
            )
        except Exception as e:
            log.warning(f"meeting_start_modal 오픈 실패, 폴백: {e}")
            handle_event_selection(client, user_id, selected_event_id=None,
                                    custom_title=custom_title or None)
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


# ── 검토 대기 회의록 액션 핸들러 ──────────────────────────────

@app.action("pending_drafts_view")
def _handle_pending_drafts_view(ack, body, client):
    """[📋 대기 목록 자세히] — 검토 대기 회의록 전체 목록 발송."""
    ack()
    threading.Thread(
        target=handle_pending_view_button,
        args=(client, body),
        daemon=True,
    ).start()


@app.action("pending_draft_review")
def _handle_pending_draft_review(ack, body, client):
    """[📝 검토] — 해당 초안 재발송."""
    ack()
    threading.Thread(
        target=handle_pending_review_button,
        args=(client, body),
        daemon=True,
    ).start()


@app.action("pending_draft_discard")
def _handle_pending_draft_discard(ack, body, client):
    """[🗑️ 버리기] — 단일 초안 삭제."""
    ack()
    threading.Thread(
        target=handle_pending_discard_button,
        args=(client, body),
        daemon=True,
    ).start()


@app.action("pending_drafts_cleanup_all")
def _handle_pending_drafts_cleanup_all(ack, body, client):
    """[🗑️ 모두 정리] — 일괄 삭제 확인 프롬프트 발송."""
    ack()
    threading.Thread(
        target=handle_pending_cleanup_all_button,
        args=(client, body),
        daemon=True,
    ).start()


@app.action("pending_drafts_cleanup_confirm")
def _handle_pending_drafts_cleanup_confirm(ack, body, client):
    """[✅ 모두 버림] — 일괄 삭제 실행."""
    ack()
    threading.Thread(
        target=handle_pending_cleanup_confirm_button,
        args=(client, body),
        daemon=True,
    ).start()


@app.action("pending_drafts_cleanup_cancel")
def _handle_pending_drafts_cleanup_cancel(ack, body, client):
    """[❌ 취소] — 일괄 삭제 확인 취소."""
    ack()
    threading.Thread(
        target=handle_pending_cleanup_cancel_button,
        args=(client, body),
        daemon=True,
    ).start()


# ── 회의록 소스 선택 액션 핸들러 (I1) ────────────────────────

def _handle_minutes_src(ack, body, client):
    """I1: /미팅종료 후 회의록 소스 선택 버튼 콜백 (I5: 본인만 허용)"""
    ack()
    action = body.get("actions", [{}])[0]
    action_id = action.get("action_id", "")
    source = action_id.removeprefix("minutes_src_")  # transcript | upload | notes | wait | cancel
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

for _src in ("transcript", "upload", "notes", "wait", "cancel"):
    app.action(f"minutes_src_{_src}")(_handle_minutes_src)


# ── 사후 회의록 복구 액션 핸들러 ──────────────────────────────

@app.action("recover_meeting_minutes")
def _handle_recover_meeting_minutes(ack, body, client):
    """`/미팅종료` 시 활성 세션이 없을 때 표시된 후보 버튼 콜백."""
    ack()
    user_id = body.get("user", {}).get("id")
    # 본인만 클릭 가능 — _pending_recovery 에 user_id 키가 있으면 그 사용자만 허용
    from agents.during import _pending_recovery
    expected = user_id if user_id in _pending_recovery else user_id
    if not _ensure_creator(client, body, expected):
        return
    threading.Thread(
        target=handle_recover_meeting_minutes_button,
        args=(client, body),
        daemon=True,
    ).start()


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


@app.view(during_agent._MEETING_START_MODAL_CALLBACK)
def handle_meeting_start_modal(ack, body, client):
    """새 미팅 시작 모달 제출 — ad-hoc 세션 생성"""
    ack()
    user_id = body["user"]["id"]
    view = body["view"]
    during_agent.handle_meeting_start_modal(client, user_id, view)


@app.action("add_attendee_to_event")
def handle_add_attendee_button(ack, body, client):
    """미팅 생성 결과 메시지의 '👥 참석자 추가' 버튼 → 모달 오픈"""
    ack()
    user_id = body["user"]["id"]
    action = body.get("actions", [{}])[0]
    event_id = action.get("value", "")
    trigger_id = body.get("trigger_id")
    if not (event_id and trigger_id):
        client.chat_postMessage(channel=user_id,
                                 text="⚠️ 모달을 열 수 없습니다 (event_id 또는 trigger_id 누락).")
        return
    try:
        # 컨텍스트 — 버튼이 게시된 채널/스레드 그대로 사용
        msg_channel = body.get("channel", {}).get("id")
        msg_ts = body.get("message", {}).get("thread_ts") or body.get("message", {}).get("ts")
        before_agent.open_attendee_add_modal(
            client, trigger_id=trigger_id, user_id=user_id, event_id=event_id,
            channel=msg_channel, thread_ts=msg_ts,
        )
    except Exception as e:
        log.warning(f"attendee_add_modal 오픈 실패: {e}")
        client.chat_postMessage(channel=user_id, text=f"⚠️ 모달 오픈 실패: {e}")


@app.view(before_agent._ATTENDEE_ADD_MODAL_CALLBACK)
def handle_attendee_add_modal(ack, body, client):
    """참석자 추가 모달 제출 — 캘린더 이벤트 업데이트"""
    ack()
    user_id = body["user"]["id"]
    view = body["view"]
    before_agent.handle_attendee_add_modal(client, user_id, view)


# ── 미팅 편집·소환 (소환 → 일정 편집 모달 → 취소) ─────────────


@app.action("summon_meeting_for_edit")
def handle_summon_meeting_for_edit(ack, body, client):
    """편집 버튼 (목록·브리핑·자연어) → 캘린더 이벤트 재드래프트화."""
    ack()
    user_id = body["user"]["id"]
    action = body.get("actions", [{}])[0]
    event_id = action.get("value", "")
    if not event_id:
        client.chat_postMessage(channel=user_id, text="⚠️ 미팅 식별 정보가 없습니다.")
        return
    msg_channel = body.get("channel", {}).get("id")
    msg_thread_ts = (body.get("message") or {}).get("thread_ts") or (body.get("message") or {}).get("ts")
    threading.Thread(
        target=before_agent.summon_meeting_draft,
        args=(client, user_id, event_id),
        kwargs=dict(channel=msg_channel, thread_ts=msg_thread_ts),
        daemon=True,
    ).start()


@app.action("edit_meeting_schedule")
def handle_edit_meeting_schedule(ack, body, client):
    """소환된 미팅 메시지의 '📅 일정 편집' 버튼 → 모달 오픈."""
    ack()
    user_id = body["user"]["id"]
    action = body.get("actions", [{}])[0]
    event_id = action.get("value", "")
    trigger_id = body.get("trigger_id")
    if not (event_id and trigger_id):
        client.chat_postMessage(channel=user_id,
                                 text="⚠️ 모달을 열 수 없습니다 (event_id 또는 trigger_id 누락).")
        return
    try:
        msg_channel = body.get("channel", {}).get("id")
        msg_ts = (body.get("message") or {}).get("thread_ts") or (body.get("message") or {}).get("ts")
        before_agent.open_meeting_edit_modal(
            client, trigger_id=trigger_id, user_id=user_id, event_id=event_id,
            channel=msg_channel, thread_ts=msg_ts,
        )
    except Exception as e:
        log.warning(f"meeting_edit_modal 오픈 실패: {e}")
        client.chat_postMessage(channel=user_id, text=f"⚠️ 모달 오픈 실패: {e}")


# ── 회의록 양식 보정 액션 ───────────────────────────────────


@app.action("summon_minutes_for_normalize")
def handle_summon_minutes_for_normalize(ack, body, client):
    """`[🔧 양식 보정]` 버튼 → 진단 + 미리보기 발송."""
    ack()
    user_id = body["user"]["id"]
    action = body.get("actions", [{}])[0]
    file_id = action.get("value", "")
    if not file_id:
        client.chat_postMessage(channel=user_id, text="⚠️ 파일 식별자가 없습니다.")
        return
    msg_channel = body.get("channel", {}).get("id")
    msg_thread_ts = (body.get("message") or {}).get("thread_ts") or (body.get("message") or {}).get("ts")
    threading.Thread(
        target=minutes_normalizer.summon_minutes_for_normalize,
        args=(client, user_id, file_id),
        kwargs=dict(channel=msg_channel, thread_ts=msg_thread_ts),
        daemon=True,
    ).start()


@app.action("normalize_apply_light")
def handle_normalize_apply_light(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    msg_channel = body.get("channel", {}).get("id")
    msg_thread_ts = (body.get("message") or {}).get("thread_ts") or (body.get("message") or {}).get("ts")
    threading.Thread(
        target=minutes_normalizer.apply_light,
        args=(client, user_id),
        kwargs=dict(channel=msg_channel, thread_ts=msg_thread_ts),
        daemon=True,
    ).start()


@app.action("normalize_apply_full")
def handle_normalize_apply_full(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    msg_channel = body.get("channel", {}).get("id")
    msg_thread_ts = (body.get("message") or {}).get("thread_ts") or (body.get("message") or {}).get("ts")
    threading.Thread(
        target=minutes_normalizer.apply_full,
        args=(client, user_id),
        kwargs=dict(channel=msg_channel, thread_ts=msg_thread_ts),
        daemon=True,
    ).start()


@app.action("normalize_confirm_full")
def handle_normalize_confirm_full(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    msg_channel = body.get("channel", {}).get("id")
    msg_thread_ts = (body.get("message") or {}).get("thread_ts") or (body.get("message") or {}).get("ts")
    threading.Thread(
        target=minutes_normalizer.confirm_full,
        args=(client, user_id),
        kwargs=dict(channel=msg_channel, thread_ts=msg_thread_ts),
        daemon=True,
    ).start()


@app.action("normalize_cancel")
def handle_normalize_cancel(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    msg_channel = body.get("channel", {}).get("id")
    msg_thread_ts = (body.get("message") or {}).get("thread_ts") or (body.get("message") or {}).get("ts")
    minutes_normalizer.cancel_normalize(
        client, user_id, channel=msg_channel, thread_ts=msg_thread_ts,
    )


@app.action("cancel_meeting_button")
def handle_cancel_meeting_button(ack, body, client):
    """소환된 미팅 메시지의 '❌ 미팅 취소' 버튼 → 기존 취소 확인 흐름 진입."""
    ack()
    user_id = body["user"]["id"]
    action = body.get("actions", [{}])[0]
    event_id = action.get("value", "")
    if not event_id:
        return
    msg_channel = body.get("channel", {}).get("id")
    msg_ts = (body.get("message") or {}).get("thread_ts") or (body.get("message") or {}).get("ts")
    threading.Thread(
        target=before_agent.trigger_cancel_for_event,
        args=(client, user_id, event_id),
        kwargs=dict(channel=msg_channel, thread_ts=msg_ts),
        daemon=True,
    ).start()


@app.view(before_agent._MEETING_EDIT_MODAL_CALLBACK)
def handle_meeting_edit_modal(ack, body, client):
    """미팅 일정 편집 모달 제출 — cal.update_event 로 변경 사항 적용."""
    ack()
    user_id = body["user"]["id"]
    view = body["view"]
    threading.Thread(
        target=before_agent.handle_meeting_edit_modal,
        args=(client, user_id, view),
        daemon=True,
    ).start()


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
    _send_trello_setup_link(client, user_id)

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


def _trello_weekly_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    channel_id = body.get("channel_id") or user_id
    days_arg = (body.get("text") or "").strip()
    try:
        days = int(days_arg) if days_arg else 7
    except ValueError:
        days = 7
    # 슬래시 커맨드는 thread_ts가 없음 — 채널 발송 시 새 메시지, DM이면 DM 그대로

    def _run():
        try:
            client.chat_postMessage(
                channel=channel_id,
                text=f"📊 주간 Trello 보고서 생성 중… (최근 {days}일)",
            )
            result = trello_report_agent.send_weekly_report(
                client, days=days, channel=channel_id,
            )
            if not result.get("posted"):
                client.chat_postMessage(
                    channel=channel_id,
                    text="⚠️ 보고서 발송 실패. 로그를 확인해주세요.",
                )
        except Exception as e:
            log.exception(f"주간 Trello 보고서 수동 실행 실패: {e}")
            client.chat_postMessage(
                channel=channel_id,
                text=f"❌ 주간 Trello 보고서 생성 실패: {e}",
            )

    threading.Thread(target=_run, daemon=True).start()

app.command("/트렐로주간보고")(_trello_weekly_handler)
app.command("/trello-weekly")(_trello_weekly_handler)


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


# ── Todo 버튼 액션 (FR-T3) ──────────────────────────────────

@app.action("todo_complete_btn")
def handle_todo_complete_btn(ack, body, client):
    ack()
    threading.Thread(
        target=todo_agent.handle_complete_button,
        args=(client, body), daemon=True,
    ).start()


@app.action("todo_cancel_btn")
def handle_todo_cancel_btn(ack, body, client):
    ack()
    threading.Thread(
        target=todo_agent.handle_cancel_button,
        args=(client, body), daemon=True,
    ).start()


@app.action("todo_delete_btn")
def handle_todo_delete_btn(ack, body, client):
    ack()
    threading.Thread(
        target=todo_agent.handle_delete_button,
        args=(client, body), daemon=True,
    ).start()


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
