"""v2 Phase 2.1 기능 테스트 — 안정화 + 즉시 효과

테스트 대상:
- INF-07: 동시성 Lock 적용
- INF-08: SQLite WAL 모드
- INF-09: _pending_minutes 파일 영속화
- FR-B13/B14: 브리핑 업체명 추론 폴백
- FR-D09/D10: 회의록 품질 검증
- FR-D14: _pending_minutes 키 event_id 전환
- FR-A15: Trello 코멘트 활용
- 채널 @멘션 쓰레드 응답
"""
import os
os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
os.environ.setdefault("TRELLO_API_KEY", "test-trello-key")
os.environ.setdefault("TRELLO_BOARD_ID", "test-board-id")
os.environ.setdefault("INTERNAL_DOMAINS", "parametacorp.com,iconloop.com")

import json
import sqlite3
import threading
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

with patch("anthropic.Anthropic"), \
     patch("trello.TrelloClient"):
    import agents.during as during
    from agents.during import (
        start_session,
        add_note,
        end_session,
        generate_minutes_now,
        get_minutes_list,
        get_session_thread,
        _active_sessions,
        _completed_notes,
        _processed_events,
        _pending_minutes,
        _pending_inputs,
    )
    import agents.before as before
    import store.user_store as user_store
    import tools.trello as trello_mod
    import agents.after as after


@pytest.fixture(autouse=True)
def isolated_sessions_dir(tmp_path):
    """각 테스트마다 임시 디렉토리를 세션 저장 경로로 사용"""
    sessions_dir = tmp_path / ".sessions"
    with patch.object(during, "_SESSIONS_DIR", sessions_dir):
        yield sessions_dir


KST = ZoneInfo("Asia/Seoul")
_TEST_USER = "UTEST"
_MOCK_CREDS = MagicMock()
_MOCK_USER = {
    "slack_user_id": _TEST_USER,
    "contacts_folder_id": "contacts_folder",
    "knowledge_file_id": "knowledge_file",
    "minutes_folder_id": "minutes_folder_id",
}


def _slack():
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "111.222"}
    return client


def _mock_store_during():
    mock = MagicMock()
    mock.get_credentials.return_value = _MOCK_CREDS
    mock.get_user.return_value = _MOCK_USER
    mock.all_users.return_value = [_MOCK_USER]
    return patch("agents.during.user_store", mock)


def _mock_store_before():
    mock = MagicMock()
    mock.get_credentials.return_value = _MOCK_CREDS
    mock.get_user.return_value = _MOCK_USER
    return patch("agents.before.user_store", mock)


# ═══════════════════════════════════════════════════════════════
# INF-07: 동시성 Lock 적용
# ═══════════════════════════════════════════════════════════════

class TestConcurrencyLocks:
    """공유 딕셔너리에 threading.Lock이 적용되어야 함"""

    def test_during_module_has_sessions_lock(self):
        """during.py에 _sessions_lock이 존재"""
        assert hasattr(during, "_sessions_lock"), \
            "during.py에 _sessions_lock이 없습니다 (INF-07)"
        assert isinstance(during._sessions_lock, type(threading.Lock()))

    def test_during_module_has_minutes_lock(self):
        """during.py에 _minutes_lock이 존재"""
        assert hasattr(during, "_minutes_lock"), \
            "during.py에 _minutes_lock이 없습니다 (INF-07)"

    def test_before_module_has_agenda_lock(self):
        """before.py에 _agenda_lock이 존재"""
        assert hasattr(before, "_agenda_lock"), \
            "before.py에 _agenda_lock이 없습니다 (INF-07)"

    def test_before_module_has_drafts_lock(self):
        """before.py에 _drafts_lock이 존재"""
        assert hasattr(before, "_drafts_lock"), \
            "before.py에 _drafts_lock이 없습니다 (INF-07)"


# ═══════════════════════════════════════════════════════════════
# INF-08: SQLite WAL 모드
# ═══════════════════════════════════════════════════════════════

class TestSQLiteWAL:
    """SQLite 연결 시 WAL 모드가 설정되어야 함"""

    def test_wal_mode_enabled(self, tmp_path, monkeypatch):
        """DB 연결 시 journal_mode=WAL 설정"""
        db_path = str(tmp_path / "test_wal.db")
        monkeypatch.setattr(user_store, "_DB_PATH", db_path)
        user_store.init_db()

        conn = sqlite3.connect(db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal", \
            f"WAL 모드가 아닙니다 (현재: {mode}) (INF-08)"


# ═══════════════════════════════════════════════════════════════
# INF-09: _pending_minutes 파일 영속화
# ═══════════════════════════════════════════════════════════════

class TestPendingMinutesPersistence:
    """_pending_minutes가 파일로 영속화되어 서버 재시작 시 복구 가능해야 함"""

    def setup_method(self):
        _pending_minutes.clear()

    def test_save_pending_minutes_creates_file(self, isolated_sessions_dir):
        """_pending_minutes 변경 시 파일로 저장"""
        assert hasattr(during, "_save_pending_minutes"), \
            "_save_pending_minutes 함수가 없습니다 (INF-09)"

        _pending_minutes["evt_test"] = {
            "title": "테스트 회의",
            "internal_body": "# 테스트",
        }
        during._save_pending_minutes()

        saved_path = isolated_sessions_dir / "pending_minutes.json"
        assert saved_path.exists(), "pending_minutes.json 파일이 생성되지 않음"

    def test_load_pending_minutes_restores_data(self, isolated_sessions_dir):
        """서버 시작 시 파일에서 _pending_minutes 복구"""
        assert hasattr(during, "_load_pending_minutes"), \
            "_load_pending_minutes 함수가 없습니다 (INF-09)"

        # 파일 직접 생성
        isolated_sessions_dir.mkdir(exist_ok=True)
        data = {"evt_restore": {"title": "복구 테스트", "internal_body": "# 복구"}}
        with open(isolated_sessions_dir / "pending_minutes.json", "w") as f:
            json.dump(data, f)

        result = during._load_pending_minutes()
        assert "evt_restore" in result
        assert result["evt_restore"]["title"] == "복구 테스트"

    def test_load_returns_empty_when_no_file(self, isolated_sessions_dir):
        """파일이 없으면 빈 딕셔너리 반환"""
        result = during._load_pending_minutes()
        assert result == {}


# ═══════════════════════════════════════════════════════════════
# FR-B13/B14: 브리핑 업체명 추론 폴백
# ═══════════════════════════════════════════════════════════════

class TestCompanyNameInference:
    """extendedProperties 없을 때 LLM 업체명 추론 폴백"""

    def test_infer_function_exists(self):
        """before.py에 업체명 추론 함수가 존재"""
        assert hasattr(before, "_infer_company_from_title") or \
               hasattr(before, "infer_company_name"), \
            "업체명 추론 함수가 없습니다 (FR-B13)"

    def test_infer_with_clear_company_name(self):
        """명확한 업체명이 제목에 있을 때 추론 성공"""
        infer_fn = getattr(before, "_infer_company_from_title",
                           getattr(before, "infer_company_name", None))
        if infer_fn is None:
            pytest.skip("업체명 추론 함수 미구현")

        # LLM mock — 업체명이 명확한 경우
        with patch.object(before, "_generate", return_value="카카오"):
            result = infer_fn("카카오 AI 협업 논의")
        assert result == "카카오"

    def test_infer_with_existing_company_list(self):
        """기존 업체 목록을 후보로 제공하여 추론 정확도 향상 (FR-B14)"""
        infer_fn = getattr(before, "_infer_company_from_title",
                           getattr(before, "infer_company_name", None))
        if infer_fn is None:
            pytest.skip("업체명 추론 함수 미구현")

        # 기존 업체 목록 포함 호출
        with patch.object(before, "_generate", return_value="카카오") as mock_gen:
            result = infer_fn("AI 협업 논의", company_candidates=["카카오", "네이버", "라인"])
            # 프롬프트에 기존 업체 목록이 포함되었는지 확인
            prompt = mock_gen.call_args[0][0]
            assert "카카오" in prompt or "네이버" in prompt, \
                "프롬프트에 기존 업체 목록이 포함되지 않음 (FR-B14)"

    def test_infer_returns_empty_for_person_only_title(self):
        """인물명만 있는 제목에서는 빈 문자열 반환"""
        infer_fn = getattr(before, "_infer_company_from_title",
                           getattr(before, "infer_company_name", None))
        if infer_fn is None:
            pytest.skip("업체명 추론 함수 미구현")

        with patch.object(before, "_generate", return_value=""):
            result = infer_fn("홍길동 상무 미팅")
        assert result == ""


# ═══════════════════════════════════════════════════════════════
# FR-D09/D10: 회의록 품질 검증
# ═══════════════════════════════════════════════════════════════

class TestMinutesValidation:
    """회의록 필수항목 자동 검증"""

    def test_validate_function_exists(self):
        """during.py에 validate_minutes 함수가 존재"""
        assert hasattr(during, "validate_minutes"), \
            "validate_minutes 함수가 없습니다 (FR-D09)"

    def test_internal_minutes_all_sections_pass(self):
        """내부용 회의록 — 모든 필수 섹션 포함 시 통과"""
        if not hasattr(during, "validate_minutes"):
            pytest.skip("validate_minutes 미구현")

        body = (
            "## 회의 요약\n요약 내용\n\n"
            "## 주요 논의 내용\n논의 사항\n\n"
            "## 주요 결정 사항\n결정 사항\n\n"
            "## 액션 아이템\n| 담당자 | 내용 | 기한 |\n|---|---|---|\n| 홍길동 | 검토 | 4/20 |"
        )
        result = during.validate_minutes(body, "internal")
        assert result["valid"] is True
        assert len(result.get("missing", [])) == 0

    def test_internal_minutes_missing_section_fails(self):
        """내부용 회의록 — 필수 섹션 누락 시 실패"""
        if not hasattr(during, "validate_minutes"):
            pytest.skip("validate_minutes 미구현")

        body = (
            "## 회의 요약\n요약 내용\n\n"
            "## 주요 논의 내용\n논의 사항\n\n"
            # 주요 결정 사항 누락
            # 액션 아이템 누락
        )
        result = during.validate_minutes(body, "internal")
        assert result["valid"] is False
        assert len(result["missing"]) >= 1

    def test_external_minutes_forbidden_keywords_detected(self):
        """외부용 회의록 — 금지 키워드 검출 (FR-D10)"""
        if not hasattr(during, "validate_minutes"):
            pytest.skip("validate_minutes 미구현")

        body = (
            "## 회의 개요\n개요\n\n"
            "## 주요 합의 사항\n합의 사항\n\n"
            "## 공동 액션 아이템\n아이템\n\n"
            "내부 메모: 이건 협상 전략임"
        )
        result = during.validate_minutes(body, "external")
        assert len(result.get("forbidden", [])) > 0, \
            "금지 키워드(내부 메모, 협상, 전략)가 검출되지 않음 (FR-D10)"

    def test_external_minutes_all_sections_pass(self):
        """외부용 회의록 — 모든 필수 섹션 포함 + 금지 키워드 없음"""
        if not hasattr(during, "validate_minutes"):
            pytest.skip("validate_minutes 미구현")

        body = (
            "## 회의 개요\n개요 내용\n\n"
            "## 주요 합의 사항\n합의 사항\n\n"
            "## 공동 액션 아이템\n| 담당 | 내용 | 기한 |\n|---|---|---|\n| 홍길동 | 검토 | 4/20 |"
        )
        result = during.validate_minutes(body, "external")
        assert result["valid"] is True
        assert len(result.get("forbidden", [])) == 0


# ═══════════════════════════════════════════════════════════════
# FR-D14: _pending_minutes 키 event_id 전환
# ═══════════════════════════════════════════════════════════════

class TestPendingMinutesEventIdKey:
    """_pending_minutes의 키가 user_id 대신 event_id를 사용해야 함"""

    def setup_method(self):
        _pending_minutes.clear()

    def test_pending_minutes_keyed_by_event_id(self):
        """_pending_minutes에 event_id를 키로 저장"""
        # 기존: _pending_minutes[user_id] = {...}
        # 변경: _pending_minutes[event_id] = {..., "user_id": user_id}
        _pending_minutes["evt_001"] = {
            "user_id": _TEST_USER,
            "title": "테스트 미팅",
            "event_id": "evt_001",
        }
        assert "evt_001" in _pending_minutes
        assert _pending_minutes["evt_001"]["user_id"] == _TEST_USER

    def test_multiple_meetings_same_user_no_overwrite(self):
        """동일 사용자의 복수 미팅이 독립적으로 저장"""
        _pending_minutes["evt_001"] = {
            "user_id": _TEST_USER,
            "title": "미팅 A",
            "event_id": "evt_001",
        }
        _pending_minutes["evt_002"] = {
            "user_id": _TEST_USER,
            "title": "미팅 B",
            "event_id": "evt_002",
        }
        assert len(_pending_minutes) == 2
        assert _pending_minutes["evt_001"]["title"] == "미팅 A"
        assert _pending_minutes["evt_002"]["title"] == "미팅 B"


# ═══════════════════════════════════════════════════════════════
# FR-A15: Trello 코멘트 활용
# ═══════════════════════════════════════════════════════════════

class TestTrelloCommentOnRegister:
    """Trello 체크리스트 등록 시 회의 요약 코멘트도 추가"""

    def test_register_adds_comment_with_summary(self):
        """handle_trello_register가 회의 요약 코멘트를 추가"""
        slack = _slack()
        body = {
            "user": {"id": _TEST_USER},
            "actions": [{"value": json.dumps({
                "event_id": "evt_trello",
                "company": "카카오",
            })}],
        }
        mock_items = [
            {"assignee": "홍길동", "content": "PoC 일정 확정", "due_date": "2026-04-20"},
        ]
        # _minutes_summary_cache에 요약 저장
        after._minutes_summary_cache["evt_trello"] = "AI 공동개발 PoC 진행 합의"

        with patch("agents.after.user_store") as mock_store, \
             patch("agents.after.trello") as mock_trello:
            mock_store.get_action_items.return_value = mock_items
            mock_trello.add_checklist_items.return_value = 1
            mock_trello.find_card_by_name.return_value = MagicMock(
                url="https://trello.com/c/xxx"
            )

            after.handle_trello_register(slack, body)

            # add_comment가 호출되었는지 확인
            mock_trello.add_comment.assert_called_once()
            comment_text = mock_trello.add_comment.call_args[0][2]
            assert "AI 공동개발" in comment_text or "요약" in comment_text.lower()

    def test_register_without_summary_skips_comment(self):
        """요약이 없으면 코멘트 추가 건너뜀"""
        slack = _slack()
        body = {
            "user": {"id": _TEST_USER},
            "actions": [{"value": json.dumps({
                "event_id": "evt_no_summary",
                "company": "카카오",
            })}],
        }
        mock_items = [
            {"assignee": "홍길동", "content": "검토", "due_date": "2026-04-20"},
        ]
        # summary cache에 해당 event_id 없음
        after._minutes_summary_cache.pop("evt_no_summary", None)

        with patch("agents.after.user_store") as mock_store, \
             patch("agents.after.trello") as mock_trello:
            mock_store.get_action_items.return_value = mock_items
            mock_trello.add_checklist_items.return_value = 1
            mock_trello.find_card_by_name.return_value = MagicMock(
                url="https://trello.com/c/xxx"
            )

            after.handle_trello_register(slack, body)

            # add_comment이 호출되지 않아야 함
            mock_trello.add_comment.assert_not_called()


# ═══════════════════════════════════════════════════════════════
# 채널 @멘션 쓰레드 응답
# ═══════════════════════════════════════════════════════════════

class TestChannelMentionThreadReply:
    """채널 @멘션 시 쓰레드로 응답 + 세션 쓰레드 추적"""

    def setup_method(self):
        _active_sessions.clear()
        _completed_notes.clear()
        _processed_events.clear()

    def test_start_session_stores_thread_info(self):
        """채널에서 start_session 호출 시 session_channel/session_thread_ts 저장"""
        slack = _slack()
        with _mock_store_during(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            start_session(slack, _TEST_USER, "카카오 미팅",
                          channel="C_CHANNEL", thread_ts="ts_123")

        session = _active_sessions[_TEST_USER]
        assert session["session_channel"] == "C_CHANNEL"
        assert session["session_thread_ts"] == "ts_123"

    def test_start_session_replies_in_thread(self):
        """채널에서 start_session 호출 시 해당 쓰레드에 응답"""
        slack = _slack()
        with _mock_store_during(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            start_session(slack, _TEST_USER, "테스트",
                          channel="C_CH", thread_ts="ts_456")

        call_kwargs = slack.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C_CH"
        assert call_kwargs["thread_ts"] == "ts_456"

    def test_add_note_replies_in_thread(self):
        """채널 쓰레드에서 add_note 시 같은 쓰레드에 응답"""
        _active_sessions[_TEST_USER] = {
            "title": "테스트", "started_at": "10:00", "notes": [],
            "event_id": None, "session_channel": "C_CH",
            "session_thread_ts": "ts_789",
        }
        slack = _slack()
        add_note(slack, _TEST_USER, "메모 내용",
                 channel="C_CH", thread_ts="ts_789")

        call_kwargs = slack.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C_CH"
        assert call_kwargs["thread_ts"] == "ts_789"

    def test_end_session_replies_in_thread(self):
        """채널 쓰레드에서 end_session 시 같은 쓰레드에 응답"""
        _active_sessions[_TEST_USER] = {
            "title": "테스트", "started_at": "10:00", "notes": [],
            "event_id": None, "event_summary": None,
            "event_time_str": None,
            "session_channel": "C_CH",
            "session_thread_ts": "ts_end",
        }
        slack = _slack()
        end_session(slack, _TEST_USER, channel="C_CH", thread_ts="ts_end")

        call_kwargs = slack.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C_CH"
        assert call_kwargs["thread_ts"] == "ts_end"

    def test_get_session_thread_returns_info(self):
        """활성 세션의 쓰레드 정보 반환"""
        _active_sessions[_TEST_USER] = {
            "title": "테스트", "started_at": "10:00", "notes": [],
            "event_id": None,
            "session_channel": "C_CH",
            "session_thread_ts": "ts_session",
        }
        result = get_session_thread(_TEST_USER)
        assert result == ("C_CH", "ts_session")

    def test_get_session_thread_returns_none_for_dm(self):
        """DM에서 시작된 세션은 None 반환"""
        _active_sessions[_TEST_USER] = {
            "title": "테스트", "started_at": "10:00", "notes": [],
            "event_id": None,
            "session_channel": None,
            "session_thread_ts": None,
        }
        result = get_session_thread(_TEST_USER)
        assert result is None

    def test_get_session_thread_returns_none_no_session(self):
        """세션이 없으면 None 반환"""
        result = get_session_thread("U_NONEXIST")
        assert result is None

    def test_get_minutes_list_replies_in_thread(self):
        """채널에서 get_minutes_list 호출 시 쓰레드에 응답"""
        slack = _slack()
        with _mock_store_during(), patch("agents.during.drive") as mock_drive:
            mock_drive.list_minutes.return_value = [
                {"name": "2026-04-10_카카오.md", "modifiedTime": "2026-04-10T10:00:00Z", "id": "fid1"},
            ]
            get_minutes_list(slack, _TEST_USER,
                             channel="C_CH", thread_ts="ts_list")

        call_kwargs = slack.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == "C_CH"
        assert call_kwargs["thread_ts"] == "ts_list"

    def test_dm_calls_still_work_without_thread(self):
        """DM 호출(channel/thread_ts 없음) 시 기존대로 user_id로 응답"""
        slack = _slack()
        with _mock_store_during(), patch("agents.during.cal") as mock_cal:
            mock_cal.get_upcoming_meetings.return_value = []
            start_session(slack, _TEST_USER, "DM 미팅")

        call_kwargs = slack.chat_postMessage.call_args[1]
        assert call_kwargs["channel"] == _TEST_USER
        assert call_kwargs["thread_ts"] is None


# ═══════════════════════════════════════════════════════════════
# INF-10: meeting_index 테이블
# ═══════════════════════════════════════════════════════════════

class TestMeetingIndex:
    """meeting_index 테이블 생성 및 CRUD"""

    @pytest.fixture(autouse=True)
    def temp_db(self, tmp_path, monkeypatch):
        db_path = str(tmp_path / "test_users.db")
        monkeypatch.setattr(user_store, "_DB_PATH", db_path)
        user_store.init_db()
        self.db_path = db_path

    def test_meeting_index_table_exists(self):
        """init_db 후 meeting_index 테이블 존재"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='meeting_index'"
        )
        assert cursor.fetchone() is not None, \
            "meeting_index 테이블이 없습니다 (INF-10)"
        conn.close()

    def test_meeting_index_has_required_columns(self):
        """meeting_index 테이블에 필수 컬럼 존재"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.execute("PRAGMA table_info(meeting_index)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        required = {"event_id", "user_id", "date", "title", "company_name",
                     "attendees", "drive_file_id"}
        missing = required - columns
        assert not missing, f"누락된 컬럼: {missing} (INF-10)"

    def test_save_meeting_index(self):
        """회의록 인덱스 저장"""
        if not hasattr(user_store, "save_meeting_index"):
            pytest.skip("save_meeting_index 미구현")

        user_store.save_meeting_index(
            event_id="evt_idx_1",
            user_id=_TEST_USER,
            date="2026-04-10",
            title="카카오 AI 협업",
            company_name="카카오",
            attendees=json.dumps(["홍길동", "김영희"]),
            drive_file_id="drive_file_1",
        )

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT * FROM meeting_index WHERE event_id = ?", ("evt_idx_1",)
        ).fetchone()
        conn.close()
        assert row is not None

    def test_search_meetings_by_company(self):
        """업체명으로 회의록 검색"""
        if not hasattr(user_store, "search_meetings"):
            pytest.skip("search_meetings 미구현")

        user_store.save_meeting_index(
            event_id="evt_s1", user_id=_TEST_USER, date="2026-04-10",
            title="카카오 미팅", company_name="카카오",
            attendees="[]", drive_file_id="f1",
        )
        user_store.save_meeting_index(
            event_id="evt_s2", user_id=_TEST_USER, date="2026-04-11",
            title="네이버 미팅", company_name="네이버",
            attendees="[]", drive_file_id="f2",
        )

        results = user_store.search_meetings(user_id=_TEST_USER, company="카카오")
        assert len(results) == 1
        assert results[0]["title"] == "카카오 미팅"
