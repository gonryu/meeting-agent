"""tools/ontology.py — person_context (인물 미팅이력) 테스트"""
import os
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import tools.ontology as ont


class TestIsMeetingTitle:
    def test_keyword(self):
        assert ont._is_meeting_title("2024-08-02 KISA 월간업무보고 회의") is True
        assert ont._is_meeting_title("komsa 간담회") is True
        assert ont._is_meeting_title("12-06 Interview (w. X)") is True

    def test_date_only(self):
        assert ont._is_meeting_title("20190109 내부") is True

    def test_non_meeting(self):
        assert ont._is_meeting_title("Brand 파트") is False
        assert ont._is_meeting_title("lib-mesh") is False


class TestPersonContext:
    def test_returns_meetings(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")

        class FakeClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def call_tool(self, name, args):
                if name == "entity_find":
                    return {"matches": [{"slug": "entity/ryu", "match_kind": "exact",
                                         "confidence": 0.95, "sources_count": 118}]}
                return {"seed": "entity/ryu", "entities": [
                    {"slug": "entity/m1", "via": "part-of", "title": "2024-08-02 KISA 월간업무보고 회의"},
                    {"slug": "entity/x", "via": "part-of", "title": "Brand 파트"},
                ]}

        monkeypatch.setattr(ont, "OntologyClient", FakeClient)
        out = ont.person_context("U1", "류혁곤")
        assert out["seed"] == "entity/ryu"
        assert out["sources_count"] == 118
        assert "2024-08-02 KISA 월간업무보고 회의" in out["meetings"]
        assert "Brand 파트" not in out["meetings"]   # 미팅 아님 제외

    def test_no_token_none(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: None)
        assert ont.person_context("U1", "류혁곤") is None

    def test_no_match_empty(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")

        class FakeClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def call_tool(self, name, args): return {"matches": []}

        monkeypatch.setattr(ont, "OntologyClient", FakeClient)
        out = ont.person_context("U1", "없는사람")
        assert out["seed"] is None and out["meetings"] == []
