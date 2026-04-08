"""tools/text_extract.py 단위 테스트"""
import os
os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")

import pytest
from unittest.mock import patch, MagicMock

from tools import text_extract


class TestIsTextDocument:
    def test_plain_text(self):
        assert text_extract.is_text_document("text/plain") is True

    def test_markdown(self):
        assert text_extract.is_text_document("text/markdown") is True

    def test_pdf(self):
        assert text_extract.is_text_document("application/pdf") is True

    def test_docx(self):
        assert text_extract.is_text_document(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ) is True

    def test_image_rejected(self):
        assert text_extract.is_text_document("image/png") is False

    def test_audio_rejected(self):
        assert text_extract.is_text_document("audio/mpeg") is False

    def test_mime_with_charset(self):
        assert text_extract.is_text_document("text/plain; charset=utf-8") is True


class TestExtractText:
    def test_plain_text_url(self):
        """Slack plain_text URL이 있으면 그걸 사용"""
        file_info = {
            "name": "notes.txt",
            "mimetype": "text/plain",
            "plain_text": "https://files.slack.com/plain_text/notes.txt",
        }
        mock_resp = MagicMock()
        mock_resp.text = "회의 내용 텍스트"
        mock_resp.raise_for_status = MagicMock()

        with patch("tools.text_extract.requests.get", return_value=mock_resp):
            result = text_extract.extract_text(file_info, "xoxb-token")

        assert result == "회의 내용 텍스트"

    def test_direct_download_fallback(self):
        """plain_text 없으면 원본 파일 직접 다운로드"""
        file_info = {
            "name": "memo.txt",
            "mimetype": "text/plain",
            "url_private_download": "https://files.slack.com/download/memo.txt",
        }
        mock_resp = MagicMock()
        mock_resp.content = "직접 다운로드 내용".encode("utf-8")
        mock_resp.raise_for_status = MagicMock()

        with patch("tools.text_extract.requests.get", return_value=mock_resp):
            result = text_extract.extract_text(file_info, "xoxb-token")

        assert result == "직접 다운로드 내용"

    def test_preview_fallback(self):
        """다운로드도 실패하면 preview 필드 사용"""
        file_info = {
            "name": "doc.pdf",
            "mimetype": "application/pdf",
            "preview": "문서 미리보기 내용",
        }
        with patch("tools.text_extract.requests.get", side_effect=Exception("오류")):
            result = text_extract.extract_text(file_info, "xoxb-token")

        assert result == "문서 미리보기 내용"

    def test_empty_result_on_failure(self):
        """모든 방법 실패 시 빈 문자열"""
        file_info = {
            "name": "unknown.bin",
            "mimetype": "application/octet-stream",
        }
        result = text_extract.extract_text(file_info, "xoxb-token")
        assert result == ""
