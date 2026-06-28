"""Google News RSS fallback for company research trends."""
import os
from io import BytesIO
from unittest.mock import patch

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")


def test_google_news_rss_parser_returns_markdown_with_urls(monkeypatch):
    from agents import news_rss

    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel>
      <item>
        <title>삼성증권, STO 지갑 RFP 추진 - 테스트뉴스</title>
        <link>https://news.google.com/rss/articles/abc</link>
        <source url="https://example.com">테스트뉴스</source>
        <pubDate>Sun, 28 Jun 2026 10:00:00 GMT</pubDate>
      </item>
    </channel></rss>
    """.encode("utf-8")

    def fake_urlopen(req, timeout=0):
        return BytesIO(xml)

    monkeypatch.setattr(news_rss, "urlopen", fake_urlopen)

    out = news_rss.search_company_news("삼성증권", max_items=2)

    assert "삼성증권, STO 지갑 RFP 추진" in out
    assert "https://news.google.com/rss/articles/abc" in out


def test_clean_title_removes_leading_news_section_tags():
    from agents.news_rss import _clean_title

    assert (
        _clean_title("[은행 이모저모] 신한은행·iM뱅크, 프로젝트 ‘판게아’ 참여 - 비즈월드", "비즈월드")
        == "신한은행·iM뱅크, 프로젝트 ‘판게아’ 참여"
    )
    assert (
        _clean_title("인사이트] '가상자산'을 잡으려는 자, '두나무'로 몰린다 - 데일리팝", "데일리팝")
        == "'가상자산'을 잡으려는 자, '두나무'로 몰린다"
    )


def test_orchestrator_uses_rss_when_web_search_has_no_url(monkeypatch):
    import agents.research_orchestrator as ro
    from agents import news_relevance, research_assist, news_rss

    monkeypatch.setattr(ro, "_company_industry", lambda *a, **k: {"industry": "증권"})
    monkeypatch.setattr(ro, "_company_competitors", lambda *a, **k: {"peers": []})
    monkeypatch.setattr(ro, "_company_trends", lambda *a, **k: "파라메타 사업 맥락의 최근 공개 정보 없음")
    monkeypatch.setattr(research_assist, "assisted_knowledge", lambda company: "")
    monkeypatch.setattr(
        news_rss,
        "search_company_news",
        lambda company: "- **[삼성증권 STO 지갑 RFP 추진]**: Google News RSS (https://example.com/sto)",
    )
    monkeypatch.setattr(news_relevance, "_judge_domain_keep",
                        lambda company, bullets: set(range(len(bullets))))
    monkeypatch.setattr(ro, "_company_synthesis", lambda **k: "- **산업 위치**: 증권사")

    out = ro.run_company_research(company_name="삼성증권")

    assert [n.title for n in out.news] == ["삼성증권 STO 지갑 RFP 추진"]
    assert out.news[0].url == "https://example.com/sto"


def test_orchestrator_keeps_targeted_rss_candidates_when_judge_drops_all(monkeypatch):
    import agents.research_orchestrator as ro
    from agents import news_relevance, research_assist, news_rss

    monkeypatch.setattr(ro, "_company_industry", lambda *a, **k: {"industry": "결제"})
    monkeypatch.setattr(ro, "_company_competitors", lambda *a, **k: {"peers": []})
    monkeypatch.setattr(ro, "_company_trends", lambda *a, **k: "파라메타 사업 맥락의 최근 공개 정보 없음")
    monkeypatch.setattr(research_assist, "assisted_knowledge", lambda company: "")
    monkeypatch.setattr(
        news_rss,
        "search_company_news",
        lambda company: "- **[페이코인 스테이블코인 결제 논의]**: Google News RSS (https://example.com/paycoin)",
    )
    monkeypatch.setattr(news_relevance, "_judge_domain_keep", lambda company, bullets: set())
    monkeypatch.setattr(ro, "_company_synthesis", lambda **k: "- **산업 위치**: 결제")

    out = ro.run_company_research(company_name="다날")

    assert [n.title for n in out.news] == ["페이코인 스테이블코인 결제 논의"]
    assert out.news[0].relevance == "mid"
