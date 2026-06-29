import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock, patch
import tools.gmail as gmail


def _thread_payload():
    def _msg(subj, b64):
        return {"payload": {"headers": [{"name": "From", "value": "a@b.com"},
                                        {"name": "Subject", "value": subj}],
                            "body": {"data": ""},
                            "parts": [{"mimeType": "text/plain", "body": {"data": b64}}]}}
    return {"messages": [_msg("RFQ", "UkZRIGJvZHk="),          # "RFQ body" (oldest)
                          _msg("최종확정", "VG90YWwgNTUsMDQwLDAwMA==")]}  # newest


def test_read_thread_newest_first():
    with patch.object(gmail, "_service") as msvc:
        msvc.return_value.users.return_value.threads.return_value.get.return_value.execute.return_value = _thread_payload()
        out = gmail.read_thread(MagicMock(), "t1")
    assert out[0]["subject"] == "최종확정"          # 최신이 먼저
    assert "55,040,000" in out[0]["body"]
    assert out[-1]["subject"] == "RFQ"


def test_search_recent_emails_includes_thread_id():
    """멀티홉(gmail_search→read_thread)을 위해 검색 결과에 thread_id가 포함돼야 함."""
    detail = {"threadId": "t1", "snippet": "견적 안내",
              "payload": {"headers": [{"name": "From", "value": "a@b.com"},
                                      {"name": "Subject", "value": "KOMSA 견적"},
                                      {"name": "Date", "value": "Sun, 15 Jun 2026 10:00:00 +0900"}],
                          "parts": [{"mimeType": "text/plain", "body": {"data": "aGVsbG8="}}]}}
    with patch.object(gmail, "_service") as msvc, \
         patch.object(gmail, "_is_worthless_email", return_value=False):
        users = msvc.return_value.users.return_value
        users.messages.return_value.list.return_value.execute.return_value = {"messages": [{"id": "m1", "threadId": "t1"}]}
        users.messages.return_value.get.return_value.execute.return_value = detail
        out = gmail.search_recent_emails(MagicMock(), "KOMSA", "KOMSA")
    assert out and out[0]["thread_id"] == "t1"
