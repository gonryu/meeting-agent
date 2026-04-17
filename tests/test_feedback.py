"""agents/feedback.py 단위 테스트"""
import os
os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
os.environ.setdefault("INTERNAL_DOMAINS", "parametacorp.com,iconloop.com")

import pytest
from unittest.mock import patch, MagicMock

with patch("anthropic.Anthropic"), \
     patch("tools.calendar._service"), \
     patch("tools.drive._service"), \
     patch("tools.gmail._service"):
    import agents.feedback as feedback

_TEST_USER = "UTEST"


def _slack():
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "111.222"}
    return client


# ── handle_feedback ──────────────────────────────────────────

class TestHandleFeedback:
    def test_saves_feature_request(self):
        """기능 요청 피드백 분류·저장·확인 메시지 전송"""
        slack = _slack()
        llm_response = '{"category": "feature_request", "summary": "캘린더 주간 뷰 추가"}'

        with patch("agents.feedback.generate_text", return_value=llm_response), \
             patch("agents.feedback.user_store") as mock_store:
            mock_store.save_feedback.return_value = 1
            feedback.handle_feedback(slack, _TEST_USER, "캘린더에 주간 뷰 기능 추가해줘")

        mock_store.save_feedback.assert_called_once_with(
            user_id=_TEST_USER,
            category="feature_request",
            content="캘린더 주간 뷰 추가",
            original="캘린더에 주간 뷰 기능 추가해줘",
        )
        msg = slack.chat_postMessage.call_args
        assert "접수" in msg.kwargs["text"]
        assert "기능 요청" in msg.kwargs["text"]

    def test_saves_bug_report(self):
        """버그 리포트 분류 테스트"""
        slack = _slack()
        llm_response = '{"category": "bug_report", "summary": "브리핑이 안 나옴"}'

        with patch("agents.feedback.generate_text", return_value=llm_response), \
             patch("agents.feedback.user_store") as mock_store:
            mock_store.save_feedback.return_value = 2
            feedback.handle_feedback(slack, _TEST_USER, "브리핑이 안 나와 버그 같아")

        mock_store.save_feedback.assert_called_once()
        args = mock_store.save_feedback.call_args
        assert args.kwargs["category"] == "bug_report"

    def test_saves_improvement(self):
        """개선 요청 분류 테스트"""
        slack = _slack()
        llm_response = '{"category": "improvement", "summary": "회의록 포맷 개선"}'

        with patch("agents.feedback.generate_text", return_value=llm_response), \
             patch("agents.feedback.user_store") as mock_store:
            mock_store.save_feedback.return_value = 3
            feedback.handle_feedback(slack, _TEST_USER, "회의록 포맷을 좀 더 보기 좋게 개선해줘")

        args = mock_store.save_feedback.call_args
        assert args.kwargs["category"] == "improvement"

    def test_fallback_on_llm_failure(self):
        """LLM 실패 시 기본값(feature_request)으로 저장"""
        slack = _slack()

        with patch("agents.feedback.generate_text", side_effect=Exception("LLM 에러")), \
             patch("agents.feedback.user_store") as mock_store:
            mock_store.save_feedback.return_value = 4
            feedback.handle_feedback(slack, _TEST_USER, "이것저것 추가해줘")

        args = mock_store.save_feedback.call_args
        assert args.kwargs["category"] == "feature_request"
        slack.chat_postMessage.assert_called_once()

    def test_invalid_category_fallback(self):
        """잘못된 카테고리 → feature_request 폴백"""
        slack = _slack()
        llm_response = '{"category": "wrong_type", "summary": "테스트"}'

        with patch("agents.feedback.generate_text", return_value=llm_response), \
             patch("agents.feedback.user_store") as mock_store:
            mock_store.save_feedback.return_value = 5
            feedback.handle_feedback(slack, _TEST_USER, "테스트 메시지")

        args = mock_store.save_feedback.call_args
        assert args.kwargs["category"] == "feature_request"

    def test_thread_ts_forwarded(self):
        """thread_ts가 Slack 메시지에 전달되는지 확인"""
        slack = _slack()
        llm_response = '{"category": "bug_report", "summary": "오류"}'

        with patch("agents.feedback.generate_text", return_value=llm_response), \
             patch("agents.feedback.user_store") as mock_store:
            mock_store.save_feedback.return_value = 6
            feedback.handle_feedback(slack, _TEST_USER, "에러 발생",
                                     channel="C123", thread_ts="ts999")

        msg = slack.chat_postMessage.call_args
        assert msg.kwargs["channel"] == "C123"
        assert msg.kwargs["thread_ts"] == "ts999"


# ── send_feedback_digest ─────────────────────────────────────

class TestSendFeedbackDigest:
    def test_no_pending_feedback(self):
        """피드백 없으면 아무것도 안 보냄"""
        slack = _slack()
        with patch("agents.feedback.user_store") as mock_store:
            mock_store.get_pending_feedback.return_value = []
            feedback.send_feedback_digest(slack)

        slack.chat_postMessage.assert_not_called()

    def test_no_channel_configured(self):
        """FEEDBACK_CHANNEL 미설정 시 건너뜀"""
        slack = _slack()
        items = [{"id": 1, "user_id": "U1", "category": "bug_report",
                  "content": "에러", "original": "에러 나요", "created_at": "2026-04-09T10:00:00"}]

        with patch("agents.feedback.user_store") as mock_store, \
             patch.object(feedback, "_FEEDBACK_CHANNEL", ""):
            mock_store.get_pending_feedback.return_value = items
            feedback.send_feedback_digest(slack)

        slack.chat_postMessage.assert_not_called()

    def test_sends_digest_and_marks_notified(self):
        """피드백 다이제스트 발송 후 notified 처리"""
        slack = _slack()
        items = [
            {"id": 1, "user_id": "U1", "category": "bug_report",
             "content": "브리핑 에러", "original": "브리핑 에러 나요",
             "created_at": "2026-04-09T08:00:00"},
            {"id": 2, "user_id": "U2", "category": "feature_request",
             "content": "주간 리포트", "original": "주간 리포트 기능 추가해줘",
             "created_at": "2026-04-09T09:00:00"},
            {"id": 3, "user_id": "U1", "category": "improvement",
             "content": "UI 개선", "original": "UI 좀 개선해줘",
             "created_at": "2026-04-09T10:00:00"},
        ]

        with patch("agents.feedback.user_store") as mock_store, \
             patch.object(feedback, "_FEEDBACK_CHANNEL", "C_ADMIN"):
            mock_store.get_pending_feedback.return_value = items
            feedback.send_feedback_digest(slack)

        # 메시지 발송 확인
        msg = slack.chat_postMessage.call_args
        text = msg.kwargs["text"]
        assert msg.kwargs["channel"] == "C_ADMIN"
        assert "3건" in text
        assert "버그" in text
        assert "기능 요청" in text
        assert "개선" in text
        assert "<@U1>" in text
        assert "<@U2>" in text

        # notified 처리 확인
        mock_store.mark_feedback_notified.assert_called_once_with([1, 2, 3])

    def test_digest_category_order(self):
        """버그 → 기능 요청 → 개선 순서로 출력"""
        slack = _slack()
        items = [
            {"id": 1, "user_id": "U1", "category": "improvement",
             "content": "개선A", "original": "개선A", "created_at": "2026-04-09T08:00:00"},
            {"id": 2, "user_id": "U2", "category": "bug_report",
             "content": "버그B", "original": "버그B", "created_at": "2026-04-09T09:00:00"},
        ]

        with patch("agents.feedback.user_store") as mock_store, \
             patch.object(feedback, "_FEEDBACK_CHANNEL", "C_ADMIN"):
            mock_store.get_pending_feedback.return_value = items
            feedback.send_feedback_digest(slack)

        text = slack.chat_postMessage.call_args.kwargs["text"]
        bug_pos = text.index("버그")
        improve_pos = text.index("개선")
        assert bug_pos < improve_pos

    def test_slack_error_does_not_mark_notified(self):
        """Slack 발송 실패 시 notified 처리하지 않음"""
        slack = _slack()
        slack.chat_postMessage.side_effect = Exception("Slack API 에러")
        items = [{"id": 1, "user_id": "U1", "category": "bug_report",
                  "content": "에러", "original": "에러", "created_at": "2026-04-09T10:00:00"}]

        with patch("agents.feedback.user_store") as mock_store, \
             patch.object(feedback, "_FEEDBACK_CHANNEL", "C_ADMIN"):
            mock_store.get_pending_feedback.return_value = items
            feedback.send_feedback_digest(slack)

        mock_store.mark_feedback_notified.assert_not_called()


# ── user_store feedback CRUD ─────────────────────────────────

class TestFeedbackStore:
    def setup_method(self):
        """테스트용 인메모리 DB"""
        import sqlite3
        from contextlib import contextmanager
        from store import user_store

        self.real_conn = user_store._conn

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE feedback (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     TEXT NOT NULL,
                category    TEXT NOT NULL,
                content     TEXT NOT NULL,
                original    TEXT NOT NULL,
                notified    INTEGER DEFAULT 0,
                created_at  TEXT NOT NULL
            )
        """)
        self._conn_obj = conn

        @contextmanager
        def mock_conn():
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        user_store._conn = mock_conn

    def teardown_method(self):
        from store import user_store
        self._conn_obj.close()
        user_store._conn = self.real_conn

    def test_save_and_get_pending(self):
        from store import user_store
        fid = user_store.save_feedback("U1", "bug_report", "에러 발생", "에러 나요")
        assert fid == 1

        items = user_store.get_pending_feedback()
        assert len(items) == 1
        assert items[0]["user_id"] == "U1"
        assert items[0]["category"] == "bug_report"
        assert items[0]["notified"] == 0

    def test_mark_notified(self):
        from store import user_store
        fid1 = user_store.save_feedback("U1", "bug_report", "에러1", "원본1")
        fid2 = user_store.save_feedback("U2", "feature_request", "기능2", "원본2")

        user_store.mark_feedback_notified([fid1, fid2])

        items = user_store.get_pending_feedback()
        assert len(items) == 0

    def test_mark_notified_empty_list(self):
        """빈 리스트로 호출해도 에러 없음"""
        from store import user_store
        user_store.mark_feedback_notified([])  # 에러 없이 통과

    def test_multiple_feedback_ordering(self):
        """created_at 순서대로 조회"""
        from store import user_store
        user_store.save_feedback("U1", "bug_report", "먼저", "먼저 원본")
        user_store.save_feedback("U2", "improvement", "나중", "나중 원본")

        items = user_store.get_pending_feedback()
        assert len(items) == 2
        assert items[0]["content"] == "먼저"
        assert items[1]["content"] == "나중"
