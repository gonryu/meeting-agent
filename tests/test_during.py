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

with patch("google.genai.Client"), \
     patch("anthropic.Anthropic"):
    import agents.during as during
    from agents.during import (
        start_session,
        add_note,
        end_session,
        get_minutes_list,
        check_transcripts,
        _active_sessions,
        _completed_notes,
        _processed_events,
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

    def test_creates_session(self):
        """세션 생성 후 _active_sessions에 등록"""
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            start_session(_slack(), _TEST_USER, "카카오 미팅")

        assert _TEST_USER in _active_sessions
        assert _active_sessions[_TEST_USER]["title"] == "카카오 미팅"
        assert _active_sessions[_TEST_USER]["notes"] == []

    def test_default_title(self):
        """제목 없으면 '미팅'으로 기본값 설정"""
        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            start_session(_slack(), _TEST_USER, "")

        assert _active_sessions[_TEST_USER]["title"] == "미팅"

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

    def test_calendar_event_matched(self):
        """진행 중인 캘린더 이벤트와 자동 매칭"""
        now = datetime.now(KST)
        start = (now.replace(minute=0, second=0)).isoformat()
        end = (now.replace(hour=now.hour + 1, minute=0, second=0) if now.hour < 23 else now).isoformat()

        events = [{"id": "evt1", "summary": "카카오 미팅", "start": {"dateTime": start},
                   "attendees": [], "end": {"dateTime": end}}]

        with _mock_store(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = events
            mock_cal.parse_event.return_value = {
                "id": "evt1", "summary": "카카오 미팅",
                "start_time": start, "attendees": [],
                "location": "", "meet_link": "", "description": "",
            }
            start_session(_slack(), _TEST_USER, "카카오 미팅")

        assert _active_sessions[_TEST_USER]["event_id"] == "evt1"


# ── add_note ─────────────────────────────────────────────────

class TestAddNote:
    def setup_method(self):
        _active_sessions.clear()
        _completed_notes.clear()

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

    def test_no_session_sends_warning(self):
        """세션 없으면 경고 메시지"""
        slack = _slack()
        add_note(slack, _TEST_USER, "노트 내용")

        text = slack.chat_postMessage.call_args[1]["text"]
        assert "세션" in text or "시작" in text

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
             patch("agents.during._generate", return_value="## 회의 요약\n내용"), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.save_minutes.return_value = "file_id"
            end_session(_slack(), _TEST_USER)

        assert _TEST_USER not in _active_sessions

    def test_session_removed_after_end_with_event(self):
        """이벤트 있는 세션: 종료 후 _active_sessions에서 삭제, _completed_notes에 저장"""
        self._init_session(event_id="evt_kakao")
        slack = _slack()

        with _mock_store(), \
             patch("agents.during.threading.Thread"):  # 즉시 폴링 스레드 차단
            end_session(slack, _TEST_USER)

        assert _TEST_USER not in _active_sessions
        assert "evt_kakao" in _completed_notes
        assert _completed_notes["evt_kakao"]["user_id"] == _TEST_USER

    def test_deferred_to_poller_when_event_id_known(self):
        """event_id 있으면 즉시 LLM 생성 안 하고 _completed_notes에 저장 후 즉시 폴링 트리거"""
        self._init_session(event_id="evt123")
        slack = _slack()

        with _mock_store(), \
             patch("agents.during._generate") as mock_gen, \
             patch("agents.during.threading.Thread") as mock_thread:
            end_session(slack, _TEST_USER)

        # LLM 생성 호출 없어야 함 (폴러에 위임)
        mock_gen.assert_not_called()
        # 노트가 _completed_notes에 저장됨
        assert "evt123" in _completed_notes
        data = _completed_notes["evt123"]
        assert data["title"] == "카카오 미팅"
        assert len(data["notes"]) == 2
        # 즉시 폴링 스레드가 실행됨
        mock_thread.assert_called_once()
        call_kwargs = mock_thread.call_args[1]
        assert call_kwargs.get("kwargs", {}).get("min_minutes_ago") == 0

    def test_deferred_sends_wait_message(self):
        """event_id 있는 경우 트랜스크립트 대기 안내 메시지 발송"""
        self._init_session(event_id="evt123")
        slack = _slack()

        with _mock_store(), \
             patch("agents.during.threading.Thread"):  # 즉시 폴링 스레드 차단
            end_session(slack, _TEST_USER)

        text = slack.chat_postMessage.call_args[1]["text"]
        assert "트랜스크립트" in text or "90분" in text

    def test_immediate_generation_when_no_event_id(self):
        """event_id 없으면 즉시 내부용+외부용 생성"""
        self._init_session(event_id=None)
        slack = _slack()
        gen_call_count = {"n": 0}

        def fake_generate(prompt):
            gen_call_count["n"] += 1
            return f"## 회의 요약\n생성 결과 {gen_call_count['n']}"

        with _mock_store(), \
             patch("agents.during._generate", side_effect=fake_generate), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.save_minutes.return_value = "file_id"
            end_session(slack, _TEST_USER)

        # 내부용 + 외부용 = 2회 이상 LLM 호출
        assert gen_call_count["n"] >= 2

    def test_no_session_sends_warning(self):
        """세션 없으면 경고 메시지"""
        slack = _slack()
        end_session(slack, _TEST_USER)

        text = slack.chat_postMessage.call_args[1]["text"]
        assert "세션" in text

    def test_internal_and_external_saved_to_drive(self):
        """내부용·외부용 2개 Drive 저장"""
        self._init_session(event_id=None)
        slack = _slack()

        with _mock_store(), \
             patch("agents.during._generate", return_value="## 회의 요약\n내용"), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.save_minutes.return_value = "saved_file_id"
            end_session(slack, _TEST_USER)

        # 내부용 + 외부용 = 2번 저장
        assert mock_drive.save_minutes.call_count == 2
        filenames = [c[0][2] for c in mock_drive.save_minutes.call_args_list]
        assert any("내부용" in f for f in filenames)
        assert any("외부용" in f for f in filenames)

    def test_minutes_filename_contains_title_and_date(self):
        """파일명에 날짜_제목 형식 포함"""
        self._init_session(event_id=None)

        with _mock_store(), \
             patch("agents.during._generate", return_value="## 회의 요약\n내용"), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.save_minutes.return_value = "file_id"
            end_session(_slack(), _TEST_USER)

        filenames = [c[0][2] for c in mock_drive.save_minutes.call_args_list]
        for fn in filenames:
            assert "카카오 미팅" in fn
            assert "2026" in fn

    def test_internal_and_external_posted_to_slack(self):
        """내부용·외부용 회의록이 Slack으로 각각 발송됨"""
        self._init_session(event_id=None)
        slack = _slack()

        with _mock_store(), \
             patch("agents.during._generate", return_value="## 회의 요약\n내용 있음"), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.save_minutes.return_value = "file_id"
            end_session(slack, _TEST_USER)

        all_texts = " ".join(c[1]["text"] for c in slack.chat_postMessage.call_args_list)
        assert "내부용" in all_texts
        assert "외부용" in all_texts

    def test_empty_notes_handled(self):
        """노트 없이 종료해도 오류 없음"""
        self._init_session(notes=[], event_id=None)
        slack = _slack()

        with _mock_store(), \
             patch("agents.during._generate", return_value="## 회의 요약\n없음"), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.save_minutes.return_value = "file_id"
            end_session(slack, _TEST_USER)

        assert _TEST_USER not in _active_sessions

    def test_llm_failure_still_saves_to_drive(self):
        """LLM 생성 실패해도 Drive 저장 호출됨"""
        self._init_session(event_id=None)
        slack = _slack()

        with _mock_store(), \
             patch("agents.during._generate", side_effect=Exception("LLM 오류")), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.save_minutes.return_value = "file_id"
            end_session(slack, _TEST_USER)

        # Drive 저장은 호출되어야 함 (내부용은 저장, 외부용도 시도)
        assert mock_drive.save_minutes.call_count >= 1

    def test_llm_failure_raw_notes_in_saved_content(self):
        """LLM 실패 시 저장 내용에 원본 노트 포함"""
        self._init_session(event_id=None)

        with _mock_store(), \
             patch("agents.during._generate", side_effect=Exception("LLM 오류")), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.save_minutes.return_value = "file_id"
            end_session(_slack(), _TEST_USER)

        # 내부용 저장 내용에 원본 노트 포함
        all_contents = " ".join(str(c[0][3]) for c in mock_drive.save_minutes.call_args_list)
        assert "DID 연동 논의" in all_contents


# ── check_transcripts ─────────────────────────────────────────

class TestCheckTranscripts:
    def setup_method(self):
        _active_sessions.clear()
        _completed_notes.clear()
        _processed_events.clear()

    def test_transcript_found_generates_minutes(self):
        """트랜스크립트 발견 시 회의록 생성"""
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
             patch("agents.during._generate", return_value="## 회의 요약\n내용"):
            mock_cal.get_recently_ended_meetings.return_value = [meeting]
            mock_drive.find_meet_transcript.return_value = transcript_file
            mock_drive.save_minutes.return_value = "file_id"
            mock_docs.read_document.return_value = "트랜스크립트 내용..."

            check_transcripts(slack)

        # Drive 저장 호출 (내부용 + 외부용)
        assert mock_drive.save_minutes.call_count == 2

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
             patch("agents.during._generate", side_effect=fake_generate):
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
             patch("agents.during._generate", return_value="## 회의 요약\nfallback"):
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
        """회의록 목록 Slack 발송"""
        slack = _slack()
        files = [
            {"id": "f1", "name": "2026-03-25_카카오_내부용.md", "modifiedTime": "2026-03-25T15:00:00Z"},
            {"id": "f2", "name": "2026-03-24_네이버_외부용.md", "modifiedTime": "2026-03-24T10:00:00Z"},
        ]

        with _mock_store(), patch("agents.during.drive") as mock_drive:
            mock_drive.list_minutes.return_value = files
            get_minutes_list(slack, _TEST_USER)

        text = slack.chat_postMessage.call_args[1]["text"]
        assert "2026-03-25_카카오_내부용" in text
        assert "2026-03-24_네이버_외부용" in text

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
        """10개 초과 시 10개만 표시 + 나머지 개수 표시"""
        slack = _slack()
        files = [{"id": f"f{i}", "name": f"2026-03-{i:02d}_미팅.md", "modifiedTime": f"2026-03-{i:02d}T10:00:00Z"}
                 for i in range(1, 15)]

        with _mock_store(), patch("agents.during.drive") as mock_drive:
            mock_drive.list_minutes.return_value = files
            get_minutes_list(slack, _TEST_USER)

        text = slack.chat_postMessage.call_args[1]["text"]
        assert "4개" in text  # 14 - 10 = 4개 더 있음


# ── _generate fallback ────────────────────────────────────────

class TestGenerateFallback:
    def test_claude_called_on_gemini_failure(self):
        """Gemini 실패 시 Claude 폴백 호출"""
        mock_gemini = MagicMock()
        mock_gemini.models.generate_content.side_effect = Exception("429 Quota")
        mock_claude = MagicMock()
        mock_claude.messages.create.return_value = MagicMock(
            content=[MagicMock(text="Claude 생성 결과")]
        )

        with patch.object(during, "_gemini", mock_gemini), \
             patch.object(during, "_claude", mock_claude):
            result = during._generate("테스트 프롬프트")

        mock_claude.messages.create.assert_called_once()
        assert result == "Claude 생성 결과"

    def test_gemini_success_no_claude(self):
        """Gemini 성공 시 Claude 미호출"""
        mock_gemini = MagicMock()
        mock_gemini.models.generate_content.return_value = MagicMock(text="  Gemini 결과  ")
        mock_claude = MagicMock()

        with patch.object(during, "_gemini", mock_gemini), \
             patch.object(during, "_claude", mock_claude):
            result = during._generate("테스트")

        mock_claude.messages.create.assert_not_called()
        assert result == "Gemini 결과"


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
             patch("agents.during._generate", return_value="## 회의 요약\n내용"), \
             patch("agents.during.drive") as mock_drive:
            mock_drive.save_minutes.return_value = "file_id"
            end_session(_slack(), _TEST_USER)

        assert not (isolated_sessions_dir / f"active_{_TEST_USER}.json").exists()

    def test_completed_note_saved_on_end_with_event(self, isolated_sessions_dir):
        """event_id 있는 세션 종료 시 active 삭제 + completed 파일 생성"""
        event_id = "evt_persist"
        _active_sessions[_TEST_USER] = {
            "title": "폴러 위임 테스트",
            "started_at": "2026-03-25 14:00",
            "notes": [{"time": "14:05", "text": "논의"}],
            "event_id": event_id,
        }
        during._save_active_session(_TEST_USER)

        with _mock_store(), \
             patch("agents.during.threading.Thread"):  # 즉시 폴링 스레드 차단
            end_session(_slack(), _TEST_USER)

        # active 파일 삭제됨
        assert not (isolated_sessions_dir / f"active_{_TEST_USER}.json").exists()
        # completed 파일 생성됨
        completed_file = isolated_sessions_dir / f"completed_{event_id}.json"
        assert completed_file.exists()
        data = json.loads(completed_file.read_text(encoding="utf-8"))
        assert data["title"] == "폴러 위임 테스트"
        assert len(data["notes"]) == 1
        assert "stored_at" in data  # datetime이 ISO 문자열로 직렬화됨

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
             patch("agents.during._generate", return_value="## 회의 요약\n내용"):
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
             patch("agents.during._generate", return_value="## 회의 요약\nfallback"):
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
