"""단계3: judge(list[NewsItem]) — 도메인 렌즈 필터, URL 구조적 보존(재작성 없음)"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")

from agents import news_relevance
from agents.research_types import NewsItem


def _items():
    return [
        NewsItem(title="N2SF 도입 본격화", summary="공공 보안체계 확산",
                 url="https://kisa.or.kr/n2sf", date="2026.06.17"),
        NewsItem(title="K-브랜드 보호 캠페인", summary="지식재산 보호 행사",
                 url="https://x.com/brand"),
        NewsItem(title="비트코인 시세 급등", summary="가상자산 시세 동향",
                 url="https://x.com/price"),
    ]


class TestJudgePreservesFields:
    def test_keeps_url_title_summary_on_kept_items(self, monkeypatch):
        # 도메인 판정: 0번만 유지
        monkeypatch.setattr(news_relevance, "_judge_domain_keep",
                            lambda company, bullets: {0})
        out = news_relevance.judge(_items(), "KISA")
        assert len(out) == 1
        assert out[0].title == "N2SF 도입 본격화"
        assert out[0].url == "https://kisa.or.kr/n2sf"      # URL 보존
        assert out[0].summary == "공공 보안체계 확산"          # 요약 보존(재작성 없음)
        assert out[0].relevance in ("high", "mid")          # 등급 필드 세팅

    def test_fast_cut_drops_price_before_llm(self, monkeypatch):
        seen = {}
        def _capture(company, bullets):
            seen["bullets"] = bullets
            return set(range(len(bullets)))   # LLM은 받은 것 전부 유지
        monkeypatch.setattr(news_relevance, "_judge_domain_keep", _capture)
        out = news_relevance.judge(_items(), "KISA")
        # 시세 항목은 fast-cut에서 LLM 도달 전 제거됨
        titles = [it.title for it in out]
        assert "비트코인 시세 급등" not in titles
        joined = " ".join(seen["bullets"])
        assert "시세" not in joined

    def test_best_effort_on_llm_failure_returns_survivors(self, monkeypatch):
        def _boom(company, bullets):
            raise RuntimeError("LLM down")
        monkeypatch.setattr(news_relevance, "_judge_domain_keep", _boom)
        out = news_relevance.judge(_items(), "KISA")
        # LLM 실패 → fast-cut 생존분(시세 제외) 통과, URL 보존
        assert any(it.url == "https://kisa.or.kr/n2sf" for it in out)
        assert all(it.title != "비트코인 시세 급등" for it in out)

    def test_empty_returns_empty(self):
        assert news_relevance.judge([], "KISA") == []
