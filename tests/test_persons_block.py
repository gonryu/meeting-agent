"""tools/slack_tools.py — build_persons_block 미팅이력 렌더 테스트"""
from tools.slack_tools import build_persons_block


def test_renders_meetings():
    blocks = build_persons_block([
        {"name": "박종도", "role": "대리",
         "meetings": ["2024-08-02 KISA 월간업무보고 회의", "2024-01-18 정기 미팅"]}])
    text = blocks[0]["text"]["text"]
    assert "박종도" in text and "대리" in text
    assert "2024-08-02 KISA 월간업무보고 회의" in text
    assert "함께한 미팅" in text


def test_no_meetings_section_when_absent():
    text = build_persons_block([{"name": "김외부"}])[0]["text"]["text"]
    assert "김외부" in text and "함께한 미팅" not in text


def test_empty_list_returns_empty():
    assert build_persons_block([]) == []
