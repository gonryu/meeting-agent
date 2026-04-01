"""tools/gmail.py 단위 테스트 — _decode_body는 순수 함수, mock 불필요"""
import base64
import pytest
from unittest.mock import patch, MagicMock

# _service() 호출 차단
with patch("tools.gmail._service"):
    from tools.gmail import _decode_body


def _b64(text: str) -> str:
    """텍스트를 URL-safe base64 인코딩"""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("utf-8")


class TestDecodeBody:
    def test_direct_body_data(self):
        """payload.body.data 직접 디코딩"""
        payload = {"body": {"data": _b64("안녕하세요 미팅 관련 내용입니다.")}}
        result = _decode_body(payload)
        assert result == "안녕하세요 미팅 관련 내용입니다."

    def test_multipart_text_plain(self):
        """multipart 중 text/plain 파트 선택"""
        payload = {
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64("<b>HTML 내용</b>")}},
                {"mimeType": "text/plain", "body": {"data": _b64("텍스트 내용")}},
            ]
        }
        result = _decode_body(payload)
        assert result == "텍스트 내용"

    def test_html_tags_stripped(self):
        """HTML 태그 제거"""
        payload = {"body": {"data": _b64("<p><b>안녕</b>하세요</p>")}}
        result = _decode_body(payload)
        assert "<" not in result
        assert "안녕" in result
        assert "하세요" in result

    def test_truncated_to_500_chars(self):
        """500자 초과 텍스트는 잘림"""
        long_text = "가" * 600
        payload = {"body": {"data": _b64(long_text)}}
        result = _decode_body(payload)
        assert len(result) == 500

    def test_exactly_500_chars(self):
        """500자 이하는 그대로"""
        text = "나" * 500
        payload = {"body": {"data": _b64(text)}}
        result = _decode_body(payload)
        assert len(result) == 500

    def test_empty_payload(self):
        """빈 payload → 빈 문자열"""
        assert _decode_body({}) == ""

    def test_no_text_plain_in_parts(self):
        """text/plain 없는 multipart → 빈 문자열"""
        payload = {
            "parts": [
                {"mimeType": "text/html", "body": {"data": _b64("<b>HTML만</b>")}},
            ]
        }
        result = _decode_body(payload)
        assert result == ""

    def test_multipart_first_text_plain_wins(self):
        """여러 text/plain 중 첫 번째만 사용"""
        payload = {
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("첫 번째")}},
                {"mimeType": "text/plain", "body": {"data": _b64("두 번째")}},
            ]
        }
        result = _decode_body(payload)
        assert result == "첫 번째"
