"""tools/slack_logger.py — 발송 로깅 래퍼 단위 테스트"""
import base64
import os

os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

import pytest
from unittest.mock import MagicMock, patch
from slack_sdk.errors import SlackApiError

import tools.slack_logger as slack_logger


class FakeClient:
    """WebClient 대역 — install_logging이 인스턴스 속성으로 메서드를 교체할 수 있다."""
    pass


def _fake():
    c = FakeClient()
    c.chat_postMessage = MagicMock(return_value={"ok": True, "ts": "1.2"})
    c.chat_update = MagicMock(return_value={"ok": True})
    c.chat_postEphemeral = MagicMock(return_value={"ok": True})
    c.conversations_open = MagicMock(return_value={"ok": True})
    return c


class TestPureHelpers:
    def test_recipient_kind(self):
        assert slack_logger._recipient_kind("U123") == ("dm", "U123")
        assert slack_logger._recipient_kind("D123") == ("dm", None)
        assert slack_logger._recipient_kind("C123") == ("channel", None)
        assert slack_logger._recipient_kind(None) == (None, None)

    def test_infer_category(self):
        assert slack_logger._infer_category("오늘의 미팅 브리핑입니다", None) == "briefing"
        assert slack_logger._infer_category("회의록 초안이 준비됐어요", None) == "minutes"
        assert slack_logger._infer_category("그냥 평범한 메시지", None) == "other"

    def test_truncate_blocks_caps_size(self):
        big = [{"type": "section", "text": "x" * 30000}]
        out = slack_logger._truncate_blocks(big)
        assert out.endswith("…(truncated)")
        assert slack_logger._truncate_blocks(None) is None


class TestInstallLogging:
    def test_success_logs_ok_and_returns_response(self):
        c = _fake()
        slack_logger.install_logging(c)
        with patch.object(slack_logger.user_store, "log_message") as mock_log:
            resp = c.chat_postMessage(channel="U1", text="안녕")
        assert resp == {"ok": True, "ts": "1.2"}
        kw = mock_log.call_args.kwargs
        assert kw["ok"] is True
        assert kw["method"] == "post"
        assert kw["recipient_user_id"] == "U1"
        assert kw["text"] == "안녕"

    def test_failure_logs_and_reraises(self):
        c = _fake()
        c.chat_postMessage.side_effect = SlackApiError("boom", {"error": "channel_not_found"})
        slack_logger.install_logging(c)
        with patch.object(slack_logger.user_store, "log_message") as mock_log:
            with pytest.raises(SlackApiError):
                c.chat_postMessage(channel="U1", text="x")
        kw = mock_log.call_args.kwargs
        assert kw["ok"] is False
        assert kw["error"] == "channel_not_found"

    def test_logging_failure_never_breaks_send(self):
        c = _fake()
        slack_logger.install_logging(c)
        with patch.object(slack_logger.user_store, "log_message",
                          side_effect=RuntimeError("db down")):
            resp = c.chat_postMessage(channel="U1", text="x")
        assert resp == {"ok": True, "ts": "1.2"}  # 발송은 정상

    def test_non_logged_method_passthrough(self):
        c = _fake()
        slack_logger.install_logging(c)
        with patch.object(slack_logger.user_store, "log_message") as mock_log:
            c.conversations_open(users="U1")
        mock_log.assert_not_called()

    def test_idempotent(self):
        c = _fake()
        slack_logger.install_logging(c)
        first = c.chat_postMessage
        slack_logger.install_logging(c)  # 두 번째 호출은 무시돼야 함
        assert c.chat_postMessage is first
