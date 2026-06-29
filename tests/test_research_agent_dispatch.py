import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock, patch
import agents.research_agent as ra


def test_tool_specs_include_all_sources():
    names = {t["name"] for t in ra._tool_specs()}
    assert {"gmail_search", "gmail_read_thread", "drive_search", "drive_read",
            "slack_channel_history", "trello_lookup", "web_search",
            "ontology_lookup", "submit_research"} <= names


def test_dispatch_routes_to_drive_search():
    ctx = ra.ToolContext(user_id="U1", creds=MagicMock(), slack_client=MagicMock(),
                         folder_id="F1")
    with patch("agents.research_agent.drive.search_files", return_value=[{"name": "x.pdf"}]) as ms:
        out = ra._dispatch("drive_search", {"query": "KOMSA"}, ctx)
    ms.assert_called_once()
    assert "x.pdf" in out


def test_dispatch_unknown_tool_returns_error_string():
    ctx = ra.ToolContext(user_id="U1", creds=MagicMock(), slack_client=None, folder_id="")
    out = ra._dispatch("nope", {}, ctx)
    assert "unknown" in out.lower() or "알 수 없" in out
