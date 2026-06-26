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


class TestStage1NewsBlock:
    """단계1: run_company_research 객체 직렬화가 레거시 final_md와 의미 동등."""

    def test_render_block_matches_legacy_semantics(self):
        import re
        from agents.research_types import render_company_news_block
        overview = "- **산업 위치**: 정보보호 전문기관\n- **경쟁 구도**: 공공 인증기관"
        trend_md = ("- **[N2SF 도입]**: 공공 확산 (2026.06.17, https://kisa.or.kr/n)\n"
                    "- **[BCMD 모집]**: 교육생 모집 (https://kisa.or.kr/b)")
        today = "2026-06-25"
        # 레거시 경로 재현
        legacy = overview.rstrip() + f"\n\n### 최근 동향 ({today} 기준)\n{trend_md.strip()}"
        # 신규 경로 (객체→직렬화)
        obj = CompanyResearch(company_name="KISA", overview=overview,
                              news=parse_trend_bullets(trend_md), searched_at=today)
        new = render_company_news_block(obj)
        assert "### 최근 동향 (2026-06-25 기준)" in new
        assert new.startswith("- **산업 위치**")           # 개요 보존
        # 같은 URL 집합·제목 보존
        u = lambda s: set(re.findall(r"https?://[^\s)]+", s))
        assert u(new) == u(legacy)
        assert "N2SF 도입" in new and "BCMD 모집" in new

    def test_no_news_block_renders_no_info(self):
        from agents.research_types import render_company_news_block
        obj = CompanyResearch(company_name="X", overview="개요만",
                              news=[], searched_at="2026-06-25")
        out = render_company_news_block(obj)
        assert out.startswith("개요만")
        assert "최근 공개된 정보 없음" in out

    def test_roundtrip_through_extractor_preserves_news(self):
        import agents.before as before
        from agents.research_types import render_company_news_block
        trend_md = "- **[N2SF 도입]**: 공공 확산 (https://kisa.or.kr/n)"
        obj = CompanyResearch(company_name="KISA", overview="개요",
                              news=parse_trend_bullets(trend_md), searched_at="2026-06-25")
        news_text = render_company_news_block(obj)
        wiki = ("---\ntitle: KISA\n---\n# KISA\n\n## 최근 동향\n"
                f"- last_searched: 2026-06-25\n{news_text}\n\n## 이메일 맥락\n")
        news_lines, _, _, _, _ = before._extract_company_content_sections(wiki)
        assert any("N2SF 도입" in n for n in news_lines)


class TestStage2ExtractNewsItems:
    """단계2: 위키 '### 최근 동향' 하위섹션을 단일 파서로 NewsItem 추출 (개요 불릿 배제)."""

    _WIKI = (
        "---\ntitle: KISA\n---\n# KISA\n\n## 최근 동향\n- last_searched: 2026-06-25\n"
        "- **산업 위치**: 정보보호 전문기관 (개요 불릿 — 뉴스 아님)\n"
        "### 최근 동향 (2026-06-25 기준)\n"
        "- **[N2SF 도입 본격화]**: KISA가 N2SF 공공 확산에 예산 투입 (https://kisa.or.kr/n)\n"
        "- **[BCMD 교육생 모집]**: 블록체인 인력 양성 (https://kisa.or.kr/b)\n\n"
        "## 이메일 맥락\n- 2026-06-01 | 협의\n"
    )

    def test_isolates_trend_subsection(self):
        from agents.research_types import extract_news_items
        items = extract_news_items(self._WIKI)
        titles = [n.title for n in items]
        assert "N2SF 도입 본격화" in titles
        assert "BCMD 교육생 모집" in titles
        assert not any("산업 위치" in t for t in titles)   # 개요 불릿 배제
        assert items[0].summary and items[0].url

    def test_no_trend_subsection_empty(self):
        from agents.research_types import extract_news_items
        wiki = ("# X\n\n## 최근 동향\n- last_searched: 2026-06-25\n"
                "### 최근 동향 (2026-06-25 기준)\n- 파라메타 사업 맥락의 최근 공개 정보 없음\n\n## 이메일 맥락\n")
        assert extract_news_items(wiki) == []

    def test_single_path_shape_skips_bookkeeping_and_source_tag(self):
        # 단일 경로(웹검색 폴백): ### 하위헤더 없음 + last_searched + 인라인 출처태그
        from agents.research_types import extract_news_items
        wiki = ("# 삼성증권\n\n## 최근 동향\n- last_searched: 2026-06-25\n"
                "- **[STO 플랫폼 출시]**: 토큰증권 베타 오픈 (https://x.com/sto) `[출처: 웹 검색, 2026-06-25]`\n\n"
                "## 이메일 맥락\n")
        items = extract_news_items(wiki)
        assert len(items) == 1
        assert items[0].title == "STO 플랫폼 출시"
        assert "출처" not in items[0].summary and "토큰증권 베타 오픈" in items[0].summary
        assert items[0].url == "https://x.com/sto"


class TestStructuredNewsItemsHelper:
    """단계2: 공통 헬퍼 _structured_news_items — 플래그·방어적 폴백."""

    _WIKI = ("# KISA\n\n## 최근 동향\n- last_searched: 2026-06-25\n"
             "### 최근 동향 (2026-06-25 기준)\n"
             "- **[N2SF 도입]**: 공공 확산 (https://kisa.or.kr/n)\n\n## 이메일 맥락\n")

    def test_returns_dicts_when_news_present(self, monkeypatch):
        import agents.before as before
        monkeypatch.delenv("STRUCTURED_RENDER", raising=False)
        out = before._structured_news_items(self._WIKI, news_lines=["x"], label="KISA")
        assert out is not None and out[0]["title"] == "N2SF 도입"
        assert out[0]["url"] == "https://kisa.or.kr/n"

    def test_fallback_when_structured_empty_but_legacy_found(self, monkeypatch):
        # 구조화 0건 + 레거시 비0건 → None(레거시 유지, 회귀 방지)
        import agents.before as before
        monkeypatch.delenv("STRUCTURED_RENDER", raising=False)
        wiki_no_trend = "# X\n\n## 최근 동향\n- last_searched: 2026-06-25\n\n## 이메일 맥락\n"
        out = before._structured_news_items(wiki_no_trend, news_lines=["레거시가 찾은 뉴스"], label="X")
        assert out is None

    def test_empty_list_when_both_empty(self, monkeypatch):
        # 구조화 0건 + 레거시 0건 → [] ('정보 없음' 렌더, None 아님)
        import agents.before as before
        monkeypatch.delenv("STRUCTURED_RENDER", raising=False)
        wiki_no_trend = "# X\n\n## 최근 동향\n- last_searched: 2026-06-25\n\n## 이메일 맥락\n"
        out = before._structured_news_items(wiki_no_trend, news_lines=[], label="X")
        assert out == []

    def test_flag_off_returns_none(self, monkeypatch):
        import agents.before as before
        monkeypatch.setenv("STRUCTURED_RENDER", "false")
        out = before._structured_news_items(self._WIKI, news_lines=["x"], label="KISA")
        assert out is None


class TestStage1Orchestrator:
    """단계1: run_company_research가 CompanyResearch 객체를 반환."""

    def test_returns_company_research_object(self, monkeypatch):
        import agents.research_orchestrator as ro
        monkeypatch.setattr(ro, "_company_industry", lambda *a, **k: {"industry": "보안"})
        monkeypatch.setattr(ro, "_company_competitors", lambda *a, **k: {"peers": []})
        monkeypatch.setattr(ro, "_company_trends", lambda *a, **k:
                            "- **[N2SF 도입]**: 공공 확산 (https://kisa.or.kr/n)")
        monkeypatch.setattr(ro, "_trend_relevance", lambda c, t: t)
        monkeypatch.setattr(ro, "_company_synthesis", lambda **k: "- **산업 위치**: 보안기관")
        out = ro.run_company_research(company_name="KISA")
        assert isinstance(out, CompanyResearch)
        assert out.company_name == "KISA"
        assert out.overview.startswith("- **산업 위치**")
        assert len(out.news) == 1 and out.news[0].title == "N2SF 도입"
        assert out.news[0].url == "https://kisa.or.kr/n"
