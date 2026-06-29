import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from agents.research_types import CompanyResearch, SourceDoc, Attendee


def test_extended_fields_default_empty():
    r = CompanyResearch(company_name="X")
    assert r.summary_line == "" and r.deal_context == ""
    assert r.source_docs == [] and r.attendees == [] and r.talking_points == []


def test_holds_rich_payload():
    r = CompanyResearch(
        company_name="KOMSA", summary_line="홍보 용역 범위 협의",
        deal_context="6/11 RFQ→6/15 견적→6/26 확정",
        source_docs=[SourceDoc(title="견적서.pdf", url="https://drive/x", why="견적 항목")],
        attendees=[Attendee(name="이성룡", role="국장", contact="a@d-antwort.com")],
        talking_points=["굿즈가 견적 45%"],
    )
    assert r.source_docs[0].title == "견적서.pdf"
    assert r.attendees[0].contact == "a@d-antwort.com"
    assert "굿즈" in r.talking_points[0]
