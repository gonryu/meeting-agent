"""tools/docs.py 단위 테스트"""
import pytest
from unittest.mock import patch, MagicMock

with patch("tools.docs._service"):
    from tools.docs import _extract_text, read_document


def _make_doc(paragraphs: list[str]) -> dict:
    """테스트용 Google Docs 구조체 생성"""
    content = []
    for para_text in paragraphs:
        content.append({
            "paragraph": {
                "elements": [
                    {"textRun": {"content": para_text}}
                ]
            }
        })
    return {"body": {"content": content}}


class TestExtractText:
    def test_single_paragraph(self):
        doc = _make_doc(["안녕하세요 미팅 트랜스크립트입니다.\n"])
        assert _extract_text(doc) == "안녕하세요 미팅 트랜스크립트입니다.\n"

    def test_multiple_paragraphs(self):
        doc = _make_doc(["첫 번째 줄\n", "두 번째 줄\n"])
        result = _extract_text(doc)
        assert "첫 번째 줄" in result
        assert "두 번째 줄" in result

    def test_empty_body(self):
        doc = {"body": {"content": []}}
        assert _extract_text(doc) == ""

    def test_no_body(self):
        assert _extract_text({}) == ""

    def test_element_without_text_run(self):
        """textRun 없는 element (예: sectionBreak) → 무시"""
        doc = {
            "body": {
                "content": [
                    {"sectionBreak": {}},
                    {"paragraph": {"elements": [{"textRun": {"content": "내용\n"}}]}},
                ]
            }
        }
        assert _extract_text(doc) == "내용\n"

    def test_multiple_runs_in_paragraph(self):
        """하나의 paragraph에 여러 textRun → 모두 이어붙임"""
        doc = {
            "body": {
                "content": [
                    {
                        "paragraph": {
                            "elements": [
                                {"textRun": {"content": "김민환: "}},
                                {"textRun": {"content": "안녕하세요\n"}},
                            ]
                        }
                    }
                ]
            }
        }
        result = _extract_text(doc)
        assert result == "김민환: 안녕하세요\n"


class TestReadDocument:
    def test_success(self):
        """정상 문서 읽기"""
        mock_svc = MagicMock()
        mock_svc.documents().get().execute.return_value = _make_doc(["트랜스크립트 내용\n"])

        with patch("tools.docs._service", return_value=mock_svc):
            result = read_document(MagicMock(), "doc_id_123")

        assert "트랜스크립트 내용" in result

    def test_api_error_raises(self):
        """API 오류 시 예외 전파"""
        mock_svc = MagicMock()
        mock_svc.documents().get().execute.side_effect = Exception("403 Forbidden")

        with patch("tools.docs._service", return_value=mock_svc):
            with pytest.raises(Exception, match="403"):
                read_document(MagicMock(), "bad_doc_id")
