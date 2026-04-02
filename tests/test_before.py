"""agents/before.py 단위 테스트"""
import os
# import 전에 환경변수 설정 (genai.Client 초기화에 필요)
os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
os.environ.setdefault("INTERNAL_DOMAINS", "parametacorp.com,iconloop.com")

import pytest
from unittest.mock import patch, MagicMock, call

# Gemini/Claude client와 google auth 모두 차단
with patch("google.genai.Client"), \
     patch("anthropic.Anthropic"), \
     patch("tools.calendar._service"), \
     patch("tools.drive._service"), \
     patch("tools.gmail._service"):
    import agents.before as before
    from agents.before import (
        handle_agenda_reply,
        _find_email,
        run_briefing,
    )

_TEST_USER_ID = "UTEST"
_MOCK_CREDS = MagicMock()
_MOCK_USER = {
    "slack_user_id": _TEST_USER_ID,
    "contacts_folder_id": "contacts_folder",
    "knowledge_file_id": "knowledge_file",
}


def _slack():
    """mock Slack client"""
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "111.222"}
    return client


def _mock_store():
    """user_store mock 패치 컨텍스트 (get_credentials + get_user)"""
    mock = MagicMock()
    mock.get_credentials.return_value = _MOCK_CREDS
    mock.get_user.return_value = _MOCK_USER
    return patch("agents.before.user_store", mock)


# ── handle_agenda_reply ───────────────────────────────────────

class TestHandleAgendaReply:
    def setup_method(self):
        """각 테스트 전 _pending_agenda 초기화"""
        before._pending_agenda.clear()

    def test_registered_thread_updates_calendar(self):
        """등록된 thread_ts → Calendar 업데이트"""
        before._pending_agenda["ts123"] = ("event_abc", _TEST_USER_ID)
        slack = _slack()

        with _mock_store(), patch("agents.before.cal") as mock_cal:
            handle_agenda_reply(slack, "ts123", "1. 파트너십 논의\n2. 계약 검토")

        mock_cal.update_event_description.assert_called_once_with(
            _MOCK_CREDS, "event_abc", "1. 파트너십 논의\n2. 계약 검토"
        )

    def test_registered_thread_deleted_after_update(self):
        """업데이트 후 _pending_agenda에서 삭제"""
        before._pending_agenda["ts123"] = ("event_abc", _TEST_USER_ID)
        slack = _slack()

        with _mock_store(), patch("agents.before.cal"):
            handle_agenda_reply(slack, "ts123", "어젠다 내용")

        assert "ts123" not in before._pending_agenda

    def test_unregistered_thread_does_nothing(self):
        """등록되지 않은 thread_ts → 아무것도 안 함"""
        slack = _slack()

        with _mock_store(), patch("agents.before.cal") as mock_cal:
            handle_agenda_reply(slack, "unknown_ts", "어젠다")

        mock_cal.update_event_description.assert_not_called()
        slack.chat_postMessage.assert_not_called()

    def test_calendar_error_sends_error_message(self):
        """Calendar 업데이트 실패 시 에러 메시지 발송"""
        before._pending_agenda["ts999"] = ("event_xyz", _TEST_USER_ID)
        slack = _slack()

        with _mock_store(), patch("agents.before.cal") as mock_cal:
            mock_cal.update_event_description.side_effect = Exception("Calendar API 오류")
            handle_agenda_reply(slack, "ts999", "어젠다")

        slack.chat_postMessage.assert_called_once()
        args = slack.chat_postMessage.call_args[1]
        assert "실패" in args["text"] or "오류" in args["text"]


# ── _find_email ───────────────────────────────────────────────

class TestFindEmail:
    def test_found_in_slack_by_real_name(self):
        """Slack real_name 매칭으로 이메일 반환"""
        slack = MagicMock()
        slack.users_list.return_value = {
            "members": [
                {"profile": {"real_name": "김민환", "display_name": "", "email": "kim@kakao.com"}},
            ]
        }
        result = _find_email(_TEST_USER_ID, "김민환", slack)
        assert result == "kim@kakao.com"

    def test_found_in_slack_by_display_name(self):
        """Slack display_name 매칭으로 이메일 반환"""
        slack = MagicMock()
        slack.users_list.return_value = {
            "members": [
                {"profile": {"real_name": "", "display_name": "민환", "email": "minhwan@co.com"}},
            ]
        }
        result = _find_email(_TEST_USER_ID, "민환", slack)
        assert result == "minhwan@co.com"

    def test_slack_fails_fallback_to_drive(self):
        """Slack 실패 시 Drive Contacts에서 검색"""
        slack = MagicMock()
        slack.users_list.side_effect = Exception("API 오류")

        with _mock_store(), patch("agents.before.drive") as mock_drive:
            mock_drive.get_person_info.return_value = (
                "# 홍길동\n이메일: hong@partner.com\n소속: 파트너사",
                "file_id_123"
            )
            result = _find_email(_TEST_USER_ID, "홍길동", slack)

        assert result == "hong@partner.com"

    def test_not_found_returns_none(self):
        """Slack/Drive 모두 없으면 None"""
        slack = MagicMock()
        slack.users_list.return_value = {"members": []}

        with _mock_store(), patch("agents.before.drive") as mock_drive:
            mock_drive.get_person_info.return_value = (None, None)
            result = _find_email(_TEST_USER_ID, "없는사람", slack)

        assert result is None

    def test_drive_email_case_insensitive_key(self):
        """Drive Contacts에서 'email:' (소문자) 키도 인식"""
        slack = MagicMock()
        slack.users_list.return_value = {"members": []}

        with _mock_store(), patch("agents.before.drive") as mock_drive:
            mock_drive.get_person_info.return_value = (
                "# 테스트\nemail: test@example.com",
                "file_id"
            )
            result = _find_email(_TEST_USER_ID, "테스트", slack)

        assert result == "test@example.com"


# ── run_briefing ──────────────────────────────────────────────

class TestRunBriefing:
    def setup_method(self):
        before._pending_agenda.clear()

    def test_no_meetings_sends_empty_message(self):
        """오늘 미팅 없으면 '미팅이 없습니다' 메시지"""
        slack = _slack()

        with _mock_store(), \
             patch("agents.before.cal") as mock_cal, \
             patch("agents.before.drive") as mock_drive:
            mock_cal.get_upcoming_meetings.return_value = []
            mock_drive.get_company_names.return_value = []
            run_briefing(slack, user_id=_TEST_USER_ID)

        # 인트로 메시지 + "미팅 없음" 메시지 = 2회 호출
        calls = slack.chat_postMessage.call_args_list
        no_meeting_calls = [c for c in calls if "없습니다" in c[1].get("text", "")]
        assert len(no_meeting_calls) == 1

    def test_returns_thread_ts_list(self):
        """2개 이벤트 → thread_ts 2개 반환"""
        slack = _slack()
        events = [
            {"id": "e1", "summary": "내부 회의", "start": {"dateTime": "2026-03-24T10:00:00+09:00"}, "attendees": []},
            {"id": "e2", "summary": "내부 스탠드업", "start": {"dateTime": "2026-03-24T11:00:00+09:00"}, "attendees": []},
        ]

        with _mock_store(), \
             patch("agents.before.cal") as mock_cal, \
             patch("agents.before.drive") as mock_drive, \
             patch("agents.before._generate", return_value="null"):
            mock_cal.get_upcoming_meetings.return_value = events
            mock_cal.parse_event.side_effect = lambda e: {
                "id": e["id"],
                "summary": e["summary"],
                "start_time": e["start"]["dateTime"],
                "location": "",
                "meet_link": "",
                "description": "",
                "attendees": [],
            }
            mock_drive.get_company_names.return_value = []
            result = run_briefing(slack, user_id=_TEST_USER_ID)

        assert len(result) == 2

    def test_external_meeting_sends_full_briefing(self):
        """업체명 추출 성공 → blocks 포함한 메시지 발송"""
        slack = _slack()
        events = [{
            "id": "e1",
            "summary": "카카오 미팅",
            "start": {"dateTime": "2026-03-24T15:00:00+09:00"},
            "attendees": [{"email": "user@kakao.com", "displayName": "김민환"}],
        }]

        with _mock_store(), \
             patch("agents.before.cal") as mock_cal, \
             patch("agents.before.drive") as mock_drive, \
             patch("agents.before.gmail") as mock_gmail, \
             patch("agents.before._search", return_value="최신 뉴스"), \
             patch("agents.before._generate", return_value="- 연결점 1"):
            mock_cal.get_upcoming_meetings.return_value = events
            mock_cal.parse_event.return_value = {
                "id": "e1",
                "summary": "카카오 미팅",
                "start_time": "2026-03-24T15:00:00+09:00",
                "location": "",
                "meet_link": "",
                "description": "",
                "attendees": [{"email": "user@kakao.com", "name": "김민환"}],
            }
            mock_drive.get_company_names.return_value = []
            mock_drive.get_company_info.return_value = (None, None, False)
            mock_drive.save_company_info.return_value = "file_id"
            mock_drive.get_company_knowledge.return_value = "회사 지식"
            mock_drive.get_person_info.return_value = (None, None)
            mock_drive.save_person_info.return_value = "file_id2"
            mock_gmail.search_recent_emails.return_value = []

            result = run_briefing(slack, user_id=_TEST_USER_ID)

        # blocks 포함한 메시지가 발송되었는지 확인
        post_calls = slack.chat_postMessage.call_args_list
        block_calls = [c for c in post_calls if c[1].get("blocks")]
        assert len(block_calls) >= 1

    def test_internal_meeting_sends_simple_briefing(self):
        """업체명 없음 → 간단 브리핑 발송 (blocks 포함)"""
        slack = _slack()
        events = [{
            "id": "e1",
            "summary": "팀 스탠드업",
            "start": {"dateTime": "2026-03-24T10:00:00+09:00"},
            "attendees": [],
        }]

        with _mock_store(), \
             patch("agents.before.cal") as mock_cal, \
             patch("agents.before.drive") as mock_drive, \
             patch("agents.before._generate", return_value="null"):
            mock_cal.get_upcoming_meetings.return_value = events
            mock_cal.parse_event.return_value = {
                "id": "e1",
                "summary": "팀 스탠드업",
                "start_time": "2026-03-24T10:00:00+09:00",
                "location": "",
                "meet_link": "",
                "description": "",
                "attendees": [],
            }
            mock_drive.get_company_names.return_value = []
            result = run_briefing(slack, user_id=_TEST_USER_ID)

        post_calls = slack.chat_postMessage.call_args_list
        block_calls = [c for c in post_calls if c[1].get("blocks")]
        assert len(block_calls) >= 1
        assert len(result) == 1
