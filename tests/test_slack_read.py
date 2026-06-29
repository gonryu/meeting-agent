import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock
from tools.slack_read import channel_history, allowed_channels


def test_allowlist_blocks_unlisted(monkeypatch):
    monkeypatch.setenv("SLACK_BIZ_CHANNELS", "C_BIZ1,C_BIZ2")
    assert "C_BIZ1" in allowed_channels()
    client = MagicMock()
    assert channel_history(client, "C_OTHER") == []
    client.conversations_history.assert_not_called()


def test_returns_recent_messages(monkeypatch):
    monkeypatch.setenv("SLACK_BIZ_CHANNELS", "C_BIZ1")
    client = MagicMock()
    client.conversations_history.return_value = {"messages": [
        {"text": "NH PoC 농협 일정 9월로 연기", "ts": "1.0", "user": "U1"},
        {"text": "펌뱅킹 7/14 확정", "ts": "2.0", "user": "U2"}]}
    out = channel_history(client, "C_BIZ1", limit=10)
    assert any("9월로 연기" in m["text"] for m in out)
