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
