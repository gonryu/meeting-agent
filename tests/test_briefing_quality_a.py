"""A: 업데이트체크 숨김(#3) + 온톨로지 스니펫/프롬프트 메타코멘트 차단(#6)"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock
import tools.slack_tools as st
import agents.ontology_synth as synth


class TestUpdateCheckHidden:
    def test_update_check_not_rendered(self):
        blocks = st.build_company_research_block(
            "KOMSA", [], [], [], ["2026-06-24 신규 리서치로 업체 Wiki 생성"], None, "", "")
        text = blocks[0]["text"]["text"]
        assert "업데이트 체크" not in text
        assert "신규 리서치로" not in text

    def test_other_sections_still_render(self):
        # 업데이트체크만 빠지고 나머지(연결점 등)는 유지
        blocks = st.build_company_research_block(
            "KOMSA", [], [], ["loopchain ↔ 결제"], ["업데이트 줄"], None, "", "")
        text = blocks[0]["text"]["text"]
        assert "파라메타 서비스 연결점" in text


class TestSnippetProvenanceStripped:
    def test_strips_confidence_marker(self):
        docs = [{"title": "인터뷰", "ym": "2026-01",
                 "snippet": "> ⚠️ **UNCERTAIN** — confidence: 0.55\n이정훈 기자는 스테이블코인에 관심."}]
        out = synth._fmt_snippets(docs)
        assert "confidence" not in out
        assert "UNCERTAIN" not in out
        assert "스테이블코인" in out

    def test_strips_likely_marker(self):
        docs = [{"title": "t", "ym": "", "snippet": "> ✓ **LIKELY** — confidence: 0.70\n핵심 내용"}]
        out = synth._fmt_snippets(docs)
        assert "LIKELY" not in out and "핵심 내용" in out


class TestMetaCommentGuard:
    def _docs(self):
        return {"slug": "x", "docs": [{"title": "인터뷰", "snippet": "이정훈 기자 스테이블코인 관심",
                                       "uri": "u", "ym": "2026-01"}]}

    def test_meta_comment_returns_none(self, monkeypatch):
        resp = MagicMock(); resp.content = [MagicMock(text="제공하신 스니펫이 불완전하여 향후 정리하겠습니다.")]
        monkeypatch.setattr(synth._claude.messages, "create", lambda **kw: resp)
        assert synth.synthesize_recent_situation("이데일리", self._docs()) is None

    def test_normal_summary_passes(self, monkeypatch):
        resp = MagicMock(); resp.content = [MagicMock(text="이정훈 기자는 블록체인 초기부터 취재, 최근 스테이블코인 관심.")]
        monkeypatch.setattr(synth._claude.messages, "create", lambda **kw: resp)
        out = synth.synthesize_recent_situation("이데일리", self._docs())
        assert out and "스테이블코인" in out["summary"]
