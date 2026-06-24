"""E: 클리핑 누수(E1) + media 미팅로그(E3) — 회사 직접연결만"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import tools.ontology as ont
import agents.before as before


class TestStrictR1NoFallback:
    def test_no_company_direct_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")

        class FakeClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def call_tool(self, name, args):
                if name == "entity_find":
                    return {"matches": [{"slug": "entity/이데일리", "match_kind": "exact", "confidence": 0.95}]}
                # 전부 person(이정훈) 경유 — 이데일리 직접연결 0
                return {"seed": "entity/이데일리", "entities": [], "documents": [
                    {"document_id": "d1", "title": "20260213_파라메타, ADB 발표", "ym": "2026-02",
                     "source_uri": "u1", "matched_via_entities": ["entity/이정훈"]},
                ]}

        monkeypatch.setattr(ont, "OntologyClient", FakeClient)
        out = ont.company_research_sources("U1", "이데일리")
        assert out["docs"] == []   # 폴백 없음 — 클리핑 차단


class TestMeetingLogTitle:
    def test_keyword_required(self):
        assert ont._is_meeting_log_title("260129 이데일리 이정훈기자 인터뷰") is True
        assert ont._is_meeting_log_title("230821 [파라메타] 회의록") is True
        assert ont._is_meeting_log_title("KISA 간담회") is True
    def test_date_only_clipping_excluded(self):
        assert ont._is_meeting_log_title("20260213_파라메타, ADB 주관 채권 포럼서 발표") is False
        assert ont._is_meeting_log_title("20251218_Townhall 행사자료") is False


class TestCompanyMeetingDocs:
    def test_keeps_company_direct_meeting_only(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")

        class FakeClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def call_tool(self, name, args):
                if name == "entity_find":
                    return {"matches": [{"slug": "entity/이데일리", "match_kind": "exact", "confidence": 0.95}]}
                return {"results": [
                    {"title": "260129 이데일리 이정훈기자 인터뷰", "snippet": "스테이블코인 관심",
                     "source_uri": "u1", "ym": "2026-01", "min_hop": 0, "matched_via_entities": ["entity/이데일리"]},
                    {"title": "20260213_파라메타, ADB 발표", "snippet": "ADB 포럼",
                     "source_uri": "u2", "ym": "2026-02", "min_hop": 2, "matched_via_entities": ["entity/이정훈"]},
                ]}

        monkeypatch.setattr(ont, "OntologyClient", FakeClient)
        out = ont.company_meeting_docs("U1", "이데일리")
        titles = [d["title"] for d in out["docs"]]
        assert "260129 이데일리 이정훈기자 인터뷰" in titles
        assert "20260213_파라메타, ADB 발표" not in titles   # 클리핑(person·키워드없음) 제외


class TestDeepMediaBranch:
    def test_media_uses_meeting_docs(self, monkeypatch):
        monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
        called = {}

        def fake_meeting(uid, c, max_docs=4):
            called["meeting"] = True
            return {"slug": "x", "docs": [{"title": "인터뷰", "snippet": "s", "uri": "u", "ym": "2026-01"}]}

        def fake_deep(uid, c):
            called["deep"] = True
            return {"slug": "x", "docs": []}

        monkeypatch.setattr(ont, "company_meeting_docs", fake_meeting)
        monkeypatch.setattr(ont, "company_research_sources", fake_deep)
        import agents.ontology_synth as synth
        monkeypatch.setattr(synth, "synthesize_recent_situation", lambda c, r: {"summary": "인터뷰 맥락", "docs": []})
        monkeypatch.setattr(synth, "synthesize_company_brief", lambda c, r: "딥 브리핑")
        out = before.deep_company_ontology("U1", "이데일리", is_media=True)
        assert "meeting" in called and "deep" not in called
        assert out == "인터뷰 맥락"

    def test_nonmedia_uses_deep(self, monkeypatch):
        monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
        called = {}

        def fake_deep(uid, c):
            called["deep"] = True
            return {"slug": "x", "docs": [{"title": "제안서", "snippet": "266억", "uri": "u", "ym": "2026-05"}]}

        monkeypatch.setattr(ont, "company_research_sources", fake_deep)
        import agents.ontology_synth as synth
        monkeypatch.setattr(synth, "synthesize_company_brief", lambda c, r: "딥 브리핑")
        out = before.deep_company_ontology("U1", "KOMSA", is_media=False)
        assert "deep" in called and out == "딥 브리핑"
