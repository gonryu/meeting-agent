import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import agents.research_agent as ra


def _b(**kw):
    return SimpleNamespace(**kw)


def test_loop_runs_tools_then_submit():
    # 핵심 소스(gmail+drive) 모두 들른 뒤 submit → nudge 없이 1회에 수락(mc.call_count==2)
    r1 = SimpleNamespace(content=[
        _b(type="tool_use", id="t1", name="drive_search", input={"query": "KOMSA"}),
        _b(type="tool_use", id="t1b", name="gmail_search", input={"query": "KOMSA"})])
    r2 = SimpleNamespace(content=[_b(type="tool_use", id="t2", name="submit_research",
            input={"summary_line": "홍보 용역", "company_identity_confirmed": "komsa=해양교통안전공단",
                   "news": [{"title": "전자증서", "summary": "블록체인 발급", "url": "https://x"}],
                   "talking_points": ["굿즈 45%"]})])
    with patch.object(ra._claude.messages, "create", side_effect=[r1, r2]) as mc, \
         patch("agents.research_agent.drive.search_files", return_value=[{"name": "견적서.pdf", "id": "f1"}]), \
         patch("agents.research_agent.gmail.search_recent_emails", return_value=[]), \
         patch.object(ra, "_run_critics", side_effect=lambda r, ctx, called, identity_claim="": r):
        ctx = ra.ToolContext(user_id="U1", creds=MagicMock(), folder_id="F1")
        out = ra._agent_loop("KOMSA", "", ctx)
    assert out is not None
    assert out.summary_line == "홍보 용역"
    assert out.news[0].title == "전자증서"
    assert out.talking_points == ["굿즈 45%"]
    assert mc.call_count == 2


def test_coverage_nudge_then_accept():
    submit_in = {"summary_line": "s", "company_identity_confirmed": "komsa=해양"}
    r1 = SimpleNamespace(content=[_b(type="tool_use", id="s1", name="submit_research", input=submit_in)])  # gap
    r2 = SimpleNamespace(content=[_b(type="tool_use", id="d1", name="drive_search", input={"query": "x"})])
    r3 = SimpleNamespace(content=[_b(type="tool_use", id="g1", name="gmail_search", input={"query": "x"})])
    r4 = SimpleNamespace(content=[_b(type="tool_use", id="s2", name="submit_research", input=submit_in)])  # now satisfied
    with patch.object(ra._claude.messages, "create", side_effect=[r1, r2, r3, r4]) as mc, \
         patch("agents.research_agent.drive.search_files", return_value=[]), \
         patch("agents.research_agent.gmail.search_recent_emails", return_value=[]), \
         patch.object(ra, "_run_critics", side_effect=lambda r, ctx, called, identity_claim="": r):
        ctx = ra.ToolContext(user_id="U", creds=MagicMock(), folder_id="F")
        out = ra._agent_loop("KOMSA", "", ctx)
    assert out is not None
    assert mc.call_count == 4   # 첫 submit은 nudge로 반려, drive+gmail 탐색 후 재submit 수락


def test_loop_returns_none_if_no_submit_in_budget():
    busy = SimpleNamespace(content=[_b(type="tool_use", id="t", name="web_search", input={"query": "x"})])
    with patch.object(ra._claude.messages, "create", return_value=busy), \
         patch("agents.before._search", return_value="..."), \
         patch.object(ra, "_MAX_ROUNDS", 3):
        ctx = ra.ToolContext(user_id="U1", creds=MagicMock(), folder_id="F1")
        out = ra._agent_loop("X", "", ctx)
    assert out is None


def test_loop_timeout_returns_none(monkeypatch):
    # monotonic이 start=0, 이후 1000 반환하도록 → 첫 라운드 진입 전 타임아웃 컷
    seq = iter([0, 1000])
    monkeypatch.setattr("agents.research_agent.time.monotonic", lambda: next(seq))
    monkeypatch.setattr(ra, "_TIMEOUT_S", 90)
    with patch.object(ra._claude.messages, "create") as mc:
        ctx = ra.ToolContext(user_id="U", creds=MagicMock(), folder_id="F")
        out = ra._agent_loop("X", "", ctx)
    assert out is None
    mc.assert_not_called()   # 타임아웃이 첫 라운드 진입 전 컷


def test_force_submit_on_last_round_accepts_despite_gap(monkeypatch):
    # 마지막 라운드면 커버리지 gap이어도 submit 강제 수락(타임아웃 대신 결과 산출)
    monkeypatch.setattr(ra, "_MAX_ROUNDS", 1)
    submit_in = {"summary_line": "강제수락", "company_identity_confirmed": "x"}
    r1 = SimpleNamespace(content=[_b(type="tool_use", id="s1", name="submit_research", input=submit_in)])
    with patch.object(ra._claude.messages, "create", return_value=r1) as mc, \
         patch.object(ra, "_run_critics", side_effect=lambda r, ctx, called, identity_claim="": r):
        ctx = ra.ToolContext(user_id="U", creds=MagicMock(), folder_id="F")
        out = ra._agent_loop("X", "", ctx)
    assert out is not None and out.summary_line == "강제수락"   # gap이어도 force라 수락
    _, kwargs = mc.call_args
    assert kwargs.get("tool_choice", {}).get("name") == "submit_research"   # tool_choice 강제
