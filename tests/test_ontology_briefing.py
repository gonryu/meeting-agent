"""온톨로지 게이팅 + 컨텍스트 렌더링 테스트"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import patch
import agents.before as before
from tools.slack_tools import build_context_block


class TestGating:
    def test_disabled_when_not_in_beta(self, monkeypatch):
        monkeypatch.setenv("ONTOLOGY_BETA_USERS", "U_other")
        monkeypatch.setattr(before.user_store, "get_ontology_token", lambda uid: "tok")
        assert before._ontology_enabled("U1") is False

    def test_disabled_when_no_token(self, monkeypatch):
        monkeypatch.setenv("ONTOLOGY_BETA_USERS", "U1")
        monkeypatch.setattr(before.user_store, "get_ontology_token", lambda uid: None)
        assert before._ontology_enabled("U1") is False

    def test_enabled(self, monkeypatch):
        monkeypatch.setenv("ONTOLOGY_BETA_USERS", "U1,U2")
        monkeypatch.setattr(before.user_store, "get_ontology_token", lambda uid: "tok")
        assert before._ontology_enabled("U1") is True


class TestContextRender:
    def test_ontology_section_rendered(self):
        ctx = {"trello": [], "emails": [], "minutes": [], "ontology": {
            "seed": "entity/komsa",
            "relations": [{"relation": "related-to", "title": "KCA"}],
            "documents": [{"title": "KOMSA 마케팅 계획", "id": "doc/1"}]}}
        blocks = build_context_block(ctx)
        text = blocks[0]["text"]["text"]
        assert "온톨로지" in text and "KCA" in text and "KOMSA 마케팅 계획" in text

    def test_no_ontology_section_when_absent(self):
        ctx = {"trello": [], "emails": [], "minutes": []}
        text = build_context_block(ctx)[0]["text"]["text"]
        assert "온톨로지" not in text
