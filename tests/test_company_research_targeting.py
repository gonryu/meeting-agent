"""Company research targeting guards: aliases, source URLs, internal companies."""
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")

from agents import before


class TestCompanyResearchProfile:
    def test_normalizes_command_suffix_without_breaking_samsung_research(self):
        from agents import company_profile

        assert company_profile.normalize_company_name("다날리서치") == "다날"
        assert company_profile.normalize_company_name("두나무 리서치") == "두나무"
        assert company_profile.normalize_company_name("삼성 리서치") == "삼성리서치"
        assert company_profile.normalize_company_name("komsa") == "KOMSA"

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

    def test_samsung_securities_and_komsa_have_domain_context(self):
        from agents import company_profile

        samsung = company_profile.trend_search_context("삼성증권")
        assert "STO" in samsung and "토큰증권" in samsung and "비수탁 지갑" in samsung

        komsa = company_profile.trend_search_context("komsa")
        assert "KOMSA" in komsa and "선박검사" in komsa and "전자증서" in komsa


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


class TestDirectCompanyResearchRoute:
    def test_direct_route_normalizes_company_research_commands(self):
        from agents import company_profile

        assert company_profile.try_direct_company_research_route("다날리서치") == (
            "research_company", {"company": "다날"}
        )
        assert company_profile.try_direct_company_research_route("두나무 리서치") == (
            "research_company", {"company": "두나무"}
        )
        assert company_profile.try_direct_company_research_route("komsa 리서치") == (
            "research_company", {"company": "KOMSA"}
        )


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

    def test_run_company_research_uses_assisted_items_as_trend_candidates(self, monkeypatch):
        import agents.research_orchestrator as ro
        from agents import news_relevance, research_assist

        monkeypatch.setattr(ro, "_company_industry", lambda *a, **k: {"industry": "가상자산"})
        monkeypatch.setattr(ro, "_company_competitors", lambda *a, **k: {"peers": []})
        monkeypatch.setattr(ro, "_company_trends", lambda *a, **k: "파라메타 사업 맥락의 최근 공개 정보 없음")
        monkeypatch.setattr(
            research_assist,
            "assisted_knowledge",
            lambda company: "- **[실명계좌 제휴 경쟁]**: 은행권 경쟁 심화 (https://example.com/upbit-bank)",
        )
        monkeypatch.setattr(news_relevance, "_judge_domain_keep",
                            lambda company, bullets: set(range(len(bullets))))
        monkeypatch.setattr(ro, "_company_synthesis", lambda **k: "- **산업 위치**: 가상자산 거래소")

        out = ro.run_company_research(company_name="업비트")

        assert [n.title for n in out.news] == ["실명계좌 제휴 경쟁"]
        assert out.news[0].url == "https://example.com/upbit-bank"


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


class TestConnectionQualityGuard:
    def test_low_value_apology_connections_are_filtered_to_fallback(self):
        generated = (
            "- 죄송하지만, 상대 업체의 구체적인 정보가 제공되지 않아 정확한 접점 분석이 어렵습니다.\n"
            "- 현재 제공된 자료에서:\n"
            "- 상대 업체의 산업 위치, 시장 포지션, 주요 사업 영역이 명시되지 않음"
        )
        context = "삼성증권 STO 지갑 비수탁 deFi RFP KYC AML"
        with patch.object(before, "_generate", return_value=generated):
            out = before._build_service_connections(context, "MyID loopchain K-BTF")

        assert "죄송" not in out
        assert "구체적인 정보" not in out
        assert "MyID" in out or "loopchain" in out

    def test_low_value_apology_does_not_return_when_no_fallback_exists(self):
        generated = "- 죄송하지만, 상대 업체의 구체적인 정보가 제공되지 않아 정확한 접점 분석이 어렵습니다."
        with patch.object(before, "_generate", return_value=generated):
            out = before._build_service_connections("", "MyID loopchain K-BTF")

        assert "죄송" not in out
        assert "분석 정보 없음" in out
