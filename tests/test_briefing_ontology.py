"""agents/before.briefing_ontology_summary — 게이팅·focus 추출·폴백"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import agents.before as before


def test_returns_summary_when_enabled(monkeypatch):
    monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
    import tools.ontology as ont
    captured = {}
    def fake_recent(uid, company, focus, max_docs=4):
        captured["focus"] = focus
        return {"slug": "entity/komsa", "docs": [{"title": "t", "snippet": "s", "uri": "u", "ym": "2026-06"}]}
    monkeypatch.setattr(ont, "recent_company_docs", fake_recent)
    import agents.ontology_synth as synth
    monkeypatch.setattr(synth, "synthesize_recent_situation",
                        lambda c, r: {"summary": "최근 수주 확정", "docs": [{"title": "t", "uri": "u"}]})
    out = before.briefing_ontology_summary("U1", "KOMSA", "Komsa 마케팅 진행협의_박종도대리")
    assert out["summary"] == "최근 수주 확정"
    assert captured["focus"] == "Komsa 마케팅 진행협의"   # '_' 이후 절단

def test_none_when_disabled(monkeypatch):
    monkeypatch.setattr(before, "_ontology_enabled", lambda uid: False)
    assert before.briefing_ontology_summary("U1", "KOMSA", "제목") is None

def test_none_on_error(monkeypatch):
    monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
    import tools.ontology as ont
    def boom(uid, c, f, max_docs=4): raise RuntimeError("net")
    monkeypatch.setattr(ont, "recent_company_docs", boom)
    assert before.briefing_ontology_summary("U1", "KOMSA", "제목") is None
