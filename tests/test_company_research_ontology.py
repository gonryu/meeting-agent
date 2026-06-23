"""업체 리서치(standalone)에 온톨로지 렌더·게이팅 주입 테스트"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")

from tools.slack_tools import build_company_research_block
import agents.before as before


class TestRenderOntology:
    def test_renders_ontology_section(self):
        onto = {"relations": [{"relation": "related-to", "title": "KCA"}],
                "documents": [{"title": "KOMSA 제안서"}]}
        blocks = build_company_research_block(
            "KOMSA", [], [], [], None, None, "", "", ontology=onto)
        text = blocks[0]["text"]["text"]
        assert "온톨로지(사내 지식)" in text
        assert "관련: KCA" in text
        assert "문서: KOMSA 제안서" in text

    def test_no_section_when_none(self):
        text = build_company_research_block("KOMSA", [], [], [])[0]["text"]["text"]
        assert "온톨로지(사내 지식)" not in text


class TestCompanyOntologyHelper:
    def test_returns_context_when_enabled(self, monkeypatch):
        monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
        import tools.ontology as ont
        monkeypatch.setattr(ont, "company_context",
                            lambda uid, c, recent=False: {
                                "seed": "entity/komsa",
                                "relations": [{"relation": "related-to", "title": "KCA"}],
                                "documents": []})
        out = before._company_ontology("U1", "KOMSA")
        assert out["relations"][0]["title"] == "KCA"

    def test_none_when_disabled(self, monkeypatch):
        monkeypatch.setattr(before, "_ontology_enabled", lambda uid: False)
        assert before._company_ontology("U1", "KOMSA") is None

    def test_none_on_error(self, monkeypatch):
        monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
        import tools.ontology as ont

        def boom(uid, c, recent=False):
            raise RuntimeError("net down")

        monkeypatch.setattr(ont, "company_context", boom)
        assert before._company_ontology("U1", "KOMSA") is None

    def test_none_when_empty(self, monkeypatch):
        monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
        import tools.ontology as ont
        monkeypatch.setattr(ont, "company_context",
                            lambda uid, c, recent=False: {
                                "seed": None, "relations": [], "documents": []})
        assert before._company_ontology("U1", "KOMSA") is None


class TestDeepBriefRender:
    def test_block_renders_deep_brief_text(self):
        from tools.slack_tools import build_company_research_block
        blocks = build_company_research_block(
            "KOMSA", [], [], [], None, None, "", "",
            ontology_brief="KOMSA 요약\n\n• 총 266억 `[출처: 제안서]`")
        text = blocks[0]["text"]["text"]
        assert "온톨로지(사내 지식)" in text and "266억" in text
