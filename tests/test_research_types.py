"""회사리서치 구조화 타입 + 단일 파서/직렬화 (스트랭글러 단계0)"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")

from agents.research_types import (
    NewsItem, CompanyResearch, parse_trend_bullets, to_markdown,
)


class TestParseTrendBullets:
    def test_title_summary_url_date(self):
        md = ("- **[2026 블록체인 밋업데이(BCMD) 교육생 모집]**: "
              "KISA가 블록체인 인력 양성을 위해 모집한다 (2026.06.23, https://www.kisa.or.kr/k)\n"
              "- **[N2SF 도입 본격화]**: N2SF 공공 확산에 예산 투입 (2026.06.17, https://www.kisa.or.kr/n)")
        items = parse_trend_bullets(md)
        assert len(items) == 2
        a = items[0]
        assert a.title == "2026 블록체인 밋업데이(BCMD) 교육생 모집"
        assert "인력 양성" in a.summary
        assert a.url == "https://www.kisa.or.kr/k"
        assert a.date == "2026.06.23"

    def test_no_info_returns_empty(self):
        assert parse_trend_bullets("- 파라메타 사업 맥락의 최근 공개 정보 없음") == []
        assert parse_trend_bullets("") == []

    def test_bullet_without_url(self):
        items = parse_trend_bullets("- **[제목만]**: 요약 내용")
        assert len(items) == 1 and items[0].url is None and items[0].title == "제목만"

    def test_plain_bullet_no_bold_title(self):
        items = parse_trend_bullets("- 그냥 제목 요약 (https://x.com)")
        assert len(items) == 1 and items[0].url == "https://x.com"


class TestToMarkdown:
    def _research(self):
        return CompanyResearch(
            company_name="KISA", company_type="normal", searched_at="2026-06-25",
            overview="- **산업 위치**: 정보보호 전문기관",
            news=[NewsItem(title="N2SF 도입 본격화", summary="N2SF 공공 확산 예산 투입",
                           url="https://www.kisa.or.kr/n", date="2026.06.17")],
            connections=["loopchain ↔ K-BTF 보안표준"],
            email_context="## 이메일 맥락\n- 2026-06-01 | 협의",
            trello_context="## Trello 맥락\n- 카드: KISA",
        )

    def test_emits_expected_sections(self):
        md = to_markdown(self._research())
        assert "## 최근 동향" in md
        assert "### 최근 동향 (2026-06-25 기준)" in md
        assert "N2SF 도입 본격화" in md and "https://www.kisa.or.kr/n" in md
        assert "## 파라메타 서비스 연결점" in md and "K-BTF 보안표준" in md
        assert "## 이메일 맥락" in md and "## Trello 맥락" in md

    def test_roundtrip_existing_extractor_recovers_news(self):
        # 전환기 호환: to_markdown 출력을 기존 추출기가 파싱해 뉴스를 복원해야 함
        import agents.before as before
        md = "---\ntitle: KISA\n---\n" + to_markdown(self._research())
        news_lines, _p, conn, _e, _u = before._extract_company_content_sections(md)
        assert any("N2SF" in n for n in news_lines)
        assert any("K-BTF 보안표준" in c for c in conn)

    def test_no_news_emits_no_info(self):
        r = CompanyResearch(company_name="X", searched_at="2026-06-25")
        md = to_markdown(r)
        assert "최근 공개된 정보 없음" in md
