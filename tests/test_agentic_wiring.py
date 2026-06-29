import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock, patch
import agents.research_orchestrator as ro
from agents.research_types import CompanyResearch


def test_flag_off_uses_legacy(monkeypatch):
    monkeypatch.delenv("AGENTIC_RESEARCH", raising=False)
    with patch("agents.research_agent.run_agentic_research") as ag, \
         patch.object(ro, "_company_industry", lambda *a, **k: {}), \
         patch.object(ro, "_company_competitors", lambda *a, **k: {"peers": []}), \
         patch.object(ro, "_company_trends", lambda *a, **k: "- 정보 없음"), \
         patch("agents.news_relevance.judge", lambda items, c: items), \
         patch.object(ro, "_company_synthesis", lambda **k: "개요"):
        out = ro.run_company_research(company_name="X")
    ag.assert_not_called()
    assert isinstance(out, CompanyResearch)


def test_flag_on_delegates_to_agent(monkeypatch):
    monkeypatch.setenv("AGENTIC_RESEARCH", "true")
    fake = CompanyResearch(company_name="KOMSA", summary_line="에이전트 결과")
    with patch("agents.research_agent.run_agentic_research", return_value=fake) as ag:
        out = ro.run_company_research(company_name="KOMSA", user_id="U1", creds=MagicMock(), allow_agent=True)
    ag.assert_called_once()
    assert out.summary_line == "에이전트 결과"


def test_flag_on_but_not_allowed_uses_legacy(monkeypatch):
    # v1 게이팅: 플래그 ON이어도 allow_agent=False(브리핑)면 에이전트 안 탐
    monkeypatch.setenv("AGENTIC_RESEARCH", "true")
    with patch("agents.research_agent.run_agentic_research") as ag, \
         patch.object(ro, "_company_industry", lambda *a, **k: {}), \
         patch.object(ro, "_company_competitors", lambda *a, **k: {"peers": []}), \
         patch.object(ro, "_company_trends", lambda *a, **k: "- 정보 없음"), \
         patch("agents.news_relevance.judge", lambda items, c: items), \
         patch.object(ro, "_company_synthesis", lambda **k: "개요"):
        out = ro.run_company_research(company_name="KOMSA", user_id="U1", creds=MagicMock())  # allow_agent 기본 False
    ag.assert_not_called()
    assert isinstance(out, CompanyResearch)


def test_flag_on_agent_fail_falls_back(monkeypatch):
    monkeypatch.setenv("AGENTIC_RESEARCH", "true")
    with patch("agents.research_agent.run_agentic_research", return_value=None), \
         patch.object(ro, "_company_industry", lambda *a, **k: {}), \
         patch.object(ro, "_company_competitors", lambda *a, **k: {"peers": []}), \
         patch.object(ro, "_company_trends", lambda *a, **k: "- 정보 없음"), \
         patch("agents.news_relevance.judge", lambda items, c: items), \
         patch.object(ro, "_company_synthesis", lambda **k: "개요"):
        out = ro.run_company_research(company_name="KOMSA", user_id="U1", creds=MagicMock(), allow_agent=True)
    assert isinstance(out, CompanyResearch) and out.company_name == "KOMSA"
