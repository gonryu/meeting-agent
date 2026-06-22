"""agents/before.deep_company_ontology — 게이팅·합성·폴백"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import agents.before as before


def test_returns_brief_when_enabled(monkeypatch):
    monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
    import tools.ontology as ont
    monkeypatch.setattr(ont, "company_research_sources",
                        lambda uid, c, max_docs=6: {"seed": "entity/komsa", "relations": [],
                                                    "docs": [{"title": "제안서", "summary": "266억"}]})
    import agents.ontology_synth as synth
    monkeypatch.setattr(synth, "synthesize_company_brief", lambda c, s: "KOMSA 브리핑 266억")
    assert before.deep_company_ontology("U1", "KOMSA") == "KOMSA 브리핑 266억"


def test_none_when_disabled(monkeypatch):
    monkeypatch.setattr(before, "_ontology_enabled", lambda uid: False)
    assert before.deep_company_ontology("U1", "KOMSA") is None


def test_none_on_error(monkeypatch):
    monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
    import tools.ontology as ont
    def boom(uid, c, max_docs=6): raise RuntimeError("net")
    monkeypatch.setattr(ont, "company_research_sources", boom)
    assert before.deep_company_ontology("U1", "KOMSA") is None
