import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock, patch
import agents.before as before
from agents.research_types import CompanyResearch


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("AGENTIC_BRIEFING", raising=False)
    assert before._agentic_briefing_enabled() is False


def test_agentic_block_posts_when_rich(monkeypatch):
    monkeypatch.setenv("AGENTIC_BRIEFING", "true")
    rich = CompanyResearch(company_name="Acash", summary_line="필리핀 결제 제안 논의")
    meeting = {"summary": "권혁주 - Acash", "attendees": [{"email": "kwonpmkr@gmail.com"}], "description": ""}
    with patch.object(before, "_get_creds_and_config", return_value=(MagicMock(), "F", "K")), \
         patch("agents.research_agent.run_agentic_research", return_value=rich) as ag, \
         patch.object(before, "_post") as post:
        ok = before._post_agentic_company_block(MagicMock(), "U1", meeting, "", None, None)
    assert ok is True
    ag.assert_called_once()
    # 제목을 주제로 넘김(company_name 비어서)
    assert ag.call_args.kwargs.get("company_name") == "권혁주 - Acash"
    assert post.called


def test_agentic_block_falls_back_when_not_rich(monkeypatch):
    monkeypatch.setenv("AGENTIC_BRIEFING", "true")
    plain = CompanyResearch(company_name="X")   # rich 아님
    meeting = {"summary": "주간회의", "attendees": [], "description": ""}
    with patch.object(before, "_get_creds_and_config", return_value=(MagicMock(), "F", "K")), \
         patch("agents.research_agent.run_agentic_research", return_value=plain), \
         patch.object(before, "_post") as post:
        ok = before._post_agentic_company_block(MagicMock(), "U1", meeting, "", None, None)
    assert ok is False        # 빈약 → 레거시 폴백 신호
    post.assert_not_called()


def test_agentic_block_empty_subject_returns_false(monkeypatch):
    meeting = {"summary": "", "attendees": [], "description": ""}
    ok = before._post_agentic_company_block(MagicMock(), "U1", meeting, "", None, None)
    assert ok is False
