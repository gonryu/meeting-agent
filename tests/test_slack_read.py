import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock
from tools.slack_read import channel_history, allowed_channels


def _client_with_members(members, messages=None):
    client = MagicMock()
    client.conversations_members.return_value = {"members": members, "response_metadata": {"next_cursor": ""}}
    client.conversations_history.return_value = {"messages": messages or [
        {"text": "NH PoC 농협 일정 9월로 연기", "ts": "1.0", "user": "U1"}]}
    return client


def test_allowlist_blocks_unlisted(monkeypatch):
    monkeypatch.setenv("SLACK_BIZ_CHANNELS", "C_BIZ1,C_BIZ2")
    assert "C_BIZ1" in allowed_channels()
    client = MagicMock()
    assert channel_history(client, "C_OTHER", requesting_user_id="U1") == []
    client.conversations_history.assert_not_called()


def test_gate_off_allowlist_only_reads(monkeypatch):
    # 기본(게이트 OFF): allowlist면 멤버십 체크 없이 읽음(추가 스코프 불필요)
    monkeypatch.delenv("SLACK_MEMBERSHIP_GATE", raising=False)
    monkeypatch.setenv("SLACK_BIZ_CHANNELS", "C_BIZ1")
    client = _client_with_members(["U2"])   # U1 없어도
    out = channel_history(client, "C_BIZ1", requesting_user_id="U1", limit=10)
    assert any("9월로 연기" in m["text"] for m in out)
    client.conversations_members.assert_not_called()   # 게이트 OFF면 멤버십 확인 안 함


def test_gate_on_member_reads(monkeypatch):
    monkeypatch.setenv("SLACK_MEMBERSHIP_GATE", "true")
    monkeypatch.setenv("SLACK_BIZ_CHANNELS", "C_BIZ1")
    client = _client_with_members(["U1", "U2"])
    out = channel_history(client, "C_BIZ1", requesting_user_id="U1", limit=10)
    assert any("9월로 연기" in m["text"] for m in out)


def test_gate_on_non_member_blocked(monkeypatch):
    monkeypatch.setenv("SLACK_MEMBERSHIP_GATE", "true")
    monkeypatch.setenv("SLACK_BIZ_CHANNELS", "C_BIZ1")
    client = _client_with_members(["U2", "U3"])   # U1 없음
    out = channel_history(client, "C_BIZ1", requesting_user_id="U1")
    assert out == []
    client.conversations_history.assert_not_called()


def test_gate_on_error_fail_closed(monkeypatch):
    monkeypatch.setenv("SLACK_MEMBERSHIP_GATE", "true")
    monkeypatch.setenv("SLACK_BIZ_CHANNELS", "C_BIZ1")
    client = MagicMock()
    client.conversations_members.side_effect = RuntimeError("scope missing")
    out = channel_history(client, "C_BIZ1", requesting_user_id="U1")
    assert out == []   # 게이트 ON·확인 실패 = 비노출
    client.conversations_history.assert_not_called()


def test_biz_channel_list_parses_id_and_name(monkeypatch):
    from tools.slack_read import biz_channel_list, allowed_channels
    monkeypatch.setenv("SLACK_BIZ_CHANNELS", "C1:parasta_biz, C2, C3:nh_biz")
    lst = biz_channel_list()
    assert {"id": "C1", "name": "parasta_biz"} in lst
    assert {"id": "C2", "name": ""} in lst
    assert allowed_channels() == {"C1", "C2", "C3"}
