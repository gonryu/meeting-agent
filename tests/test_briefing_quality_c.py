"""C: 업체 타입 분류(#1+#2) — 언론사면 뉴스 스킵 + 연결점 고정문구"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import agents.before as before


class TestClassifyCompanyType:
    def test_media(self, monkeypatch):
        monkeypatch.setattr(before, "_generate", lambda p: "media")
        assert before._classify_company_type("이데일리") == "media"

    def test_normalizes_and_strips(self, monkeypatch):
        monkeypatch.setattr(before, "_generate", lambda p: "  PROSPECT.\n")
        assert before._classify_company_type("카카오") == "prospect"

    def test_unknown_falls_to_other(self, monkeypatch):
        monkeypatch.setattr(before, "_generate", lambda p: "헛소리응답")
        assert before._classify_company_type("X") == "other"

    def test_llm_failure_returns_other(self, monkeypatch):
        def boom(p): raise RuntimeError("api")
        monkeypatch.setattr(before, "_generate", boom)
        assert before._classify_company_type("X") == "other"


class TestMediaMessage:
    def test_media_connection_message_constant(self):
        # 언론사 연결점 고정문구가 존재하고 '해당' 의미를 담음
        assert "연결점" in before._MEDIA_CONNECTION_MSG
        assert "언론" in before._MEDIA_CONNECTION_MSG
