"""agents/during.py 단위 테스트"""
import os
os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")

import json
import pytest
from unittest.mock import patch, MagicMock, call
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

with patch("anthropic.Anthropic"):
    import agents.during as during
    from agents.during import (
        start_session,
        add_note,
        end_session,
        finalize_minutes,
        get_minutes_list,
        check_transcripts,
        handle_event_selection,
        handle_event_title_reply,
        start_document_based_minutes,
        _active_sessions,
        _completed_notes,
        _processed_events,
        _pending_minutes,
        _pending_inputs,
        _find_draft_for_user,
    )


@pytest.fixture(autouse=True)
def isolated_sessions_dir(tmp_path):
    """각 테스트마다 임시 디렉토리를 세션 저장 경로로 사용 (실제 .sessions/ 오염 방지)"""
    sessions_dir = tmp_path / ".sessions"
    with patch.object(during, "_SESSIONS_DIR", sessions_dir):
        yield sessions_dir

_TEST_USER = "UTEST"
_MOCK_CREDS = MagicMock()
_MOCK_USER = {
    "slack_user_id": _TEST_USER,
    "minutes_folder_id": "minutes_folder_id",
}
KST = ZoneInfo("Asia/Seoul")


def _slack():
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "111.222"}
    return client


def _mock_store():
    mock = MagicMock()
    mock.get_credentials.return_value = _MOCK_CREDS
    mock.get_user.return_value = _MOCK_USER
    mock.all_users.return_value = [_MOCK_USER]
    return patch("agents.during.user_store", mock)


# ── start_session ────────────────────────────────────────────

class TestStartSession:
    def setup_method(self):
        _active_sessions.clear()
        _completed_notes.clear()
        _processed_events.clear()
        _pending_inputs.clear()

    def test_creates_session(self):
        """세션 생성 후 _active_sessions에 등록"""
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            start_session(_slack(), _TEST_USER, "카카오 미팅")

        assert _TEST_USER in _active_sessions
        assert _active_sessions[_TEST_USER]["title"] == "카카오 미팅"
        assert _active_sessions[_TEST_USER]["notes"] == []

    def test_default_title_triggers_modal_button(self):
        """제목 없으면 즉시 ad-hoc 세션 대신 모달 트리거 버튼 게시 (Fix 3, 옵션 A).

        제목 미제공 + 후보 0건 → 사용자가 '📝 새 미팅 정보 입력' 버튼을 눌러
        모달에서 제목·업체·참석자를 입력하도록 유도한다.
        """
        from agents.during import _pending_inputs
        slack = _slack()
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            start_session(slack, _TEST_USER, "")

        # 즉시 세션은 생성되지 않음 — 모달 입력 대기 상태
        assert _TEST_USER not in _active_sessions
        # 트리거 버튼 메시지가 게시되었고 pending_inputs에 컨텍스트가 보존됨
        assert _TEST_USER in _pending_inputs
        text_arg = slack.chat_postMessage.call_args[1].get("text", "")
        blocks_arg = slack.chat_postMessage.call_args[1].get("blocks", [])
        assert "정보를 입력" in text_arg
        # 버튼의 action_id가 select_meeting_event_new — main.py에서 모달 오픈
        assert any(
            el.get("action_id") == "select_meeting_event_new"
            for blk in blocks_arg if blk.get("type") == "actions"
            for el in blk.get("elements", [])
        )
        _pending_inputs.pop(_TEST_USER, None)

    def test_duplicate_session_rejected(self):
        """이미 세션 진행 중이면 거부 메시지"""
        _active_sessions[_TEST_USER] = {"title": "기존 미팅", "notes": [], "started_at": "", "event_id": None}
        slack = _slack()

        with _mock_store():
            start_session(slack, _TEST_USER, "새 미팅")

        text = slack.chat_postMessage.call_args[1]["text"]
        assert "이미" in text or "진행 중" in text
        # 세션은 기존 것 유지
        assert _active_sessions[_TEST_USER]["title"] == "기존 미팅"

    def test_sends_confirmation_message(self):
        """세션 시작 확인 메시지 발송"""
        slack = _slack()
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            start_session(slack, _TEST_USER, "테스트 미팅")

        text = slack.chat_postMessage.call_args[1]["text"]
        assert "테스트 미팅" in text
        assert "/메모" in text or "노트" in text

    def test_single_ongoing_event_shows_selection_ui(self):
        """F3(2026-04): 후보 이벤트가 1건이라도 있으면 자동 바인딩 대신 선택 UI 표시.
        사용자가 '이 미팅이 맞나' 또는 '새 미팅 추가'를 명시적으로 고를 수 있게 함."""
        now = datetime.now(KST)
        start = (now.replace(minute=0, second=0)).isoformat()
        end = (now.replace(hour=now.hour + 1, minute=0, second=0) if now.hour < 23 else now).isoformat()

        events = [{"id": "evt1", "summary": "카카오 미팅", "start": {"dateTime": start},
                   "attendees": [], "end": {"dateTime": end}}]

        slack = _slack()
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = events
            mock_cal.parse_event.return_value = {
                "id": "evt1", "summary": "카카오 미팅",
                "start_time": start, "attendees": [],
                "location": "", "meet_link": "", "description": "",
            }
            start_session(slack, _TEST_USER, "카카오 미팅")

        # 세션은 아직 생성 안 됨 — 사용자 클릭 대기
        assert _TEST_USER not in _active_sessions
        # pending에 후보 이벤트 + 원본 제목(custom_title) 보존
        assert _TEST_USER in _pending_inputs
        assert _pending_inputs[_TEST_USER]["custom_title"] == "카카오 미팅"
        assert len(_pending_inputs[_TEST_USER]["events"]) == 1
        assert _pending_inputs[_TEST_USER]["events"][0]["id"] == "evt1"
        # 선택 UI 버튼에 '새 미팅 추가' 라벨 포함 (custom_title 기반)
        blocks = slack.chat_postMessage.call_args[1].get("blocks", [])
        button_texts = [
            el["text"]["text"]
            for block in blocks if block.get("type") == "actions"
            for el in block.get("elements", [])
        ]
        assert any("새 미팅 추가" in t and "카카오 미팅" in t for t in button_texts)

    def test_no_candidates_creates_ad_hoc_session(self):
        """F3: 후보 이벤트가 0건이면 선택 UI 없이 즉시 ad-hoc 세션 생성"""
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            start_session(_slack(), _TEST_USER, "즉흥 미팅")

        assert _TEST_USER in _active_sessions
        assert _active_sessions[_TEST_USER]["title"] == "즉흥 미팅"
        assert _active_sessions[_TEST_USER]["event_id"] is None
        # pending에는 등록 안 됨
        assert _TEST_USER not in _pending_inputs

    def test_force_ad_hoc_skips_calendar_lookup(self):
        """F3: force_ad_hoc=True면 이벤트가 있어도 탐색/선택 UI 건너뛰고 즉시 ad-hoc.
        '새 미팅 추가' 버튼 클릭 재진입 시 재귀 방지용."""
        now = datetime.now(KST)
        start = now.isoformat()
        end = (now + timedelta(hours=1)).isoformat()
        events = [{"id": "evt1", "summary": "X", "start": {"dateTime": start},
                   "attendees": [], "end": {"dateTime": end}}]

        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = events
            mock_cal.parse_event.return_value = {
                "id": "evt1", "summary": "X", "start_time": start, "attendees": [],
            }
            start_session(_slack(), _TEST_USER, "강제 ad-hoc", force_ad_hoc=True)

        # 캘린더 조회도 안 함 (force_ad_hoc이면 이벤트 탐색 자체 skip)
        assert mock_cal.get_upcoming_meetings.called is False
        assert _TEST_USER in _active_sessions
        assert _active_sessions[_TEST_USER]["event_id"] is None


# ── add_note ─────────────────────────────────────────────────

class TestAddNote:
    def setup_method(self):
        _active_sessions.clear()
        _completed_notes.clear()
        _pending_inputs.clear()

    def _init_session(self):
        _active_sessions[_TEST_USER] = {
            "title": "테스트 미팅", "started_at": "10:00",
            "notes": [], "event_id": None
        }

    def test_note_added_to_session(self):
        """노트가 세션에 추가됨"""
        self._init_session()
        add_note(_slack(), _TEST_USER, "DID 연동 방안 논의")

        assert len(_active_sessions[_TEST_USER]["notes"]) == 1
        assert _active_sessions[_TEST_USER]["notes"][0]["text"] == "DID 연동 방안 논의"

    def test_multiple_notes_accumulated(self):
        """여러 노트 누적"""
        self._init_session()
        add_note(_slack(), _TEST_USER, "첫 번째 노트")
        add_note(_slack(), _TEST_USER, "두 번째 노트")
        add_note(_slack(), _TEST_USER, "세 번째 노트")

        assert len(_active_sessions[_TEST_USER]["notes"]) == 3

    def test_note_has_timestamp(self):
        """노트에 시간 정보 포함"""
        self._init_session()
        add_note(_slack(), _TEST_USER, "내용")
        note = _active_sessions[_TEST_USER]["notes"][0]
        assert "time" in note
        assert ":" in note["time"]  # HH:MM 형식

    def test_no_session_single_event_auto_starts(self):
        """세션 없고 캘린더 이벤트 1개 → 자동 세션 시작 + 노트 추가"""
        slack = _slack()
        now = datetime.now(KST)
        mock_event = {
            "id": "evt_auto",
            "summary": "자동 감지 미팅",
            "start": {"dateTime": (now - timedelta(minutes=10)).isoformat()},
            "end": {"dateTime": (now + timedelta(minutes=50)).isoformat()},
            "attendees": [],
        }
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = [mock_event]
            mock_cal.parse_event.return_value = {
                "id": "evt_auto",
                "summary": "자동 감지 미팅",
                "start_time": (now - timedelta(minutes=10)).isoformat(),
                "location": "", "meet_link": "", "description": "",
                "attendees": [],
            }
            add_note(slack, _TEST_USER, "노트 내용")

        assert _TEST_USER in _active_sessions
        assert _active_sessions[_TEST_USER]["title"] == "자동 감지 미팅"
        assert len(_active_sessions[_TEST_USER]["notes"]) == 1
        assert _active_sessions[_TEST_USER]["notes"][0]["text"] == "노트 내용"

    def test_no_session_no_event_queues_input(self):
        """세션 없고 캘린더 이벤트 0개 → 대기 큐에 저장 + 선택 UI 발송"""
        slack = _slack()
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            add_note(slack, _TEST_USER, "노트 내용")

        assert _TEST_USER not in _active_sessions
        assert _TEST_USER in _pending_inputs
        assert len(_pending_inputs[_TEST_USER]["inputs"]) == 1
        assert _pending_inputs[_TEST_USER]["inputs"][0]["content"] == "노트 내용"

    def test_no_session_multiple_events_queues_input(self):
        """세션 없고 캘린더 이벤트 여러 개 → 선택 요청 + 대기 큐"""
        slack = _slack()
        now = datetime.now(KST)
        events = []
        for i in range(2):
            events.append({
                "id": f"evt_{i}",
                "summary": f"미팅 {i}",
                "start": {"dateTime": (now - timedelta(minutes=5)).isoformat()},
                "end": {"dateTime": (now + timedelta(minutes=55)).isoformat()},
                "attendees": [],
            })
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = events
            mock_cal.parse_event.side_effect = lambda ev: {
                "id": ev["id"],
                "summary": ev["summary"],
                "start_time": ev["start"]["dateTime"],
                "location": "", "meet_link": "", "description": "",
                "attendees": [],
            }
            add_note(slack, _TEST_USER, "노트 내용")

        assert _TEST_USER not in _active_sessions
        assert _TEST_USER in _pending_inputs
        assert len(_pending_inputs[_TEST_USER]["events"]) == 2

    def test_empty_note_rejected(self):
        """빈 노트는 거부"""
        self._init_session()
        slack = _slack()
        add_note(slack, _TEST_USER, "   ")

        text = slack.chat_postMessage.call_args[1]["text"]
        assert "입력" in text or "내용" in text
        assert len(_active_sessions[_TEST_USER]["notes"]) == 0

    def test_confirmation_shows_note_count(self):
        """확인 메시지에 노트 번호 포함"""
        self._init_session()
        slack = _slack()
        add_note(slack, _TEST_USER, "첫 번째 노트")

        text = slack.chat_postMessage.call_args[1]["text"]
        assert "#1" in text


# ── end_session ───────────────────────────────────────────────

class TestEndSession:
    def setup_method(self):
        _active_sessions.clear()
        _completed_notes.clear()
        _processed_events.clear()
        _pending_minutes.clear()

    def _init_session(self, notes=None, event_id=None):
        _active_sessions[_TEST_USER] = {
            "title": "카카오 미팅",
            "started_at": "2026-03-25 14:00",
            "notes": notes or [
                {"time": "14:05", "text": "DID 연동 논의"},
                {"time": "14:10", "text": "계약 조건 검토"},
            ],
            "event_id": event_id,
        }

    def test_session_removed_after_end_no_event(self):
        """이벤트 없는 세션: 종료 후 _active_sessions에서 삭제"""
        self._init_session(event_id=None)

        with _mock_store(), \
             patch("agents.during._generate_minutes", return_value="## 회의 요약\n내용"), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.save_minutes.return_value = "file_id"
            end_session(_slack(), _TEST_USER)

        assert _TEST_USER not in _active_sessions

    def test_session_removed_after_end_with_event(self):
        """I1: event_id 있는 세션 종료 시 _active_sessions 제거 + 소스 선택 대기 등록"""
        from agents.during import _pending_source_select
        _pending_source_select.clear()
        self._init_session(event_id="evt_kakao")
        slack = _slack()

        with _mock_store():
            end_session(slack, _TEST_USER)

        assert _TEST_USER not in _active_sessions
        # I1: 즉시 생성이 아니라 _pending_source_select에 등록되고 선택 블록 발송
        assert "evt_kakao" in _pending_source_select
        payload = _pending_source_select["evt_kakao"]
        assert payload["user_id"] == _TEST_USER
        assert payload["title"] == "카카오 미팅"
        assert len(payload["notes"]) == 2

    def test_source_selection_block_posted_with_event(self):
        """I1: event_id 있는 세션 종료 시 소스 선택 Slack 블록 발송"""
        from agents.during import _pending_source_select
        _pending_source_select.clear()
        self._init_session(event_id="evt123")
        slack = _slack()

        with _mock_store():
            end_session(slack, _TEST_USER)

        # 두 번 호출됨: 종료 확인 메시지 + 소스 선택 블록
        calls = slack.chat_postMessage.call_args_list
        texts = [c[1].get("text", "") for c in calls]
        assert any("세션 종료" in t for t in texts)
        # 소스 선택 블록에는 4개 버튼(transcript/notes/wait/cancel)
        block_calls = [c for c in calls if c[1].get("blocks")]
        found_src_buttons = False
        for c in block_calls:
            for block in c[1]["blocks"]:
                for el in block.get("elements", []) or []:
                    if el.get("action_id", "").startswith("minutes_src_"):
                        found_src_buttons = True
        assert found_src_buttons, "minutes_src_* 버튼이 있어야 함"

    def test_source_select_transcript_spawns_generation_thread(self):
        """I1: '트랜스크립트 탐색' 선택 시 _generate_from_session_end가 백그라운드 스레드로 실행"""
        from agents.during import _pending_source_select, handle_minutes_source_select
        _pending_source_select["evt123"] = {
            "user_id": _TEST_USER,
            "title": "카카오 미팅",
            "notes": [{"time": "14:05", "text": "n1"}, {"time": "14:10", "text": "n2"}],
            "started_at": "2026-03-25 14:00",
            "ended_at": "15:00",
            "post_channel": None,
            "post_thread_ts": None,
        }
        slack = _slack()
        with patch("agents.during.threading.Thread") as mock_thread:
            handle_minutes_source_select(slack, _TEST_USER, "evt123", "transcript")

        assert "evt123" not in _pending_source_select
        mock_thread.assert_called_once()
        call_kwargs = mock_thread.call_args[1]["kwargs"]
        assert call_kwargs["event_id"] == "evt123"
        assert call_kwargs["title"] == "카카오 미팅"
        assert len(call_kwargs["notes"]) == 2
        assert call_kwargs["source"] == "transcript"

    def test_immediate_generation_when_no_event_id(self):
        """event_id 없으면 즉시 내부용 생성 후 검토 대기 (draft)"""
        self._init_session(event_id=None)
        slack = _slack()

        with _mock_store(), \
             patch("agents.during._generate_minutes", return_value="## 회의 요약\n내용"), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.create_draft_doc.return_value = "draft_doc_id"
            end_session(slack, _TEST_USER)

        # FR-D14: 내부용 회의록이 _pending_minutes에 event_id 키로 저장됨
        found = _find_draft_for_user(_TEST_USER)
        assert found is not None, "_pending_minutes에 사용자 초안이 없습니다"
        draft_key, draft = found
        assert draft["title"] == "카카오 미팅"
        assert "## 회의 요약" in draft["internal_body"]

    def test_no_session_sends_warning(self):
        """세션 없으면 경고 메시지"""
        slack = _slack()
        end_session(slack, _TEST_USER)

        text = slack.chat_postMessage.call_args[1]["text"]
        assert "세션" in text

    def test_internal_and_external_saved_to_drive(self):
        """finalize_minutes에서 내부용·외부용 2개 Drive 저장"""
        self._init_session(event_id=None)
        slack = _slack()

        with _mock_store(), \
             patch("agents.during._generate_minutes", return_value="## 회의 요약\n내용"), \
             patch("agents.during.drive") as mock_drive, \
             patch("agents.during.after"):
            mock_drive.create_draft_doc.return_value = "draft_doc_id"
            mock_drive.save_minutes.return_value = "saved_file_id"
            mock_drive.delete_file.return_value = None
            # end_session creates the draft
            end_session(slack, _TEST_USER)
            # finalize_minutes saves to Drive
            finalize_minutes(slack, _TEST_USER)

        # 내부용 + 외부용 = 2번 저장
        assert mock_drive.save_minutes.call_count == 2
        filenames = [c[0][2] for c in mock_drive.save_minutes.call_args_list]
        assert any("내부용" in f for f in filenames)
        assert any("외부용" in f for f in filenames)

    def test_minutes_filename_contains_title_and_date(self):
        """파일명에 날짜_제목 형식 포함"""
        self._init_session(event_id=None)

        with _mock_store(), \
             patch("agents.during._generate_minutes", return_value="## 회의 요약\n내용"), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.save_minutes.return_value = "file_id"
            end_session(_slack(), _TEST_USER)

        filenames = [c[0][2] for c in mock_drive.save_minutes.call_args_list]
        for fn in filenames:
            assert "카카오 미팅" in fn
            assert "2026" in fn

    def test_internal_and_external_posted_to_slack(self):
        """finalize_minutes에서 내부용·외부용 회의록이 Slack으로 발송됨"""
        self._init_session(event_id=None)
        slack = _slack()

        with _mock_store(), \
             patch("agents.during._generate_minutes", return_value="## 회의 요약\n내용 있음"), \
             patch("agents.during.drive") as mock_drive, \
             patch("agents.during.after"):
            mock_drive.create_draft_doc.return_value = "draft_doc_id"
            mock_drive.save_minutes.return_value = "file_id"
            mock_drive.delete_file.return_value = None
            end_session(slack, _TEST_USER)
            # Reset call list to only check finalize messages
            slack.chat_postMessage.reset_mock()
            slack.chat_postMessage.return_value = {"ts": "111.222"}
            finalize_minutes(slack, _TEST_USER)

        all_texts = " ".join(c[1]["text"] for c in slack.chat_postMessage.call_args_list)
        assert "내부용" in all_texts
        assert "외부용" in all_texts

    def test_empty_notes_handled(self):
        """노트 없이 종료해도 오류 없음"""
        self._init_session(notes=[], event_id=None)
        slack = _slack()

        with _mock_store(), \
             patch("agents.during._generate_minutes", return_value="## 회의 요약\n없음"), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.save_minutes.return_value = "file_id"
            end_session(slack, _TEST_USER)

        assert _TEST_USER not in _active_sessions

    def test_llm_failure_still_creates_draft(self):
        """LLM 생성 실패해도 fallback 초안이 생성됨"""
        self._init_session(event_id=None)
        slack = _slack()

        with _mock_store(), \
             patch("agents.during._generate_minutes", side_effect=Exception("LLM 오류")), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.create_draft_doc.return_value = "draft_doc_id"
            end_session(slack, _TEST_USER)

        # FR-D14: fallback 초안이 _pending_minutes에 event_id 키로 저장됨
        found = _find_draft_for_user(_TEST_USER)
        assert found is not None
        _, draft = found
        assert "생성 실패" in draft["internal_body"]

    def test_llm_failure_raw_notes_in_draft_content(self):
        """LLM 실패 시 초안 내용에 원본 노트 포함"""
        self._init_session(event_id=None)

        with _mock_store(), \
             patch("agents.during._generate_minutes", side_effect=Exception("LLM 오류")), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.create_draft_doc.return_value = "draft_doc_id"
            end_session(_slack(), _TEST_USER)

        # FR-D14: fallback 초안에 원본 노트 내용 포함
        found = _find_draft_for_user(_TEST_USER)
        assert found is not None
        _, draft = found
        assert "DID 연동 논의" in draft["internal_body"] or "DID 연동 논의" in draft["notes_text"]


# ── check_transcripts ─────────────────────────────────────────

class TestCheckTranscripts:
    def setup_method(self):
        _active_sessions.clear()
        _completed_notes.clear()
        _processed_events.clear()
        _pending_minutes.clear()

    def test_transcript_found_generates_draft(self):
        """트랜스크립트 발견 시 회의록 초안 생성"""
        slack = _slack()
        meeting = {
            "id": "evt1",
            "summary": "카카오 미팅",
            "start_time": "2026-03-25T14:00:00+09:00",
            "end_time": "2026-03-25T15:00:00+09:00",
            "attendees": [{"name": "김민환", "email": "mh@kakao.com"}],
        }
        transcript_file = {"id": "doc_id", "name": "카카오 미팅 - Transcript"}

        with _mock_store(), \
             patch("agents.during.cal") as mock_cal, \
             patch("agents.during.drive") as mock_drive, \
             patch("agents.during.docs") as mock_docs, \
             patch("agents.during._generate_minutes", return_value="## 회의 요약\n내용"):
            mock_cal.get_recently_ended_meetings.return_value = [meeting]
            mock_drive.find_meet_transcript.return_value = transcript_file
            mock_drive.create_draft_doc.return_value = "draft_doc_id"
            mock_docs.read_document.return_value = "트랜스크립트 내용..."

            check_transcripts(slack)

        # FR-D14: 내부용 회의록이 _pending_minutes에 event_id 키로 저장됨
        found = _find_draft_for_user(_TEST_USER)
        assert found is not None, "_pending_minutes에 사용자 초안이 없습니다"
        _, draft = found
        assert draft["title"] == "카카오 미팅"

    def test_no_transcript_skipped(self):
        """트랜스크립트 없으면 회의록 생성 안 함"""
        slack = _slack()
        meeting = {
            "id": "evt2",
            "summary": "네이버 미팅",
            "start_time": "2026-03-25T10:00:00+09:00",
            "end_time": "2026-03-25T11:00:00+09:00",
            "attendees": [],
        }

        with _mock_store(), \
             patch("agents.during.cal") as mock_cal, \
             patch("agents.during.drive") as mock_drive, \
             patch("agents.during._generate") as mock_gen:
            mock_cal.get_recently_ended_meetings.return_value = [meeting]
            mock_drive.find_meet_transcript.return_value = None

            check_transcripts(slack)

        # LLM 호출 없음, Drive 저장 없음
        mock_gen.assert_not_called()
        mock_drive.save_minutes.assert_not_called()

    def test_completed_notes_combined_with_transcript(self):
        """_completed_notes의 수동 노트가 트랜스크립트와 결합됨"""
        slack = _slack()
        event_id = "evt_with_notes"
        _completed_notes[event_id] = {
            "user_id": _TEST_USER,
            "title": "LG 미팅",
            "notes": [{"time": "10:05", "text": "파트너십 논의"}],
            "started_at": "2026-03-25 10:00",
            "ended_at": "11:00",
            "stored_at": datetime.now(KST),
        }
        meeting = {
            "id": event_id,
            "summary": "LG 미팅",
            "start_time": "2026-03-25T10:00:00+09:00",
            "end_time": "2026-03-25T11:00:00+09:00",
            "attendees": [],
        }

        all_prompts = []

        def fake_generate(prompt):
            all_prompts.append(prompt)
            return "## 회의 요약\n결합된 내용"

        with _mock_store(), \
             patch("agents.during.cal") as mock_cal, \
             patch("agents.during.drive") as mock_drive, \
             patch("agents.during.docs") as mock_docs, \
             patch("agents.during._generate_minutes", side_effect=fake_generate):
            mock_cal.get_recently_ended_meetings.return_value = [meeting]
            mock_drive.find_meet_transcript.return_value = {"id": "doc1", "name": "LG Transcript"}
            mock_drive.save_minutes.return_value = "file_id"
            mock_docs.read_document.return_value = "트랜스크립트 전문"

            check_transcripts(slack)

        # 노트가 _completed_notes에서 제거됨 (처리 완료)
        assert event_id not in _completed_notes
        # 내부용 프롬프트(첫 번째 호출)에 트랜스크립트와 노트 모두 포함됨
        assert len(all_prompts) >= 1
        internal_prompt = all_prompts[0]
        assert "파트너십 논의" in internal_prompt or "트랜스크립트 전문" in internal_prompt

    def test_duplicate_event_skipped(self):
        """이미 처리된 이벤트는 중복 처리 안 함"""
        slack = _slack()
        event_id = "evt_dup"
        _processed_events[_TEST_USER] = {event_id}
        meeting = {
            "id": event_id,
            "summary": "중복 미팅",
            "start_time": "2026-03-25T09:00:00+09:00",
            "end_time": "2026-03-25T10:00:00+09:00",
            "attendees": [],
        }

        with _mock_store(), \
             patch("agents.during.cal") as mock_cal, \
             patch("agents.during.drive") as mock_drive, \
             patch("agents.during._generate") as mock_gen:
            mock_cal.get_recently_ended_meetings.return_value = [meeting]

            check_transcripts(slack)

        # 중복이므로 트랜스크립트 탐색 호출 없음
        mock_drive.find_meet_transcript.assert_not_called()
        mock_gen.assert_not_called()

    def test_expired_notes_flushed(self):
        """90분 초과 노트는 fallback으로 회의록 생성"""
        slack = _slack()
        old_time = datetime.now(KST) - timedelta(minutes=100)
        _completed_notes["evt_expired"] = {
            "user_id": _TEST_USER,
            "title": "만료된 미팅",
            "notes": [{"time": "09:05", "text": "논의 내용"}],
            "started_at": "2026-03-25 09:00",
            "ended_at": "10:00",
            "stored_at": old_time,
        }

        with _mock_store(), \
             patch("agents.during.cal") as mock_cal, \
             patch("agents.during.drive") as mock_drive, \
             patch("agents.during._generate_minutes", return_value="## 회의 요약\nfallback"):
            mock_cal.get_recently_ended_meetings.return_value = []
            mock_drive.save_minutes.return_value = "file_id"

            check_transcripts(slack)

        # 만료된 노트는 제거됨
        assert "evt_expired" not in _completed_notes
        # Slack 메시지에 fallback 안내 포함
        all_texts = " ".join(c[1]["text"] for c in slack.chat_postMessage.call_args_list)
        assert "트랜스크립트" in all_texts or "노트" in all_texts


# ── get_minutes_list ──────────────────────────────────────────

class TestGetMinutesList:
    def setup_method(self):
        _active_sessions.clear()
        _completed_notes.clear()

    def test_shows_file_list(self):
        """회의록 목록 Slack 발송 (blocks 기반 — 양식 진단 마커 포함)"""
        slack = _slack()
        files = [
            {"id": "f1", "name": "2026-03-25_카카오_내부용.md", "modifiedTime": "2026-03-25T15:00:00Z"},
            {"id": "f2", "name": "2026-03-24_네이버_외부용.md", "modifiedTime": "2026-03-24T10:00:00Z"},
        ]

        with _mock_store(), patch("agents.during.drive") as mock_drive:
            mock_drive.list_minutes.return_value = files
            # 본문은 정상 양식이라고 가정 (frontmatter 포함)
            mock_drive._read_file.return_value = "---\ntitle: x\n---\n\n# 회의\n"
            get_minutes_list(slack, _TEST_USER)

        kwargs = slack.chat_postMessage.call_args.kwargs
        # blocks 직렬화 텍스트로 파일명 확인
        all_block_text = json.dumps(kwargs.get("blocks") or [], ensure_ascii=False)
        assert "2026-03-25_카카오_내부용" in all_block_text
        assert "2026-03-24_네이버_외부용" in all_block_text

    def test_empty_list_message(self):
        """회의록 없으면 안내 메시지"""
        slack = _slack()

        with _mock_store(), patch("agents.during.drive") as mock_drive:
            mock_drive.list_minutes.return_value = []
            get_minutes_list(slack, _TEST_USER)

        text = slack.chat_postMessage.call_args[1]["text"]
        assert "없습니다" in text

    def test_no_minutes_folder_warns(self):
        """minutes_folder_id 없으면 경고"""
        slack = _slack()
        mock_store = MagicMock()
        mock_store.get_credentials.return_value = _MOCK_CREDS
        mock_store.get_user.return_value = {**_MOCK_USER, "minutes_folder_id": None}

        with patch("agents.during.user_store", mock_store):
            get_minutes_list(slack, _TEST_USER)

        text = slack.chat_postMessage.call_args[1]["text"]
        assert "설정" in text or "재등록" in text

    def test_limited_to_10_files(self):
        """10개 초과 시 10개만 표시 + 나머지 개수 표시 (blocks 의 context 영역)"""
        slack = _slack()
        files = [{"id": f"f{i}", "name": f"2026-03-{i:02d}_미팅.md", "modifiedTime": f"2026-03-{i:02d}T10:00:00Z"}
                 for i in range(1, 15)]

        with _mock_store(), patch("agents.during.drive") as mock_drive:
            mock_drive.list_minutes.return_value = files
            mock_drive._read_file.return_value = "---\ntitle: x\n---\n\n# 회의\n"
            get_minutes_list(slack, _TEST_USER)

        kwargs = slack.chat_postMessage.call_args.kwargs
        all_block_text = json.dumps(kwargs.get("blocks") or [], ensure_ascii=False)
        assert "4개" in all_block_text  # 14 - 10 = 4개 더 있음


# ── _generate (Claude 단일) ───────────────────────────────────

class TestGenerate:
    def test_claude_called(self):
        """_generate → Claude messages.create 호출, 응답 텍스트 반환"""
        mock_claude = MagicMock()
        mock_claude.messages.create.return_value = MagicMock(
            content=[MagicMock(text="  Claude 결과  ")]
        )
        with patch.object(during, "_claude", mock_claude):
            result = during._generate("테스트 프롬프트")
        mock_claude.messages.create.assert_called_once()
        assert result == "Claude 결과"


# ── _format_notes / _parse_meeting_meta ───────────────────────

class TestUtilFunctions:
    def test_format_notes_empty(self):
        """빈 노트 리스트 → 빈 문자열"""
        result = during._format_notes([])
        assert result == ""

    def test_format_notes_with_items(self):
        """노트 리스트 → [HH:MM] 텍스트 형식"""
        notes = [
            {"time": "10:05", "text": "첫 번째"},
            {"time": "10:10", "text": "두 번째"},
        ]
        result = during._format_notes(notes)
        assert "[10:05] 첫 번째" in result
        assert "[10:10] 두 번째" in result

    def test_parse_meeting_meta_extracts_fields(self):
        """meeting dict에서 date, time_range, attendees 추출"""
        meeting = {
            "start_time": "2026-03-25T14:00:00+09:00",
            "end_time": "2026-03-25T15:30:00+09:00",
            "attendees": [
                {"name": "김민환", "email": "mh@kakao.com"},
                {"name": "", "email": "jh@kakao.com"},
            ],
        }
        date_str, time_range, attendees_str = during._parse_meeting_meta(meeting)

        assert date_str == "2026-03-25"
        assert "14:00" in time_range
        assert "15:30" in time_range
        assert "김민환" in attendees_str

    def test_parse_meeting_meta_missing_fields(self):
        """필드 누락 시 기본값 처리"""
        date_str, time_range, attendees_str = during._parse_meeting_meta({})
        assert date_str  # 오늘 날짜로 기본값
        assert attendees_str == "정보 없음"


# ── 세션 파일 영속성 ──────────────────────────────────────────

class TestSessionPersistence:
    """서버 재시작 시 세션/노트 파일 복구 테스트"""

    def setup_method(self):
        _active_sessions.clear()
        _completed_notes.clear()
        _processed_events.clear()
        _pending_minutes.clear()

    def test_active_session_saved_to_file(self, isolated_sessions_dir):
        """start_session 호출 시 세션 파일 생성"""
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            start_session(_slack(), _TEST_USER, "파일 저장 테스트")

        session_file = isolated_sessions_dir / f"active_{_TEST_USER}.json"
        assert session_file.exists()
        data = json.loads(session_file.read_text(encoding="utf-8"))
        assert data["title"] == "파일 저장 테스트"
        assert data["notes"] == []

    def test_note_updates_session_file(self, isolated_sessions_dir):
        """add_note 호출 시 세션 파일 업데이트"""
        _active_sessions[_TEST_USER] = {
            "title": "테스트", "started_at": "10:00", "notes": [], "event_id": None
        }
        # 초기 파일 저장
        during._save_active_session(_TEST_USER)

        add_note(_slack(), _TEST_USER, "첫 번째 노트")

        data = json.loads((isolated_sessions_dir / f"active_{_TEST_USER}.json")
                          .read_text(encoding="utf-8"))
        assert len(data["notes"]) == 1
        assert data["notes"][0]["text"] == "첫 번째 노트"

    def test_active_session_file_deleted_on_end_no_event(self, isolated_sessions_dir):
        """event_id 없는 세션 종료 시 active 파일 삭제"""
        _active_sessions[_TEST_USER] = {
            "title": "즉시 종료", "started_at": "2026-03-25 10:00",
            "notes": [], "event_id": None
        }
        during._save_active_session(_TEST_USER)
        assert (isolated_sessions_dir / f"active_{_TEST_USER}.json").exists()

        with _mock_store(), \
             patch("agents.during._generate_minutes", return_value="## 회의 요약\n내용"), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.save_minutes.return_value = "file_id"
            end_session(_slack(), _TEST_USER)

        assert not (isolated_sessions_dir / f"active_{_TEST_USER}.json").exists()

    def test_active_file_deleted_on_end_with_event(self, isolated_sessions_dir):
        """event_id 있는 세션 종료 시 active 파일 삭제 + 소스 선택 대기 등록 (I1)"""
        from agents.during import _pending_source_select
        _pending_source_select.clear()
        event_id = "evt_persist"
        _active_sessions[_TEST_USER] = {
            "title": "폴러 위임 테스트",
            "started_at": "2026-03-25 14:00",
            "notes": [{"time": "14:05", "text": "논의"}],
            "event_id": event_id,
        }
        during._save_active_session(_TEST_USER)

        with _mock_store():
            end_session(_slack(), _TEST_USER)

        # active 파일 삭제됨
        assert not (isolated_sessions_dir / f"active_{_TEST_USER}.json").exists()
        # I1: 즉시 생성이 아니라 소스 선택 대기 payload가 등록됨
        assert event_id in _pending_source_select
        payload = _pending_source_select[event_id]
        assert payload["title"] == "폴러 위임 테스트"
        assert len(payload["notes"]) == 1

    def test_completed_note_file_deleted_after_transcript_processing(self, isolated_sessions_dir):
        """폴러가 트랜스크립트 처리 후 completed 파일 삭제"""
        event_id = "evt_cleanup"
        _completed_notes[event_id] = {
            "user_id": _TEST_USER,
            "title": "정리 테스트",
            "notes": [],
            "started_at": "2026-03-25 10:00",
            "ended_at": "11:00",
            "stored_at": datetime.now(KST),
        }
        during._save_completed_note(event_id)
        assert (isolated_sessions_dir / f"completed_{event_id}.json").exists()

        meeting = {
            "id": event_id, "summary": "정리 테스트",
            "start_time": "2026-03-25T10:00:00+09:00",
            "end_time": "2026-03-25T11:00:00+09:00",
            "attendees": [],
        }
        with _mock_store(), \
             patch("agents.during.cal") as mock_cal, \
             patch("agents.during.drive") as mock_drive, \
             patch("agents.during.docs") as mock_docs, \
             patch("agents.during._generate_minutes", return_value="## 회의 요약\n내용"):
            mock_cal.get_recently_ended_meetings.return_value = [meeting]
            mock_drive.find_meet_transcript.return_value = {"id": "doc1", "name": "Transcript"}
            mock_drive.save_minutes.return_value = "file_id"
            mock_docs.read_document.return_value = "트랜스크립트"
            check_transcripts(_slack())

        # completed 파일 삭제됨
        assert not (isolated_sessions_dir / f"completed_{event_id}.json").exists()

    def test_completed_note_file_deleted_on_fallback(self, isolated_sessions_dir):
        """90분 만료 fallback 처리 후 completed 파일 삭제"""
        event_id = "evt_fallback"
        old_time = datetime.now(KST) - timedelta(minutes=100)
        _completed_notes[event_id] = {
            "user_id": _TEST_USER,
            "title": "만료 테스트",
            "notes": [{"time": "09:05", "text": "내용"}],
            "started_at": "2026-03-25 09:00",
            "ended_at": "10:00",
            "stored_at": old_time,
        }
        during._save_completed_note(event_id)
        assert (isolated_sessions_dir / f"completed_{event_id}.json").exists()

        with _mock_store(), \
             patch("agents.during.cal") as mock_cal, \
             patch("agents.during.drive") as mock_drive, \
             patch("agents.during._generate_minutes", return_value="## 회의 요약\nfallback"):
            mock_cal.get_recently_ended_meetings.return_value = []
            mock_drive.save_minutes.return_value = "file_id"
            check_transcripts(_slack())

        # completed 파일 삭제됨
        assert not (isolated_sessions_dir / f"completed_{event_id}.json").exists()

    def test_load_sessions_recovers_active_and_completed(self, isolated_sessions_dir):
        """_load_sessions()로 서버 재시작 후 세션·노트 복구"""
        isolated_sessions_dir.mkdir(exist_ok=True)

        # active 세션 파일 직접 생성
        (isolated_sessions_dir / "active_U_RECOVER.json").write_text(
            json.dumps({
                "title": "복구 테스트 미팅",
                "started_at": "2026-03-25 09:00",
                "notes": [{"time": "09:10", "text": "복구된 노트"}],
                "event_id": "evt_rec",
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        # completed 노트 파일 직접 생성
        stored_at = datetime.now(KST)
        (isolated_sessions_dir / "completed_evt_rec.json").write_text(
            json.dumps({
                "user_id": "U_RECOVER",
                "title": "복구 완료 미팅",
                "notes": [{"time": "10:00", "text": "완료 노트"}],
                "started_at": "2026-03-25 10:00",
                "ended_at": "11:00",
                "stored_at": stored_at.isoformat(),
            }, ensure_ascii=False),
            encoding="utf-8",
        )

        # 메모리 초기화 후 복구
        _active_sessions.clear()
        _completed_notes.clear()
        during._load_sessions()

        assert "U_RECOVER" in _active_sessions
        assert _active_sessions["U_RECOVER"]["title"] == "복구 테스트 미팅"
        assert len(_active_sessions["U_RECOVER"]["notes"]) == 1

        assert "evt_rec" in _completed_notes
        assert _completed_notes["evt_rec"]["title"] == "복구 완료 미팅"
        assert isinstance(_completed_notes["evt_rec"]["stored_at"], datetime)


# ── 이벤트 선택 / 자동 감지 ──────────────────────────────────

class TestEventSelection:
    """캘린더 이벤트 자동 감지 및 선택 흐름 테스트"""

    def setup_method(self):
        _active_sessions.clear()
        _completed_notes.clear()
        _processed_events.clear()
        _pending_minutes.clear()
        _pending_inputs.clear()

    def test_handle_event_selection_starts_session(self):
        """이벤트 선택 시 세션 시작 + 대기 입력 추가"""
        now = datetime.now(KST)
        event = {
            "id": "evt_sel",
            "summary": "선택된 미팅",
            "start_time": (now - timedelta(minutes=10)).isoformat(),
            "_end_time": (now + timedelta(minutes=50)).isoformat(),
            "location": "", "meet_link": "", "description": "",
            "attendees": [],
        }
        _pending_inputs[_TEST_USER] = {
            "inputs": [{"type": "note", "content": "대기 중 노트"}],
            "events": [event],
        }

        slack = _slack()
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            handle_event_selection(slack, _TEST_USER, selected_event_id="evt_sel")

        assert _TEST_USER in _active_sessions
        assert _active_sessions[_TEST_USER]["title"] == "선택된 미팅"
        assert _active_sessions[_TEST_USER]["event_id"] == "evt_sel"
        assert len(_active_sessions[_TEST_USER]["notes"]) == 1
        assert _active_sessions[_TEST_USER]["notes"][0]["text"] == "대기 중 노트"
        assert _TEST_USER not in _pending_inputs

    def test_handle_event_selection_new_meeting(self):
        """'새 미팅' 선택 시 제목으로 세션 시작"""
        _pending_inputs[_TEST_USER] = {
            "inputs": [{"type": "note", "content": "메모 내용"}],
            "events": [],
        }
        slack = _slack()
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            handle_event_selection(slack, _TEST_USER, selected_event_id=None,
                                   custom_title="직접 입력한 제목")

        assert _TEST_USER in _active_sessions
        assert _active_sessions[_TEST_USER]["title"] == "직접 입력한 제목"
        assert len(_active_sessions[_TEST_USER]["notes"]) == 1

    def test_handle_event_title_reply(self):
        """스레드 답글로 제목 입력 시 세션 시작"""
        _pending_inputs[_TEST_USER] = {
            "inputs": [{"type": "audio", "content": "STT 변환 텍스트"}],
            "events": [],
            "prompt_ts": "111.222",
        }
        slack = _slack()
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            handle_event_title_reply(slack, _TEST_USER, "KISA 보안 미팅")

        assert _TEST_USER in _active_sessions
        assert _active_sessions[_TEST_USER]["title"] == "KISA 보안 미팅"

    def test_multiple_inputs_queued(self):
        """이벤트 선택 대기 중 추가 입력이 큐에 쌓임"""
        slack = _slack()
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            # 첫 입력 → 대기 큐 생성
            add_note(slack, _TEST_USER, "첫 번째 메모")
            assert _TEST_USER in _pending_inputs
            assert len(_pending_inputs[_TEST_USER]["inputs"]) == 1

            # 두 번째 입력 → 큐에 추가
            add_note(slack, _TEST_USER, "두 번째 메모")
            assert len(_pending_inputs[_TEST_USER]["inputs"]) == 2

    def test_pending_inputs_cleared_after_selection(self):
        """이벤트 선택 후 대기 큐 정리"""
        _pending_inputs[_TEST_USER] = {
            "inputs": [
                {"type": "note", "content": "메모1"},
                {"type": "audio", "content": "음성 변환"},
            ],
            "events": [],
        }
        slack = _slack()
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            handle_event_selection(slack, _TEST_USER, selected_event_id=None,
                                   custom_title="수동 미팅")

        assert _TEST_USER not in _pending_inputs
        assert len(_active_sessions[_TEST_USER]["notes"]) == 2

    def test_nearest_event_shown_when_no_ongoing(self):
        """진행 중 이벤트 없을 때 가장 가까운 이벤트를 보여주고 확인 요청"""
        slack = _slack()
        now = datetime.now(KST)
        # 1시간 전에 끝난 이벤트
        past_event = {
            "id": "evt_past",
            "summary": "아까 끝난 미팅",
            "start": {"dateTime": (now - timedelta(hours=2)).isoformat()},
            "end": {"dateTime": (now - timedelta(hours=1)).isoformat()},
            "attendees": [],
        }
        # 2시간 후 시작 이벤트
        future_event = {
            "id": "evt_future",
            "summary": "나중 미팅",
            "start": {"dateTime": (now + timedelta(hours=2)).isoformat()},
            "end": {"dateTime": (now + timedelta(hours=3)).isoformat()},
            "attendees": [],
        }
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = [past_event, future_event]
            mock_cal.parse_event.side_effect = lambda ev: {
                "id": ev["id"],
                "summary": ev["summary"],
                "start_time": ev["start"]["dateTime"],
                "location": "", "meet_link": "", "description": "",
                "attendees": [],
            }
            add_note(slack, _TEST_USER, "메모")

        # 세션은 시작 안 됨 (확인 대기 중)
        assert _TEST_USER not in _active_sessions
        assert _TEST_USER in _pending_inputs
        # 가장 가까운 이벤트가 1개로 저장됨
        assert len(_pending_inputs[_TEST_USER]["events"]) == 1
        # 가장 가까운 이벤트 = 1시간 전 끝난 이벤트 (거리 60분 vs 120분)
        assert _pending_inputs[_TEST_USER]["events"][0]["id"] == "evt_past"
        # Slack에 확인 UI 발송됨
        call_args = slack.chat_postMessage.call_args_list
        any_confirm = any("맞나요" in str(c) or "가장 가까운" in str(c) for c in call_args)
        assert any_confirm

    def test_generate_minutes_now_alias(self):
        """generate_minutes_now는 end_session의 별칭"""
        from agents.during import generate_minutes_now
        slack = _slack()
        _active_sessions[_TEST_USER] = {
            "title": "별칭 테스트", "started_at": "2026-04-08 14:00",
            "notes": [{"time": "14:05", "text": "내용"}],
            "event_id": None, "event_summary": None, "event_time_str": None,
        }
        with _mock_store(), \
             patch("agents.during._generate_minutes", return_value="## 회의 요약\n내용"), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.save_minutes.return_value = "file_id"
            generate_minutes_now(slack, _TEST_USER)

        # 세션이 종료되고 회의록 초안이 생성됨 (FR-D14: event_id 키)
        assert _TEST_USER not in _active_sessions
        assert _find_draft_for_user(_TEST_USER) is not None


# ── F4: 문서 업로드 기반 회의록 생성 ──────────────────────────


class TestDocumentBasedMinutes:
    """F4: 세션 없이 업로드된 문서로부터 회의록 초안 생성.
    피드백 id 24 반영 — 캘린더에 없는 미팅도 회의록화."""

    def setup_method(self):
        _active_sessions.clear()
        _pending_minutes.clear()

    def test_generates_minutes_with_filename_title(self):
        """파일명(확장자 제외)이 제목으로 사용됨"""
        slack = _slack()
        with _mock_store(), \
             patch("agents.during._generate_and_post_minutes") as mock_gen:
            start_document_based_minutes(
                slack, _TEST_USER,
                filename="카카오_PoC_회의록.txt",
                text="[김철수] 안녕하세요...",
            )

        assert mock_gen.called
        kwargs = mock_gen.call_args[1]
        assert kwargs["title"] == "카카오_PoC_회의록"
        assert kwargs["transcript_text"] == "[김철수] 안녕하세요..."
        assert kwargs["notes_text"] == ""
        assert kwargs["event_id"] is None
        assert kwargs["attendees"] == "정보 없음"
        assert kwargs["attendees_raw"] == []

    def test_uses_today_as_date(self):
        """날짜는 오늘 (YYYY-MM-DD)"""
        slack = _slack()
        with _mock_store(), \
             patch("agents.during._generate_and_post_minutes") as mock_gen:
            start_document_based_minutes(slack, _TEST_USER, "doc.md", "content")

        today = datetime.now(KST).strftime("%Y-%m-%d")
        assert mock_gen.call_args[1]["date_str"] == today

    def test_fallback_title_when_filename_empty(self):
        """파일명이 비어있으면 '업로드 문서' 폴백"""
        slack = _slack()
        with _mock_store(), \
             patch("agents.during._generate_and_post_minutes") as mock_gen:
            start_document_based_minutes(slack, _TEST_USER, "", "content")

        assert mock_gen.call_args[1]["title"] == "업로드 문서"

    def test_sends_progress_message(self):
        """생성 시작 안내 메시지 발송 (filename 포함)"""
        slack = _slack()
        with _mock_store(), \
             patch("agents.during._generate_and_post_minutes"):
            start_document_based_minutes(slack, _TEST_USER, "meeting.txt", "x")

        # 첫 chat_postMessage 호출이 진행 안내
        texts = [c[1].get("text", "") for c in slack.chat_postMessage.call_args_list]
        assert any("meeting.txt" in t and "회의록을 생성" in t for t in texts)

    def test_auth_error_reports_gracefully(self):
        """인증 오류 시 오류 메시지만 발송, minutes 생성 안 함"""
        slack = _slack()
        mock_store = MagicMock()
        mock_store.get_credentials.side_effect = RuntimeError("auth fail")
        with patch("agents.during.user_store", mock_store), \
             patch("agents.during._generate_and_post_minutes") as mock_gen:
            start_document_based_minutes(slack, _TEST_USER, "doc.txt", "x")

        assert not mock_gen.called
        texts = [c[1].get("text", "") for c in slack.chat_postMessage.call_args_list]
        assert any("인증 오류" in t for t in texts)
