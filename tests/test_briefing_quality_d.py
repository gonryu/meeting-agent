"""D: 이전 미팅 맥락에 온톨로지 미팅 문서 주입(#4)"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import agents.before as before
from tools.slack_tools import build_context_block


class TestSummaryReturnsMeetings:
    def test_meetings_filtered_from_recent_docs(self, monkeypatch):
        monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
        import tools.ontology as ont
        monkeypatch.setattr(ont, "recent_company_docs",
                            lambda uid, c, focus, max_docs=4: {"slug": "entity/이데일리", "docs": [
                                {"title": "260129 이데일리 이정훈기자 인터뷰", "snippet": "스테이블코인 관심",
                                 "uri": "https://wiki/x", "ym": "2026-01"},
                                {"title": "회사 개요", "snippet": "...", "uri": "https://wiki/y", "ym": "2026-02"},
                            ]})
        import agents.ontology_synth as synth
        monkeypatch.setattr(synth, "synthesize_recent_situation",
                            lambda c, r: {"summary": "이정훈 기자 스테이블코인 관심", "docs": [{"title": "인터뷰", "uri": "https://wiki/x"}]})
        out = before.briefing_ontology_summary("U1", "이데일리", "이데일리 이정훈기자님")
        assert out["summary"] == "이정훈 기자 스테이블코인 관심"
        # 미팅성 문서(인터뷰)만 meetings에
        titles = [m["title"] for m in out["meetings"]]
        assert "260129 이데일리 이정훈기자 인터뷰" in titles
        assert "회사 개요" not in titles
        assert out["meetings"][0]["uri"] == "https://wiki/x"


class TestContextRendersOntologyMeetings:
    def test_renders_meeting_links(self):
        ctx = {"trello": [], "emails": [], "minutes": [],
               "ontology_meetings": [{"title": "260129 이데일리 인터뷰", "uri": "https://wiki/x", "ym": "2026-01"}]}
        text = build_context_block(ctx)[0]["text"]["text"]
        assert "<https://wiki/x|260129 이데일리 인터뷰>" in text
        assert "이전 미팅 기록 없음" not in text   # 온톨로지 미팅 있으면 '없음' 미표시

    def test_no_meetings_still_shows_none(self):
        ctx = {"trello": [], "emails": [], "minutes": [], "ontology_meetings": []}
        text = build_context_block(ctx)[0]["text"]["text"]
        assert "이전 미팅 기록 없음" in text
