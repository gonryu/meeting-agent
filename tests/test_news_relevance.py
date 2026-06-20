"""agents/news_relevance.py 단위·회귀 테스트"""
import base64
import os

os.environ.setdefault("ENCRYPTION_KEY", "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest
from unittest.mock import patch, MagicMock

with patch("anthropic.Anthropic"):
    import agents.news_relevance as nr


class TestLoaders:
    def test_load_relevance_def_has_sections(self):
        text = nr._load_relevance_def()
        assert "Positive" in text and "Negative" in text and "high 신호" in text

    def test_load_negatives_parses_bullets(self):
        negs = nr._load_negatives()
        assert "김프" in negs
        assert any("시세" in n for n in negs)
        assert "에어드롭" in negs


class TestNegativeFastCut:
    def test_drops_price_noise(self):
        txt = (
            "- 카카오, DID 기반 모바일 신분증 공공 PoC 수주 (출처)\n"
            "- 비트코인 시세 9만달러 돌파, 김프 3% (출처)\n"
            "- [마감시황] 가상자산 급등 (출처)"
        )
        out = nr._negative_fast_cut(txt)
        assert "DID 기반" in out
        assert "비트코인 시세" not in out
        assert "마감시황" not in out

    def test_keeps_info_none_line(self):
        assert "정보 없음" in nr._negative_fast_cut("- 파라메타 사업 맥락의 최근 공개 정보 없음")

    def test_ascii_negative_word_boundary(self):
        # 'airdrop'은 negative지만, 단어 일부로 우연히 든 정상 기사는 보존
        txt = "- Hairdropper 신제품 출시\n- 신규 토큰 airdrop 이벤트 진행"
        out = nr._negative_fast_cut(txt)
        assert "Hairdropper" in out         # 워드경계 — 오매칭 안 함
        assert "airdrop 이벤트" not in out   # 정확 단어 매칭 — 제거


class TestJudgeNews:
    def _items(self, mapping):
        # mapping: {index: relevance}
        return {"items": [{"i": i, "relevance": r} for i, r in mapping.items()]}

    def test_keeps_high_mid_drops_low(self):
        news = (
            "- 카카오, DID 신분증 공공 PoC 수주 (출처)\n"
            "- 카카오, RWA 토큰화 파트너십 발표 (출처)\n"
            "- 카카오, 신제품 마케팅 이벤트 (출처)"
        )
        verdict = self._items({0: "high", 1: "mid", 2: "low"})
        with patch.object(nr, "_judge_with_llm", return_value={0: "high", 1: "mid", 2: "low"}):
            out = nr.judge_news("카카오", news)
        assert "DID 신분증" in out and "[관련도: high]" in out
        assert "RWA 토큰화" in out and "[관련도: mid]" in out
        assert "마케팅 이벤트" not in out

    def test_excludes_same_name_company(self):
        news = "- (동명 타사) 카카오미용실 신규 오픈 (출처)"
        with patch.object(nr, "_judge_with_llm", return_value={0: "exclude"}):
            out = nr.judge_news("카카오", news)
        assert "정보 없음" in out

    def test_llm_failure_passes_fastcut_result(self):
        news = (
            "- 카카오, DID 신분증 공공 PoC 수주 (출처)\n"
            "- 비트코인 시세 급등 (출처)"
        )
        with patch.object(nr, "_judge_with_llm", side_effect=RuntimeError("LLM down")):
            out = nr.judge_news("카카오", news)
        # fast-cut으로 시세는 제거되고, 판정 실패라 나머지는 보존(정보 없음 강제 X)
        assert "DID 신분증" in out
        assert "비트코인 시세" not in out

    def test_empty_input(self):
        assert "정보 없음" in nr.judge_news("카카오", "")
        assert "정보 없음" in nr.judge_news("카카오", "   ")
