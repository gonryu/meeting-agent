import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock, patch
import agents.research_agent as ra
from agents.research_types import CompanyResearch, NewsItem


def test_coverage_critic_flags_unvisited_sources():
    assert ra._coverage_gap({"web_search"}) is True
    assert ra._coverage_gap({"gmail_search", "drive_search", "web_search"}) is False


def test_url_grounding_drops_unsourced_claims():
    r = CompanyResearch(company_name="X", news=[
        NewsItem(title="근거 있음", summary="s", url="https://ok"),
        NewsItem(title="근거 없음", summary="s", url=None)])
    with patch.object(ra, "_url_grounding_keep", return_value={0}):
        out = ra._apply_url_grounding(r)
    titles = [n.title for n in out.news]
    assert "근거 있음" in titles and "근거 없음" not in titles


def test_run_critics_keeps_when_all_grounded(monkeypatch):
    r = CompanyResearch(company_name="X", summary_line="ok",
                        news=[NewsItem(title="t", summary="s", url="https://ok")])
    monkeypatch.setattr(ra, "_url_grounding_keep", lambda r: {0})
    ctx = ra.ToolContext(user_id="U", creds=MagicMock(), folder_id="F")
    out = ra._run_critics(r, ctx, called={"gmail_search", "drive_search"})
    assert out.news and out.summary_line == "ok"


def test_collect_domains():
    from agents.research_types import CompanyResearch, Attendee, SourceDoc
    r = CompanyResearch(company_name="x",
                        attendees=[Attendee(name="a", contact="lee@komsa.or.kr")],
                        source_docs=[SourceDoc(title="t", url="https://drive.google.com/x")])
    doms = ra._collect_domains(r)
    assert "komsa.or.kr" in doms and "drive.google.com" in doms


def test_identity_caveat_on_domain_mismatch(monkeypatch):
    from agents.research_types import CompanyResearch, Attendee
    r = CompanyResearch(company_name="komsa", summary_line="협의",
                        attendees=[Attendee(name="x", contact="a@komsa-ag.de")])
    monkeypatch.setattr(ra, "_identity_consistent", lambda c, claim, doms: False)
    out = ra._run_critics(r, MagicMock(), called={"gmail_search", "drive_search"},
                          identity_claim="komsa=한국해양교통안전공단")
    assert out.summary_line.startswith("⚠️")
