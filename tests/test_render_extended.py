import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from tools.slack_tools import build_company_research_block_v2
from agents.research_types import CompanyResearch, NewsItem, SourceDoc, Attendee


def _all_text(blocks):
    return "\n".join(b["text"]["text"] for b in blocks)


def test_renders_all_sections():
    r = CompanyResearch(
        company_name="KOMSA", summary_line="홍보 용역 범위 협의",
        deal_context="6/11 RFQ→6/15 견적→6/26 확정",
        news=[NewsItem(title="전자증서", summary="블록체인 발급", url="https://x")],
        connections=["loopchain ↔ 전자증서"],
        source_docs=[SourceDoc(title="견적서.pdf", url="https://drive/x", why="견적 항목")],
        attendees=[Attendee(name="이성룡", role="국장", contact="a@d-antwort.com")],
        talking_points=["굿즈가 견적 45%"])
    text = _all_text(build_company_research_block_v2(r))
    assert "홍보 용역 범위 협의" in text
    assert "RFQ" in text
    assert "<https://x|전자증서>" in text
    assert "견적서.pdf" in text
    assert "이성룡" in text and "국장" in text
    assert "굿즈가 견적 45%" in text


def test_cold_meeting_graceful():
    r = CompanyResearch(company_name="신규업체", summary_line="첫 미팅")
    text = _all_text(build_company_research_block_v2(r))
    assert "신규업체" in text


def test_long_brief_splits_under_3000():
    from agents.research_types import CompanyResearch, NewsItem
    big = "가" * 1500
    r = CompanyResearch(company_name="KOMSA", summary_line="요약",
                        deal_context=big, talking_points=[big, big])
    blocks = build_company_research_block_v2(r)
    assert len(blocks) >= 2                      # 분할됨
    assert all(len(b["text"]["text"]) <= 3000 for b in blocks)   # 모든 블록 한도 이내
