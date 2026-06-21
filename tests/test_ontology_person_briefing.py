"""인물 온톨로지 주입 헬퍼 테스트 (게이팅·attach)"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import agents.before as before


class TestPersonMeetingsHelper:
    def test_attaches_when_enabled(self, monkeypatch):
        monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
        import tools.ontology as ont
        monkeypatch.setattr(ont, "person_context",
                            lambda uid, name: {"seed": "entity/x", "meetings": ["2024-01-18 정기 미팅"], "sources_count": 5})
        out = before._person_meetings("U1", "박종도")
        assert out == ["2024-01-18 정기 미팅"]

    def test_empty_when_disabled(self, monkeypatch):
        monkeypatch.setattr(before, "_ontology_enabled", lambda uid: False)
        assert before._person_meetings("U1", "박종도") == []

    def test_empty_on_error(self, monkeypatch):
        monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
        import tools.ontology as ont
        def boom(uid, name): raise RuntimeError("net down")
        monkeypatch.setattr(ont, "person_context", boom)
        assert before._person_meetings("U1", "박종도") == []
