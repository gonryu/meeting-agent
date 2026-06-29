import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock, patch
import tools.gmail as gmail


def _thread_payload():
    return {"messages": [
        {"payload": {"headers": [{"name": "From", "value": "이성룡 <a@d-antwort.com>"},
                                  {"name": "Date", "value": "Sun, 15 Jun 2026 10:00:00 +0900"},
                                  {"name": "Subject", "value": "KOMSA 견적서"}],
                     "body": {"data": ""},
                     "parts": [{"mimeType": "text/plain",
                                "body": {"data": "VG90YWwgNTUsMDQwLDAwMA=="}}]}},  # "Total 55,040,000"
    ]}


def test_read_thread_returns_messages_with_body():
    with patch.object(gmail, "_service") as msvc:
        msvc.return_value.users.return_value.threads.return_value.get.return_value.execute.return_value = _thread_payload()
        out = gmail.read_thread(MagicMock(), "thread123")
    assert out and out[0]["from"].startswith("이성룡")
    assert "55,040,000" in out[0]["body"]
    assert out[0]["subject"] == "KOMSA 견적서"
