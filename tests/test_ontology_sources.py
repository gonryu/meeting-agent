"""tools/ontology.py — company_research_sources (R1 필터 + fetch 묶음)"""
import os
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import tools.ontology as ont


class TestResearchSources:
    def test_filters_offcompany_and_fetches(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")
        calls = {"fetch": []}

        class FakeClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def call_tool(self, name, args):
                if name == "entity_find":
                    return {"matches": [{"slug": "entity/komsa", "match_kind": "exact", "confidence": 0.95}]}
                # entity_cluster
                return {"seed": "entity/komsa", "entities": [
                    {"slug": "entity/kca", "via": "related-to", "title": "KCA"}],
                    "documents": [
                        {"document_id": "d1", "title": "KOMSA 제안서", "ym": "2026-05",
                         "source_uri": "u1", "space_display": "Drive",
                         "matched_via_entities": ["entity/komsa"]},
                        {"document_id": "d_off", "title": "타사 문서", "ym": "2026-05",
                         "source_uri": "u2", "space_display": "EN",
                         "matched_via_entities": ["entity/other"]},  # 업체 미연결 → R1 제거
                    ]}

        monkeypatch.setattr(ont, "OntologyClient", FakeClient)
        monkeypatch.setattr(ont, "document_fetch",
                            lambda uid, did, **k: {"title": did, "summary": f"본문 {did}",
                                                   "uri": "u", "space": "s"} or calls["fetch"].append(did))
        out = ont.company_research_sources("U1", "KOMSA", max_docs=4)
        ids = [d["id"] for d in out["docs"]]
        assert "d1" in ids and "d_off" not in ids        # R1: 업체 연결만
        assert out["docs"][0]["summary"].startswith("본문")  # fetch 본문 채워짐
        assert out["seed"] == "entity/komsa"

    def test_no_token_none(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: None)
        assert ont.company_research_sources("U1", "KOMSA") is None
