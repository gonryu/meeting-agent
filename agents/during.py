"""During 에이전트 — 미팅 중 노트 수집 및 사후 회의록 생성

동작 방식:
  - 트랜스크립트 폴링은 항상 실행 (수동 세션 여부와 무관)
  - /미팅종료 시 노트를 저장하고 즉시 1회 트랜스크립트 탐색 (백그라운드 스레드)
  - 폴러가 트랜스크립트 발견 시 노트와 결합하여 회의록 생성
  - 90분 경과 후 트랜스크립트 미수집 시 노트만으로 생성 (fallback)
  - 회의록은 내부용 / 외부용 2종 생성
  - 세션/노트는 .sessions/ 폴더에 JSON으로 백업 → 서버 재시작 시 자동 복구
"""
import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import os
import anthropic
from google import genai

from store import user_store
from tools import drive, docs, calendar as cal
from prompts.briefing import minutes_internal_prompt, minutes_external_prompt
from agents import after

log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

_gemini = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
_GEMINI_MODEL = "gemini-2.0-flash"
_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_CLAUDE_MODEL = "claude-haiku-4-5"

# ── 세션 파일 저장 경로 ──────────────────────────────────────
_SESSIONS_DIR = Path(__file__).parent.parent / ".sessions"

# ── 상태 저장소 ───────────────────────────────────────────────

# 진행 중인 수동 노트 세션
# { user_id: { title, started_at, notes, event_id } }
_active_sessions: dict[str, dict] = {}

# /미팅종료 후 폴러 대기 중인 노트
# { event_id: { user_id, title, notes, started_at, ended_at, stored_at } }
_completed_notes: dict[str, dict] = {}

# 트랜스크립트 처리 완료 이벤트 (중복 방지)
# { user_id: set(event_id) }
_processed_events: dict[str, set] = {}


# ── 세션 파일 저장/복구 헬퍼 ─────────────────────────────────


def _ensure_sessions_dir():
    _SESSIONS_DIR.mkdir(exist_ok=True)


def _save_active_session(user_id: str):
    """진행 중인 세션을 JSON 파일로 저장"""
    data = _active_sessions.get(user_id)
    if data is None:
        return
    try:
        _ensure_sessions_dir()
        path = _SESSIONS_DIR / f"active_{user_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"세션 파일 저장 실패 ({user_id}): {e}")


def _delete_active_session_file(user_id: str):
    """진행 중인 세션 파일 삭제"""
    try:
        (_SESSIONS_DIR / f"active_{user_id}.json").unlink(missing_ok=True)
    except Exception as e:
        log.warning(f"세션 파일 삭제 실패 ({user_id}): {e}")


def _save_completed_note(event_id: str):
    """완료된 노트를 JSON 파일로 저장 (stored_at datetime → ISO string)"""
    data = _completed_notes.get(event_id)
    if data is None:
        return
    try:
        _ensure_sessions_dir()
        path = _SESSIONS_DIR / f"completed_{event_id}.json"
        serializable = {**data, "stored_at": data["stored_at"].isoformat()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"완료 노트 파일 저장 실패 ({event_id}): {e}")


def _delete_completed_note_file(event_id: str):
    """완료된 노트 파일 삭제"""
    try:
        (_SESSIONS_DIR / f"completed_{event_id}.json").unlink(missing_ok=True)
    except Exception as e:
        log.warning(f"완료 노트 파일 삭제 실패 ({event_id}): {e}")


def _save_processed_events(user_id: str):
    """처리 완료 이벤트 목록을 JSON 파일로 저장"""
    events = _processed_events.get(user_id)
    if not events:
        return
    try:
        _ensure_sessions_dir()
        path = _SESSIONS_DIR / f"processed_{user_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(list(events), f, ensure_ascii=False)
    except Exception as e:
        log.warning(f"처리 완료 이벤트 저장 실패 ({user_id}): {e}")


def _load_sessions():
    """서버 시작 시 .sessions/ 폴더에서 세션·노트 복구"""
    if not _SESSIONS_DIR.exists():
        return

    # 진행 중인 세션 복구
    for path in _SESSIONS_DIR.glob("active_*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            user_id = path.stem[len("active_"):]
            _active_sessions[user_id] = data
            log.info(f"세션 복구: {user_id} ({data.get('title')}, "
                     f"노트 {len(data.get('notes', []))}개)")
        except Exception as e:
            log.warning(f"세션 파일 로드 실패 ({path}): {e}")

    # 완료된 노트 복구
    for path in _SESSIONS_DIR.glob("completed_*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            event_id = path.stem[len("completed_"):]
            data["stored_at"] = datetime.fromisoformat(data["stored_at"])
            _completed_notes[event_id] = data
            log.info(f"완료 노트 복구: {event_id} ({data.get('title')}, "
                     f"노트 {len(data.get('notes', []))}개)")
        except Exception as e:
            log.warning(f"완료 노트 파일 로드 실패 ({path}): {e}")

    # 처리 완료 이벤트 복구
    for path in _SESSIONS_DIR.glob("processed_*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                event_ids = json.load(f)
            user_id = path.stem[len("processed_"):]
            _processed_events[user_id] = set(event_ids)
            log.info(f"처리 완료 이벤트 복구: {user_id} ({len(event_ids)}개)")
        except Exception as e:
            log.warning(f"처리 완료 이벤트 파일 로드 실패 ({path}): {e}")


# 모듈 로드 시 자동 복구
_load_sessions()


# ── LLM 헬퍼 ─────────────────────────────────────────────────


def _generate(prompt: str) -> str:
    """텍스트 생성 — Gemini 우선, 실패 시 Claude 폴백"""
    try:
        resp = _gemini.models.generate_content(model=_GEMINI_MODEL, contents=prompt)
        return resp.text.strip()
    except Exception as e:
        log.warning(f"Gemini _generate 실패, Claude로 폴백: {e}")
        msg = _claude.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()


def _get_creds_and_config(user_id: str):
    creds = user_store.get_credentials(user_id)
    user = user_store.get_user(user_id)
    return creds, user.get("minutes_folder_id")


def _post(slack_client, *, user_id: str, channel: str = None,
          thread_ts: str = None, text: str = None, blocks=None):
    slack_client.chat_postMessage(
        channel=channel or user_id,
        thread_ts=thread_ts,
        text=text or "",
        blocks=blocks,
    )


# ── 수동 노트 세션 ─────────────────────────────────────────────


def start_session(slack_client, user_id: str, title: str):
    """/미팅시작 {제목} — 수동 노트 세션 시작"""
    try:
        creds, _ = _get_creds_and_config(user_id)
    except Exception as e:
        _post(slack_client, user_id=user_id, text=f"⚠️ 인증 오류: {e}")
        return

    if user_id in _active_sessions:
        _post(slack_client, user_id=user_id,
              text=f"⚠️ 이미 진행 중인 세션이 있습니다: *{_active_sessions[user_id]['title']}*\n"
                   f"`/미팅종료` 후 다시 시작해주세요.")
        return

    # 진행 중인 캘린더 이벤트 매칭
    event_id = None
    title_to_use = title or "미팅"
    try:
        now = datetime.now(KST)
        events = cal.get_upcoming_meetings(creds, days=1)
        for ev in events:
            parsed = cal.parse_event(ev)
            start_str = parsed.get("start_time", "")
            end_str = ev.get("end", {}).get("dateTime", "")
            if not start_str or not end_str:
                continue
            try:
                start_dt = datetime.fromisoformat(start_str)
                end_dt = datetime.fromisoformat(end_str)
                if (start_dt <= now <= end_dt) or (title_to_use.lower() in parsed["summary"].lower()):
                    event_id = parsed["id"]
                    break
            except Exception:
                pass
    except Exception as e:
        log.warning(f"캘린더 이벤트 매칭 실패: {e}")

    _active_sessions[user_id] = {
        "title": title_to_use,
        "started_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "notes": [],
        "event_id": event_id,
    }
    _save_active_session(user_id)

    event_info = " (캘린더 이벤트 연동됨)" if event_id else ""
    _post(slack_client, user_id=user_id,
          text=f"✅ *{title_to_use}* 노트 세션 시작{event_info}\n"
               f"`/메모 내용` 으로 실시간 메모를 기록하세요.\n"
               f"미팅이 끝나면 `/미팅종료` 를 입력해주세요.")


def add_note(slack_client, user_id: str, note_text: str):
    """/메모 {내용} — 진행 중 세션에 노트 추가"""
    if user_id not in _active_sessions:
        _post(slack_client, user_id=user_id,
              text="⚠️ 진행 중인 세션이 없습니다. `/미팅시작 제목` 으로 먼저 시작해주세요.")
        return

    if not note_text.strip():
        _post(slack_client, user_id=user_id,
              text="⚠️ 노트 내용을 입력해주세요. 예: `/메모 DID 연동 방안 논의`")
        return

    timestamp = datetime.now(KST).strftime("%H:%M")
    _active_sessions[user_id]["notes"].append({"time": timestamp, "text": note_text.strip()})
    _save_active_session(user_id)
    count = len(_active_sessions[user_id]["notes"])
    _post(slack_client, user_id=user_id,
          text=f"📝 노트 #{count} 저장: _{note_text.strip()}_")


def _generate_from_session_end(slack_client, *, user_id: str, event_id: str,
                                title: str, notes: list, started_at: str, ended_at: str):
    """/미팅종료 즉시 실행 — 트랜스크립트 1회 확인 후 결과에 관계없이 회의록 생성."""
    processed = _processed_events.setdefault(user_id, set())
    if event_id in processed:
        return

    try:
        creds, minutes_folder_id = _get_creds_and_config(user_id)
    except Exception as e:
        log.error(f"인증 오류 ({user_id}): {e}")
        _post(slack_client, user_id=user_id, text=f"⚠️ 인증 오류: {e}")
        return

    # 트랜스크립트 1회 탐색
    _post(slack_client, user_id=user_id, text=f"🔍 *{title}* 트랜스크립트 탐색 중...")
    transcript_text = ""
    try:
        transcript_file = drive.find_meet_transcript(creds, title, None)
        if transcript_file:
            log.info(f"트랜스크립트 발견 (즉시): {transcript_file['name']}")
            transcript_text = docs.read_document(creds, transcript_file["id"])
        else:
            log.info(f"트랜스크립트 없음, 노트만으로 생성: {title}")
    except Exception as e:
        log.warning(f"트랜스크립트 탐색 실패: {e}")

    processed.add(event_id)
    _save_processed_events(user_id)

    # Calendar 이벤트에서 날짜·참석자 조회 시도
    date_str = datetime.now(KST).strftime("%Y-%m-%d")
    time_range = f"{started_at.split(' ')[-1]} ~ {ended_at}"
    attendees_str = "정보 없음"
    attendees_raw = []
    try:
        recently_ended = cal.get_recently_ended_meetings(creds, min_minutes_ago=0, max_minutes_ago=90)
        for m in recently_ended:
            if m.get("id") == event_id:
                date_str, time_range, attendees_str = _parse_meeting_meta(m)
                attendees_raw = m.get("attendees", [])
                break
    except Exception as e:
        log.warning(f"Calendar 이벤트 조회 실패: {e}")

    notes_text = _format_notes(notes)
    _generate_and_post_minutes(
        slack_client, user_id=user_id,
        title=title, date_str=date_str, time_range=time_range,
        attendees=attendees_str,
        transcript_text=transcript_text, notes_text=notes_text,
        minutes_folder_id=minutes_folder_id, creds=creds,
        event_id=event_id, attendees_raw=attendees_raw,
    )


def end_session(slack_client, user_id: str):
    """/미팅종료 — 트랜스크립트를 즉시 확인하고 회의록 생성."""
    if user_id not in _active_sessions:
        _post(slack_client, user_id=user_id,
              text="⚠️ 진행 중인 미팅 세션이 없습니다.")
        return

    session = _active_sessions.pop(user_id)
    title = session["title"]
    notes = session["notes"]
    event_id = session["event_id"]
    started_at = session["started_at"]
    ended_at = datetime.now(KST).strftime("%H:%M")

    _delete_active_session_file(user_id)

    note_count = len(notes)
    _post(slack_client, user_id=user_id,
          text=f"✅ *{title}* 세션 종료. 노트 {note_count}개 저장됨.\n"
               f"📡 트랜스크립트를 확인하고 회의록을 생성 중입니다...")

    if event_id:
        threading.Thread(
            target=_generate_from_session_end,
            kwargs=dict(
                slack_client=slack_client,
                user_id=user_id,
                event_id=event_id,
                title=title,
                notes=notes,
                started_at=started_at,
                ended_at=ended_at,
            ),
            daemon=True,
        ).start()
    else:
        # 캘린더 연동 없음 — 동일하게 즉시 생성 (백그라운드 불필요)
        try:
            creds, minutes_folder_id = _get_creds_and_config(user_id)
        except Exception as e:
            _post(slack_client, user_id=user_id, text=f"⚠️ 인증 오류: {e}")
            return

        date_str = datetime.now(KST).strftime("%Y-%m-%d")
        time_range = f"{started_at.split(' ')[-1]} ~ {ended_at}"
        notes_text = _format_notes(notes)
        _generate_and_post_minutes(
            slack_client, user_id=user_id,
            title=title, date_str=date_str, time_range=time_range,
            attendees="정보 없음",
            transcript_text="", notes_text=notes_text,
            minutes_folder_id=minutes_folder_id, creds=creds,
            event_id=None, attendees_raw=[],
        )


# ── 트랜스크립트 폴링 ──────────────────────────────────────────


def check_transcripts(slack_client):
    """APScheduler 10분 주기 호출.
    트랜스크립트 폴링 + 90분 초과 노트 fallback 처리.
    """
    for user in user_store.all_users():
        user_id = user["slack_user_id"]
        try:
            _check_transcripts_for_user(slack_client, user_id)
        except Exception as e:
            log.error(f"트랜스크립트 체크 실패 ({user_id}): {e}")

    # 90분 경과 후에도 처리 못한 노트 → 노트만으로 fallback 생성
    _flush_expired_notes(slack_client)


def _check_transcripts_for_user(slack_client, user_id: str, min_minutes_ago: int = 10):
    """사용자별 트랜스크립트 탐색 및 회의록 생성.
    min_minutes_ago=0 으로 호출하면 방금 종료된 미팅도 즉시 탐색.
    """
    creds, minutes_folder_id = _get_creds_and_config(user_id)
    recently_ended = cal.get_recently_ended_meetings(
        creds, min_minutes_ago=min_minutes_ago, max_minutes_ago=90
    )
    if not recently_ended:
        return

    processed = _processed_events.setdefault(user_id, set())

    for meeting in recently_ended:
        event_id = meeting.get("id")
        if not event_id or event_id in processed:
            continue

        title = meeting.get("summary", "미팅")
        end_str = meeting.get("end_time", "")
        ended_after = None
        if end_str:
            try:
                ended_after = datetime.fromisoformat(end_str) - timedelta(minutes=5)
            except Exception:
                pass

        # 트랜스크립트 탐색 (항상 시도 — 수동 세션 여부 무관)
        try:
            transcript_file = drive.find_meet_transcript(creds, title, ended_after)
        except Exception as e:
            log.warning(f"트랜스크립트 탐색 실패 ({title}): {e}")
            continue

        if not transcript_file:
            log.info(f"트랜스크립트 없음: {title}")
            continue

        log.info(f"트랜스크립트 발견: {transcript_file['name']}")

        # 수동 노트 수집 (파일도 함께 삭제)
        notes_data = _completed_notes.pop(event_id, None)
        if notes_data is not None:
            _delete_completed_note_file(event_id)
        # 진행 중인 세션에서도 노트 수집 (세션 종료 없이 폴러가 먼저 발견하는 경우)
        if notes_data is None:
            for uid, sess in _active_sessions.items():
                if sess.get("event_id") == event_id and uid == user_id:
                    notes_data = {
                        "user_id": user_id,
                        "title": sess["title"],
                        "notes": list(sess["notes"]),
                        "started_at": sess["started_at"],
                        "ended_at": datetime.now(KST).strftime("%H:%M"),
                    }
                    break

        processed.add(event_id)
        _save_processed_events(user_id)

        try:
            transcript_text = docs.read_document(creds, transcript_file["id"])
        except Exception as e:
            log.error(f"트랜스크립트 읽기 실패: {e}")
            _post(slack_client, user_id=user_id,
                  text=f"⚠️ *{title}* 트랜스크립트 읽기 실패: {e}")
            continue

        # 날짜/시간/참석자 파싱
        date_str, time_range, attendees_str = _parse_meeting_meta(meeting)
        notes_text = _format_notes(notes_data["notes"] if notes_data else [])

        _post(slack_client, user_id=user_id,
              text=f"📝 *{title}* 트랜스크립트 수집 완료. 회의록을 생성 중입니다...")

        _generate_and_post_minutes(
            slack_client, user_id=user_id,
            title=title, date_str=date_str, time_range=time_range,
            attendees=attendees_str,
            transcript_text=transcript_text,
            notes_text=notes_text,
            minutes_folder_id=minutes_folder_id, creds=creds,
            event_id=event_id,
            attendees_raw=meeting.get("attendees", []),
        )


def _flush_expired_notes(slack_client):
    """90분 이상 경과한 노트 → 트랜스크립트 없이 노트만으로 fallback 생성"""
    now = datetime.now(KST)
    expired = [
        eid for eid, data in _completed_notes.items()
        if (now - data["stored_at"]).total_seconds() > 90 * 60
    ]
    for event_id in expired:
        data = _completed_notes.pop(event_id)
        _delete_completed_note_file(event_id)
        user_id = data["user_id"]
        title = data["title"]
        processed = _processed_events.setdefault(user_id, set())
        if event_id in processed:
            continue
        processed.add(event_id)
        _save_processed_events(user_id)

        log.info(f"노트 fallback 처리 (트랜스크립트 없음): {title}")
        _post(slack_client, user_id=user_id,
              text=f"⏰ *{title}* 트랜스크립트를 수집하지 못했습니다. 노트만으로 회의록을 생성합니다.")

        try:
            creds, minutes_folder_id = _get_creds_and_config(user_id)
        except Exception as e:
            log.error(f"fallback 인증 오류 ({user_id}): {e}")
            continue

        date_str = data["stored_at"].strftime("%Y-%m-%d")
        time_range = f"{data['started_at'].split(' ')[-1]} ~ {data['ended_at']}"
        notes_text = _format_notes(data["notes"])

        _generate_and_post_minutes(
            slack_client, user_id=user_id,
            title=title, date_str=date_str, time_range=time_range,
            attendees="정보 없음",
            transcript_text="", notes_text=notes_text,
            minutes_folder_id=minutes_folder_id, creds=creds,
            event_id=event_id, attendees_raw=[],
        )


# ── 회의록 생성 공통 ──────────────────────────────────────────


def _generate_and_post_minutes(slack_client, *, user_id: str, title: str,
                                date_str: str, time_range: str, attendees: str,
                                transcript_text: str, notes_text: str,
                                minutes_folder_id, creds,
                                event_id: str | None = None,
                                attendees_raw: list | None = None):
    """내부용·외부용 회의록 생성 → Drive 저장 → Slack 발송 → After Agent 트리거"""
    meeting_date = f"{date_str} {time_range}".strip()

    # 입력 소스 표기
    sources = []
    if transcript_text:
        sources.append("트랜스크립트")
    if notes_text:
        sources.append("수동 노트")
    source_label = " + ".join(sources) if sources else "없음"

    # ── 내부용 생성 ──
    _post(slack_client, user_id=user_id, text=f"✍️ *{title}* 내부용 회의록 생성 중... (1/2)")
    try:
        internal_body = _generate(
            minutes_internal_prompt(title, meeting_date, attendees,
                                    transcript_text, notes_text)
        )
    except Exception as e:
        log.error(f"내부용 회의록 생성 실패: {e}")
        internal_body = f"## 회의 요약\n(생성 실패: {e})\n\n## 원본\n{notes_text or transcript_text[:2000]}"

    # ── 외부용 생성 ──
    _post(slack_client, user_id=user_id, text=f"✍️ *{title}* 외부용 회의록 생성 중... (2/2)")
    try:
        external_body = _generate(
            minutes_external_prompt(title, meeting_date, attendees, internal_body)
        )
    except Exception as e:
        log.error(f"외부용 회의록 생성 실패: {e}")
        external_body = f"## 회의 개요\n(생성 실패: {e})\n"

    # ── Drive 저장 ──
    _post(slack_client, user_id=user_id, text=f"💾 *{title}* Drive에 회의록 저장 중...")
    internal_file_id = external_file_id = None
    if minutes_folder_id:
        # 내부용
        internal_content = _build_minutes_content(
            title, date_str, time_range, attendees, source_label,
            internal_body, transcript_text, notes_text, kind="내부용"
        )
        # 외부용 (원본 첨부 없음)
        external_content = _build_minutes_content(
            title, date_str, time_range, attendees, source_label,
            external_body, "", "", kind="외부용"
        )
        try:
            internal_file_id = drive.save_minutes(
                creds, minutes_folder_id,
                f"{date_str}_{title}_내부용.md", internal_content
            )
            external_file_id = drive.save_minutes(
                creds, minutes_folder_id,
                f"{date_str}_{title}_외부용.md", external_content
            )
            log.info(f"회의록 저장: {title} 내부용={internal_file_id} 외부용={external_file_id}")
        except Exception as e:
            log.error(f"회의록 Drive 저장 실패: {e}")

    # ── Slack 발송 ──
    _post_combined_minutes(
        slack_client, user_id=user_id,
        title=title, source_label=source_label,
        internal_body=internal_body, external_body=external_body,
        internal_file_id=internal_file_id,
        external_file_id=external_file_id,
    )

    # ── After Agent 백그라운드 실행 ──
    threading.Thread(
        target=after.trigger_after_meeting,
        kwargs=dict(
            slack_client=slack_client,
            user_id=user_id,
            event_id=event_id,
            title=title,
            date_str=date_str,
            attendees_raw=attendees_raw or [],
            internal_body=internal_body,
            external_body=external_body,
            creds=creds,
        ),
        daemon=True,
    ).start()


def _build_minutes_content(title: str, date_str: str, time_range: str,
                            attendees: str, source: str, body: str,
                            transcript_text: str, notes_text: str,
                            kind: str) -> str:
    """Drive 저장용 마크다운 파일 내용 구성"""
    lines = [
        f"# {title} ({kind})",
        "",
        "## 기본 정보",
        f"- 날짜: {date_str}",
        f"- 시간: {time_range}",
        f"- 참석자: {attendees}",
        f"- 입력 소스: {source}",
        f"- 구분: {kind}",
        "",
        body,
    ]
    if kind == "내부용":
        if transcript_text:
            preview = transcript_text[:3000]
            suffix = "..." if len(transcript_text) > 3000 else ""
            lines += ["", "---", "## 원본 트랜스크립트", preview + suffix]
        if notes_text:
            lines += ["", "---", "## 원본 수동 노트", notes_text]
    return "\n".join(lines)


def _post_combined_minutes(slack_client, *, user_id: str, title: str,
                            source_label: str, internal_body: str, external_body: str,
                            internal_file_id: str | None, external_file_id: str | None):
    """내부용·외부용 회의록 Drive 링크를 Slack으로 발송"""
    def drive_link(file_id: str) -> str:
        return f"https://drive.google.com/file/d/{file_id}/view"

    if internal_file_id:
        internal_line = f"📄 *내부용*: {drive_link(internal_file_id)}"
    else:
        internal_line = "📄 *내부용*: Drive 저장 실패"

    if external_file_id:
        external_line = f"📤 *외부용* (상대방 공유 가능): {drive_link(external_file_id)}"
    else:
        external_line = "📤 *외부용*: Drive 저장 실패"

    slack_client.chat_postMessage(
        channel=user_id,
        text=(
            f"*📋 회의록이 생성되었습니다: {title}*  |  _소스: {source_label}_\n"
            f"{internal_line}\n"
            f"{external_line}"
        ),
    )


# ── 유틸리티 ──────────────────────────────────────────────────


def _format_notes(notes: list) -> str:
    """노트 리스트를 텍스트로 변환"""
    if not notes:
        return ""
    return "\n".join(f"[{n['time']}] {n['text']}" for n in notes)


def _parse_meeting_meta(meeting: dict) -> tuple[str, str, str]:
    """meeting dict에서 date_str, time_range, attendees_str 추출"""
    start_str = meeting.get("start_time", "")
    end_str = meeting.get("end_time", "")
    attendees_list = meeting.get("attendees", [])

    date_str = datetime.now(KST).strftime("%Y-%m-%d")
    time_range = ""
    try:
        start_dt = datetime.fromisoformat(start_str)
        end_dt = datetime.fromisoformat(end_str)
        date_str = start_dt.strftime("%Y-%m-%d")
        time_range = f"{start_dt.strftime('%H:%M')} ~ {end_dt.strftime('%H:%M')}"
    except Exception:
        pass

    attendees_str = ", ".join(
        a.get("name") or a.get("email", "") for a in attendees_list
    ) or "정보 없음"

    return date_str, time_range, attendees_str


# ── 회의록 목록 조회 ──────────────────────────────────────────


def get_minutes_list(slack_client, user_id: str):
    """/회의록 — 저장된 회의록 목록 조회"""
    try:
        creds, minutes_folder_id = _get_creds_and_config(user_id)
    except Exception as e:
        _post(slack_client, user_id=user_id, text=f"⚠️ 인증 오류: {e}")
        return

    if not minutes_folder_id:
        _post(slack_client, user_id=user_id,
              text="⚠️ Minutes 폴더가 설정되지 않았습니다. `/재등록` 으로 재인증해주세요.")
        return

    try:
        files = drive.list_minutes(creds, minutes_folder_id)
    except Exception as e:
        _post(slack_client, user_id=user_id, text=f"⚠️ 회의록 조회 실패: {e}")
        return

    if not files:
        _post(slack_client, user_id=user_id, text="📁 저장된 회의록이 없습니다.")
        return

    lines = [f"*📋 저장된 회의록 ({len(files)}개)*"]
    for f in files[:10]:
        name = f.get("name", "").replace(".md", "")
        modified = f.get("modifiedTime", "")[:10]
        file_id = f.get("id", "")
        link = f"https://drive.google.com/file/d/{file_id}/view" if file_id else ""
        link_text = f"  <{link}|열기>" if link else ""
        lines.append(f"• {name}  _{modified}_{link_text}")
    if len(files) > 10:
        lines.append(f"_...외 {len(files) - 10}개_")

    _post(slack_client, user_id=user_id, text="\n".join(lines))
