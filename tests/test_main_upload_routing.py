"""main.py — 파일 업로드 라우팅 (F4) 단위 테스트

활성 세션 유무에 따라 add_note vs start_document_based_minutes로 분기되는지 검증.
"""
import os

os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3ODk=")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("TRELLO_API_KEY", "test")
os.environ.setdefault("TRELLO_BOARD_ID", "test")
os.environ.setdefault("FEEDBACK_CHANNEL", "C_F")

import pytest
from unittest.mock import patch, MagicMock

with patch("anthropic.Anthropic"), \
     patch("slack_bolt.App"), \
     patch("slack_bolt.adapter.socket_mode.SocketModeHandler"):
    import main
    from agents.during import _active_sessions, _pending_inputs


_USER = "UTEST"


class TestTextUploadRouting:
    def setup_method(self):
        _active_sessions.clear()
        _pending_inputs.clear()

    def _file_info(self, name="meeting.txt"):
        return {"id": "F1", "name": name, "mimetype": "text/plain"}

    def test_no_session_routes_to_document_minutes(self):
        """활성 세션·pending 없음 → F4 경로: start_document_based_minutes"""
        client = MagicMock()
        captured = {}

        def fake_thread(target, args, daemon):
            captured["target"] = target
            captured["args"] = args
            t = MagicMock()
            t.start = lambda: None
            return t

        with patch("main.text_extract.extract_text",
                   return_value="문서 본문 내용"), \
             patch("main.threading.Thread", side_effect=fake_thread), \
             patch("main.add_note") as mock_add_note, \
             patch("main.start_document_based_minutes") as mock_doc_minutes:
            main._handle_text_upload(client, _USER, self._file_info())

        # add_note는 호출 안 됨
        assert not mock_add_note.called
        # Thread target이 start_document_based_minutes
        assert captured["target"] is mock_doc_minutes
        # args: (client, user_id, filename, text)
        _, uid, fname, text = captured["args"]
        assert uid == _USER
        assert fname == "meeting.txt"
        assert text == "문서 본문 내용"

    def test_active_session_routes_to_add_note(self):
        """활성 세션 있음 → 기존 동작: add_note"""
        _active_sessions[_USER] = {
            "title": "기존 미팅", "started_at": "10:00", "notes": [],
            "event_id": None,
        }
        client = MagicMock()
        with patch("main.text_extract.extract_text", return_value="문서 내용"), \
             patch("main.add_note") as mock_add_note, \
             patch("main.start_document_based_minutes") as mock_doc_minutes:
            main._handle_text_upload(client, _USER, self._file_info())

        # add_note가 호출되고 prefix 포함
        assert mock_add_note.called
        call = mock_add_note.call_args
        assert "[문서: meeting.txt]" in call[1]["note_text"]
        assert "문서 내용" in call[1]["note_text"]
        assert call[1]["input_type"] == "document"
        # start_document_based_minutes는 호출 안 됨
        assert not mock_doc_minutes.called

    def test_pending_event_selection_routes_to_add_note(self):
        """이벤트 선택 대기 중이면 → add_note (기존 대기열 경로)"""
        _pending_inputs[_USER] = {"inputs": [], "events": []}
        client = MagicMock()
        with patch("main.text_extract.extract_text", return_value="content"), \
             patch("main.add_note") as mock_add_note, \
             patch("main.start_document_based_minutes") as mock_doc_minutes:
            main._handle_text_upload(client, _USER, self._file_info())

        assert mock_add_note.called
        assert not mock_doc_minutes.called

    def test_empty_extracted_text_aborts(self):
        """텍스트 추출 결과가 비어있으면 경고 후 중단"""
        client = MagicMock()
        with patch("main.text_extract.extract_text", return_value=""), \
             patch("main.add_note") as mock_add_note, \
             patch("main.start_document_based_minutes") as mock_doc_minutes:
            main._handle_text_upload(client, _USER, self._file_info())

        assert not mock_add_note.called
        assert not mock_doc_minutes.called
        # 경고 메시지 발송
        assert any("추출하지 못" in c[1].get("text", "")
                   for c in client.chat_postMessage.call_args_list)
