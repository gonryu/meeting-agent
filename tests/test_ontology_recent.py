"""tools/ontology.recent_company_docs — 검색 + R1 필터"""
import os
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import tools.ontology as ont


class TestRecentCompanyDocs:
    def test_filters_offcompany_keeps_connected_with_query_prepend(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")
        seen = {}

        class FakeClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def call_tool(self, name, args):
                if name == "entity_find":
                    return {"matches": [{"slug": "entity/komsa", "match_kind": "exact", "confidence": 0.95}]}
                seen["query"] = args.get("query"); seen["sort"] = args.get("sort_by")
                return {"results": [
                    {"title": "KISA KOMSA 우선협상", "snippet": "우선협상대상자 선정 완료",
                     "source_uri": "u1", "ym": "2026-06", "min_hop": 0, "matched_via_entities": ["entity/komsa"]},
                    {"title": "타사 마케팅", "snippet": "다른 회사", "source_uri": "u2",
                     "ym": "2026-06", "min_hop": 2, "matched_via_entities": ["entity/other"]},
                    {"title": "빈 스니펫", "snippet": "", "source_uri": "u3",
                     "ym": "2026-06", "min_hop": 0, "matched_via_entities": ["entity/komsa"]},
                ]}

        monkeypatch.setattr(ont, "OntologyClient", FakeClient)
        out = ont.recent_company_docs("U1", "KOMSA", "마케팅 진행협의", max_docs=4)
        titles = [d["title"] for d in out["docs"]]
        assert "KISA KOMSA 우선협상" in titles      # 연결 + 스니펫 있음
        assert "타사 마케팅" not in titles            # R1: 미연결 제거
        assert "빈 스니펫" not in titles              # snippet 없으면 제외
        assert seen["query"].startswith("KOMSA")     # 업체명 prepend
        assert seen["sort"] == "score"               # 관련도순(recent 아님)

    def test_no_token_none(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: None)
        assert ont.recent_company_docs("U1", "KOMSA", "x") is None

    def test_no_slug_empty(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")

        class FC:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def call_tool(self, name, args): return {"matches": []}

        monkeypatch.setattr(ont, "OntologyClient", FC)
        out = ont.recent_company_docs("U1", "없는업체", "x")
        assert out["slug"] is None and out["docs"] == []
