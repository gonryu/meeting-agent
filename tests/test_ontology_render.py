"""온톨로지 섹션 렌더 — 한국어 라벨·노이즈 필터·문서 링크"""
from tools.slack_tools import build_company_research_block, build_context_block


def _onto():
    return {"relations": [
                {"relation": "related-to", "title": "KISA 공공과제"},
                {"relation": "instance-of", "title": "01. Cluster 구성하기"},  # 노이즈
            ],
            "documents": [
                {"title": "발표자료_KOMSA.pdf", "uri": "https://drive/x"},
                {"title": "회의록", "uri": ""},
            ]}


def test_research_block_korean_label_noise_link():
    text = build_company_research_block("KOMSA", [], [], [], None, None, "", "",
                                        ontology=_onto())[0]["text"]["text"]
    assert "관련: KISA 공공과제" in text          # 한국어 라벨
    assert "01. Cluster" not in text              # 노이즈 제외
    assert "<https://drive/x|발표자료_KOMSA>" in text  # 링크 + 확장자 제거
    assert "• 문서: 회의록" in text               # uri 없으면 평문


def test_context_block_same_rules():
    text = build_context_block({"trello": [], "emails": [], "minutes": [],
                                "ontology": _onto()})[0]["text"]["text"]
    assert "관련: KISA 공공과제" in text
    assert "01. Cluster" not in text
    assert "<https://drive/x|발표자료_KOMSA>" in text


class TestBriefingProseRender:
    def test_prose_rendered_with_links(self):
        from tools.slack_tools import build_context_block
        ctx = {"trello": [], "emails": [], "minutes": [], "ontology_recent": {
            "summary": "KOMSA는 2026-06 수주 확정. 홍보예산 턴키 협의 중.",
            "docs": [{"title": "KISA KOMSA", "uri": "https://x"}]}}
        text = build_context_block(ctx)[0]["text"]["text"]
        assert "온톨로지(사내 지식)" in text
        assert "수주 확정" in text
        assert "<https://x|KISA KOMSA>" in text

    def test_prose_takes_priority_over_structured(self):
        from tools.slack_tools import build_context_block
        ctx = {"trello": [], "emails": [], "minutes": [],
               "ontology_recent": {"summary": "프로즈 요약", "docs": []},
               "ontology": {"relations": [{"relation": "related-to", "title": "KCA"}], "documents": []}}
        text = build_context_block(ctx)[0]["text"]["text"]
        assert "프로즈 요약" in text
        assert "관련: KCA" not in text   # 프로즈 있으면 구조화 미렌더
