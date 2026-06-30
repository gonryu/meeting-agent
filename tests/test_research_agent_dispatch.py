import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock, patch
import agents.research_agent as ra


def test_tool_specs_include_all_sources():
    names = {t["name"] for t in ra._tool_specs()}
    assert {"gmail_search", "gmail_read_thread", "drive_search", "drive_read",
            "slack_channel_history", "trello_lookup", "web_search",
            "ontology_lookup", "submit_research"} <= names


def test_dispatch_routes_to_drive_search():
    ctx = ra.ToolContext(user_id="U1", creds=MagicMock(), slack_client=MagicMock(),
                         folder_id="F1")
    with patch("agents.research_agent.drive.search_files", return_value=[{"name": "x.pdf"}]) as ms:
        out = ra._dispatch("drive_search", {"query": "KOMSA"}, ctx)
    ms.assert_called_once()
    assert "x.pdf" in out


def test_dispatch_unknown_tool_returns_error_string():
    ctx = ra.ToolContext(user_id="U1", creds=MagicMock(), slack_client=None, folder_id="")
    out = ra._dispatch("nope", {}, ctx)
    assert "unknown" in out.lower() or "알 수 없" in out


def test_ontology_doc_fetch_spec_and_dispatch():
    names = {t["name"] for t in ra._tool_specs()}
    assert "ontology_doc_fetch" in names
    ctx = ra.ToolContext(user_id="U1", creds=MagicMock(), folder_id="")
    with patch("agents.research_agent.ontology.document_fetch",
               return_value={"title": "소타텍 회의록", "summary": "휴대폰 인증 진행"}) as md:
        out = ra._dispatch("ontology_doc_fetch", {"document_id": "d1"}, ctx)
    md.assert_called_once()
    assert "소타텍 회의록" in out


def test_ontology_brief_empty_when_not_enabled(monkeypatch):
    import agents.before as before
    monkeypatch.setattr(before, "_ontology_enabled", lambda u: False)
    assert ra._ontology_brief("U1", "다날") == ""


def test_ontology_brief_formats_when_enabled(monkeypatch):
    import agents.before as before
    monkeypatch.setattr(before, "_ontology_enabled", lambda u: True)
    monkeypatch.setattr(ra.ontology, "company_context",
        lambda u, c, recent=True: {
            "relations": [{"relation": "관련", "title": "소타텍 회의"}],
            "documents": [{"id": "d1", "title": "2025-06-10 소타텍 주간보고"}]})
    brief = ra._ontology_brief("U1", "다날")
    assert "소타텍 회의" in brief and "document_id=d1" in brief


def test_initial_prompt_includes_ontology_context():
    p = ra._initial_prompt("다날", "", ontology_context="관계: 관련=소타텍")
    assert "사내 온톨로지 맥락" in p and "소타텍" in p


def test_string_fields_not_char_exploded():
    # 모델이 connections/talking_points를 문자열로 줘도 문자폭발 안 됨(블→['블'...] 방지)
    r = ra._to_company_research(
        {"summary_line": "x", "connections": "블록체인 기반 협력", "talking_points": "홍콩 리테일 출시"},
        "미래에셋")
    assert r.connections == ["블록체인 기반 협력"]
    assert r.talking_points == ["홍콩 리테일 출시"]


def test_malformed_dict_fields_skipped_not_crash():
    # news가 문자열 등 비정상이어도 크래시 없이 빈 처리(폴백 유발 방지)
    r = ra._to_company_research(
        {"summary_line": "x", "news": "그냥 문자열", "attendees": None}, "X")
    assert r.news == [] and r.attendees == []
