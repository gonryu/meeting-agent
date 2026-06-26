"""단계2: 구조화 NewsItem → Slack 렌더 (정규식 우회, 제목 링크 + 썰 표시)"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")

from tools.slack_tools import build_company_research_block, _format_news_item_for_slack


class TestFormatNewsItem:
    def test_title_link_and_summary(self):
        out = _format_news_item_for_slack(
            {"title": "N2SF 도입 본격화", "summary": "KISA가 공공 확산에 예산 투입",
             "url": "https://kisa.or.kr/n"})
        assert "<https://kisa.or.kr/n|N2SF 도입 본격화>" in out
        assert "KISA가 공공 확산에 예산 투입" in out      # 썰 표시
        assert " — " in out

    def test_no_url_plain_title_summary(self):
        out = _format_news_item_for_slack(
            {"title": "제목", "summary": "요약", "url": None})
        assert out == "제목 — 요약"

    def test_summary_equal_title_no_dash(self):
        out = _format_news_item_for_slack(
            {"title": "같은말", "summary": "같은말", "url": "https://x.com"})
        assert out == "<https://x.com|같은말>"

    def test_empty_returns_blank(self):
        assert _format_news_item_for_slack({"title": "", "summary": "", "url": None}) == ""


class TestBlockStructuredPath:
    def test_renders_from_news_items(self):
        block = build_company_research_block(
            "KISA", news_lines=[], parascope_lines=[], connection_lines=["x"],
            news_items=[
                {"title": "N2SF 도입", "summary": "공공 확산 예산 투입",
                 "url": "https://kisa.or.kr/n"},
            ])
        text = block[0]["text"]["text"]
        assert "<https://kisa.or.kr/n|N2SF 도입>" in text
        assert "공공 확산 예산 투입" in text
        assert "최근 동향 정보 없음" not in text

    def test_empty_news_items_shows_no_info(self):
        block = build_company_research_block(
            "KISA", news_lines=[], parascope_lines=[], connection_lines=["x"],
            news_items=[])
        text = block[0]["text"]["text"]
        assert "최근 동향 정보 없음" in text

    def test_legacy_path_unchanged_when_news_items_none(self):
        # news_items 미지정 → 기존 news_lines 경로 (하위호환)
        block = build_company_research_block(
            "KISA",
            news_lines=["N2SF 도입 — 공공 확산 (https://kisa.or.kr/n)"],
            parascope_lines=[], connection_lines=["x"])
        text = block[0]["text"]["text"]
        assert "kisa.or.kr/n" in text
