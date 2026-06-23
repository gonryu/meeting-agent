"""tools/ontology.detect_company_in_title — 제목 엔티티 감지"""
import os
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import tools.ontology as ont


# entity_find 응답을 토큰별로 흉내내는 Fake
_DB = {
    "KISA": {"slug": "entity/kisa", "title": "KISA", "etype": "organization",
             "match_kind": "exact", "importance": 1.0, "sources_count": 262},
    "과기부": {"slug": "entity/과기부", "title": "과기부", "etype": "organization",
            "match_kind": "exact", "importance": 0.8, "sources_count": 9},
    "이데일리": {"slug": "entity/이데일리", "title": "이데일리", "etype": "organization",
             "match_kind": "exact", "importance": 0.8, "sources_count": 1},
    "이정훈기자님": {"slug": "entity/이정훈", "title": "이정훈", "etype": "person",
               "match_kind": "fuzzy", "importance": 0.6, "sources_count": 3},
    "6ixgo": {"slug": "entity/go", "title": "Go", "etype": "technology",
              "match_kind": "substring", "importance": 1.0, "sources_count": 42},
    "InfraTeam": {"slug": "entity/infrateam", "title": "InfraTeam", "etype": "organization",
                  "match_kind": "exact", "importance": 0.9, "sources_count": 50},
}


def _mk_client(monkeypatch):
    monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def call_tool(self, name, args):
            tok = args.get("name", "")
            m = _DB.get(tok)
            return {"matches": [m]} if m else {"matches": []}

    monkeypatch.setattr(ont, "OntologyClient", FakeClient)


class TestDetect:
    def test_kisa_gwagibu_picks_highest_importance(self, monkeypatch):
        _mk_client(monkeypatch)
        assert ont.detect_company_in_title("U1", "KISA, 과기부 간담회") == "KISA"

    def test_media_org(self, monkeypatch):
        _mk_client(monkeypatch)
        assert ont.detect_company_in_title("U1", "이데일리 이정훈기자님") == "이데일리"

    def test_internal_work_no_clean_match(self, monkeypatch):
        _mk_client(monkeypatch)
        # 6ixgo→Go(substring/technology), MoU/촬영/제작=stopword → None
        assert ont.detect_company_in_title("U1", "6ixgo MoU 촬영 - 백이미지 제작") is None

    def test_own_org_denylisted(self, monkeypatch):
        _mk_client(monkeypatch)
        assert ont.detect_company_in_title("U1", "InfraTeam 회의") is None

    def test_no_token_none(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: None)
        assert ont.detect_company_in_title("U1", "KISA 간담회") is None

    def test_empty_title(self, monkeypatch):
        _mk_client(monkeypatch)
        assert ont.detect_company_in_title("U1", "") is None
