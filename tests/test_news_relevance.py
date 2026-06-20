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

    def test_precision_first_keeps_percent_article(self):
        # FIX2: \d+% 패턴 제거 — 정상 사업 기사를 컷하면 안 됨
        out = nr._negative_fast_cut("- 카카오, 30% 비용 절감 블록체인 솔루션 공공기관 공급")
        assert "비용 절감" in out

    def test_precision_first_keeps_whale_lab(self):
        # FIX2: bare '고래' 토큰 제거 — '고래연구소' 같은 정상 기업명 오컷 방지
        out = nr._negative_fast_cut("- 고래연구소, 블록체인 플랫폼 출시")
        assert "고래연구소" in out

    def test_still_cuts_price_quote(self):
        # 고신뢰 '시세' 패턴은 여전히 컷
        assert nr._negative_fast_cut("- 비트코인 시세 급등").strip() == ""

    def test_still_cuts_market_close_tag(self):
        # 고신뢰 [마감시황] 태그는 여전히 컷
        assert nr._negative_fast_cut("- [마감시황] 가상자산 급등").strip() == ""


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

    def test_add_tags_false_omits_relevance_suffix(self):
        # FIX1: add_tags=False면 보존 불릿에 [관련도: x] 접미사를 붙이지 않음
        news = (
            "- 카카오, DID 신분증 공공 PoC 수주 (출처)\n"
            "- 카카오, RWA 토큰화 파트너십 발표 (출처)"
        )
        with patch.object(nr, "_judge_with_llm", return_value={0: "high", 1: "mid"}):
            out = nr.judge_news("카카오", news, add_tags=False)
        assert "DID 신분증" in out and "RWA 토큰화" in out
        assert "[관련도" not in out

    def test_preserves_non_bullet_header_lines(self):
        # 방어: ### 최근 동향 같은 비불릿 헤더가 섞여도 임의 삭제하지 않음
        news = (
            "### 최근 동향\n"
            "- 카카오, DID 신분증 공공 PoC 수주 (출처)"
        )
        with patch.object(nr, "_judge_with_llm", return_value={0: "high"}):
            out = nr.judge_news("카카오", news, add_tags=False)
        assert "### 최근 동향" in out
        assert "DID 신분증" in out


class TestWiringContract:
    """research_company가 두 경로 모두 judge_news를 거치는지 (계약 회귀)."""

    def test_judge_news_signature(self):
        # before.py가 호출하는 시그니처: judge_news(company, text, today=...)
        import inspect
        sig = inspect.signature(nr.judge_news)
        params = list(sig.parameters)
        assert params[0] == "company_name"
        assert params[1] == "news_text"
        assert "today" in sig.parameters

    def test_old_filter_removed(self):
        with patch("anthropic.Anthropic"), \
             patch("tools.calendar._service"), \
             patch("tools.drive._service"), \
             patch("tools.gmail._service"):
            import agents.before as before
        assert not hasattr(before, "_filter_parameta_relevant_news")
        assert not hasattr(before, "_PARAMETA_RELEVANCE_KEYWORDS")


import json
from pathlib import Path

_GOLDEN = Path(__file__).parent / "golden" / "news_relevance.jsonl"


class TestGoldenSet:
    def _load(self):
        return [json.loads(l) for l in _GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_golden_schema(self):
        rows = self._load()
        assert len(rows) >= 16
        valid = {"high", "mid", "low", "exclude"}
        for r in rows:
            assert r["expected"]["relevance"] in valid
            assert r["company"] and r["title"]
            # eval 하네스가 description을 읽으므로(classify_stub/haiku) 필수
            assert "description" in r, f"{r.get('id')} description 누락"

    def test_fastcut_removes_obvious_noise_in_golden(self):
        """expected=low 시세/시황 골든 항목은 fast-cut으로 제거돼야(결정적)."""
        rows = self._load()
        noisy = [r for r in rows if r["id"] in ("rel-009", "rel-010")]
        for r in noisy:
            line = f"- {r['title']} ({r['company']})"
            assert nr._negative_fast_cut(line).strip() == "", f"{r['id']} fast-cut 미제거"
