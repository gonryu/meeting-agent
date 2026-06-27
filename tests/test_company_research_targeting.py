"""Company research targeting guards: aliases, source URLs, internal companies."""
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")

from agents import before


class TestCompanyResearchProfile:
    def test_dunamu_search_context_includes_upbit_and_regulatory_terms(self):
        from agents import company_profile

        ctx = company_profile.trend_search_context("두나무")

        assert "업비트" in ctx
        assert "Upbit" in ctx
        assert "Dunamu" in ctx
        assert "실명계좌" in ctx
        assert "특금법" in ctx
        assert "추천 검색 질의" in ctx
        assert "업비트 실명계좌" in ctx

    def test_danal_search_context_includes_paycoin_and_stablecoin_terms(self):
        from agents import company_profile

        ctx = company_profile.trend_search_context("다날")

        assert "페이코인" in ctx
        assert "Paycoin" in ctx
        assert "스테이블코인" in ctx
        assert "온체인 KYC" in ctx
        assert "페이코인 스테이블코인" in ctx

    def test_parameta_is_internal_company(self):
        from agents import company_profile

        assert company_profile.is_internal_company("파라메타")
        assert company_profile.is_internal_company("Parameta")


class TestTrendPromptTargeting:
    def test_company_trends_injects_alias_and_domain_context(self, monkeypatch):
        import agents.research_orchestrator as ro

        seen = {}

        def fake_search(prompt, **kwargs):
            seen["prompt"] = prompt
            return "- **[x]**: y (https://example.com)"

        monkeypatch.setattr(ro, "_call_llm_with_search", fake_search)

        ro._company_trends("두나무", "2026-06-28")

        assert "동일 실체/검색 별칭" in seen["prompt"]
        assert "추천 검색 질의" in seen["prompt"]
        assert "업비트" in seen["prompt"]
        assert "디지털자산" in seen["prompt"]
        assert "URL 없는 항목은 제외" in seen["prompt"]


class TestOrchestratorSourceRequirement:
    def test_run_company_research_keeps_only_sourced_news(self, monkeypatch):
        import agents.research_orchestrator as ro
        from agents import news_relevance

        monkeypatch.setattr(ro, "_company_industry", lambda *a, **k: {"industry": "보안"})
        monkeypatch.setattr(ro, "_company_competitors", lambda *a, **k: {"peers": []})
        monkeypatch.setattr(
            ro,
            "_company_trends",
            lambda *a, **k: (
                "- **[무출처 동향]**: 요약만 있음\n"
                "- **[출처 있는 동향]**: 요약 있음 (https://example.com/news)"
            ),
        )
        monkeypatch.setattr(news_relevance, "_judge_domain_keep",
                            lambda company, bullets: set(range(len(bullets))))
        monkeypatch.setattr(ro, "_company_synthesis", lambda **k: "- **산업 위치**: 보안기관")

        out = ro.run_company_research(company_name="KISA")

        assert [n.title for n in out.news] == ["출처 있는 동향"]
        assert out.news[0].url == "https://example.com/news"


class TestInternalCompanyResearchGuard:
    def test_internal_company_skips_external_news_and_connections(self):
        with patch.object(before, "_get_creds_and_config",
                          return_value=(MagicMock(), "contacts_id", "knowledge_id")), \
             patch.object(before, "drive") as mock_drive, \
             patch.object(before, "gmail") as mock_gmail, \
             patch.object(before, "trello") as mock_trello, \
             patch.object(before, "_search") as mock_search, \
             patch.object(before, "_build_service_connections") as mock_connections:
            mock_drive.get_company_info.return_value = (None, None, False)
            mock_drive.get_company_knowledge.return_value = "서비스 정보"
            mock_drive.save_company_info.return_value = "file_123"
            mock_drive.save_source_file.return_value = "source_123"
            mock_gmail.search_recent_emails.return_value = []
            mock_trello.get_card_context.return_value = {"card": None}

            content, _ = before.research_company("UTEST", "파라메타", force=True)

        mock_search.assert_not_called()
        mock_connections.assert_not_called()
        mock_drive.save_source_file.assert_not_called()
        assert "company_type: internal" in content
        assert "자사/내부 조직은 외부 업체 동향 리서치 대상이 아닙니다" in content
        assert "자사/내부 조직으로 분류되어 파라메타 서비스 연결점 분석은 생략합니다." in content
        assert "[출처: 웹 검색" not in content
