import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import agents.before as before
from agents.research_types import CompanyResearch


def test_pop_and_is_rich():
    before._last_research_obj[("U1", "KOMSA")] = CompanyResearch(company_name="KOMSA", summary_line="x")
    got = before.pop_last_research("U1", "KOMSA")
    assert got and before.is_rich_research(got)
    assert before.pop_last_research("U1", "KOMSA") is None   # popped


def test_is_rich_false_for_plain():
    assert before.is_rich_research(CompanyResearch(company_name="X")) is False
