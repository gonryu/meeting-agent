import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import agents.research_agent as ra


def _b(**kw):
    return SimpleNamespace(**kw)


def test_loop_runs_tools_then_submit():
    r1 = SimpleNamespace(content=[_b(type="tool_use", id="t1", name="drive_search", input={"query": "KOMSA"})])
    r2 = SimpleNamespace(content=[_b(type="tool_use", id="t2", name="submit_research",
            input={"summary_line": "홍보 용역", "company_identity_confirmed": "komsa=해양교통안전공단",
                   "news": [{"title": "전자증서", "summary": "블록체인 발급", "url": "https://x"}],
                   "talking_points": ["굿즈 45%"]})])
    with patch.object(ra._claude.messages, "create", side_effect=[r1, r2]) as mc, \
         patch("agents.research_agent.drive.search_files", return_value=[{"name": "견적서.pdf", "id": "f1"}]), \
         patch.object(ra, "_run_critics", side_effect=lambda r, ctx, called: r):
        ctx = ra.ToolContext(user_id="U1", creds=MagicMock(), folder_id="F1")
        out = ra._agent_loop("KOMSA", "", ctx)
    assert out is not None
    assert out.summary_line == "홍보 용역"
    assert out.news[0].title == "전자증서"
    assert out.talking_points == ["굿즈 45%"]
    assert mc.call_count == 2


def test_loop_returns_none_if_no_submit_in_budget():
    busy = SimpleNamespace(content=[_b(type="tool_use", id="t", name="web_search", input={"query": "x"})])
    with patch.object(ra._claude.messages, "create", return_value=busy), \
         patch("agents.before._search", return_value="..."), \
         patch.object(ra, "_MAX_ROUNDS", 3):
        ctx = ra.ToolContext(user_id="U1", creds=MagicMock(), folder_id="F1")
        out = ra._agent_loop("X", "", ctx)
    assert out is None
