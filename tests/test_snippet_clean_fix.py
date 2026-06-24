"""F1: _clean_snippet 단일라인 출처마커가 본문까지 삭제하던 버그 회귀"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import agents.ontology_synth as synth


class TestCleanSnippetSingleLine:
    def test_single_line_marker_keeps_body(self):
        # 실제 document_search 스니펫: 마커와 본문이 한 줄
        s = "> ⚠️ **UNCERTAIN** — confidence: 0.55 이데일리 이정훈 기자와 김종협 대표의 인터뷰, 스테이블코인 관심"
        out = synth._clean_snippet(s)
        assert "이데일리 이정훈 기자" in out      # 본문 보존
        assert "스테이블코인" in out
        assert "confidence" not in out           # 마커 제거
        assert "UNCERTAIN" not in out

    def test_multiline_marker_keeps_body(self):
        s = "> ✓ **LIKELY** — confidence: 0.70\n핵심 내용 보존"
        out = synth._clean_snippet(s)
        assert "핵심 내용 보존" in out and "confidence" not in out

    def test_no_marker_unchanged(self):
        assert synth._clean_snippet("그냥 본문") == "그냥 본문"

    def test_empty_safe(self):
        assert synth._clean_snippet("") == ""
