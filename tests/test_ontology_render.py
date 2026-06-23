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
