"""tools/drive.py — Minutes/Transcript 관련 신규 함수 단위 테스트"""
import pytest
from unittest.mock import patch, MagicMock, call
from datetime import datetime, timezone

with patch("tools.drive._service"):
    from tools.drive import find_meet_transcript, save_minutes, list_minutes


def _mock_svc():
    return MagicMock()


class TestFindMeetTranscript:
    def _patch(self, svc):
        return patch("tools.drive._service", return_value=svc)

    def test_transcript_found(self):
        """Meet Recordings → 회의 폴더 → Transcript 파일 순으로 탐색 성공"""
        svc = _mock_svc()
        transcript_file = {
            "id": "transcript_doc_id",
            "name": "카카오 미팅 - Transcript",
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-03-25T14:00:00Z",
        }

        # 1) Meet Recordings 폴더 조회
        # 2) 회의명 서브폴더 조회 (NFD 시도)
        # 3) Transcript 파일 조회
        svc.files().list().execute.side_effect = [
            {"files": [{"id": "recordings_folder_id", "name": "Meet Recordings"}]},  # 루트 폴더
            {"files": [{"id": "meeting_folder_id", "name": "카카오 미팅"}]},           # 서브폴더 NFD
            {"files": [transcript_file]},                                               # Transcript
        ]

        with self._patch(svc):
            result = find_meet_transcript(MagicMock(), "카카오 미팅")

        assert result is not None
        assert result["id"] == "transcript_doc_id"

    def test_no_recordings_folder(self):
        """Meet Recordings 폴더 없으면 None 반환"""
        svc = _mock_svc()
        svc.files().list().execute.return_value = {"files": []}

        with self._patch(svc):
            result = find_meet_transcript(MagicMock(), "카카오 미팅")

        assert result is None

    def test_no_transcript_file(self):
        """서브폴더는 있지만 Transcript 없으면 None"""
        svc = _mock_svc()
        svc.files().list().execute.side_effect = [
            {"files": [{"id": "recordings_id"}]},
            {"files": [{"id": "meeting_folder_id"}]},
            {"files": []},  # Transcript 없음
        ]

        with self._patch(svc):
            result = find_meet_transcript(MagicMock(), "카카오 미팅")

        assert result is None

    def test_returns_most_recent_transcript(self):
        """여러 Transcript 파일 중 가장 최근 것 반환"""
        svc = _mock_svc()
        files = [
            {"id": "new_id", "name": "Transcript (2)", "mimeType": "application/vnd.google-apps.document", "modifiedTime": "2026-03-25T15:00:00Z"},
            {"id": "old_id", "name": "Transcript (1)", "mimeType": "application/vnd.google-apps.document", "modifiedTime": "2026-03-25T10:00:00Z"},
        ]
        svc.files().list().execute.side_effect = [
            {"files": [{"id": "recordings_id"}]},
            {"files": [{"id": "meeting_folder_id"}]},
            {"files": files},
        ]

        with self._patch(svc):
            result = find_meet_transcript(MagicMock(), "미팅")

        assert result["id"] == "new_id"


class TestSaveMinutes:
    def test_creates_new_file(self):
        """신규 회의록 파일 생성"""
        svc = _mock_svc()
        svc.files().list().execute.return_value = {"files": []}  # _find_file: 없음
        svc.files().create().execute.return_value = {"id": "new_minutes_id"}

        with patch("tools.drive._service", return_value=svc):
            file_id = save_minutes(MagicMock(), "minutes_folder_id", "2026-03-25_카카오.md", "# 회의록\n내용")

        assert file_id == "new_minutes_id"

    def test_updates_existing_file(self):
        """같은 이름 파일 이미 있으면 업데이트"""
        svc = _mock_svc()
        existing = {"id": "existing_id", "name": "2026-03-25_카카오.md", "modifiedTime": "2026-03-25T10:00:00Z"}
        svc.files().list().execute.return_value = {"files": [existing]}
        svc.files().update().execute.return_value = {}

        with patch("tools.drive._service", return_value=svc):
            file_id = save_minutes(MagicMock(), "minutes_folder_id", "2026-03-25_카카오.md", "# 수정된 내용")

        svc.files().update.assert_called()
        assert file_id == "existing_id"


class TestListMinutes:
    def test_returns_sorted_files(self):
        """회의록 목록 최신순 반환"""
        svc = _mock_svc()
        files = [
            {"id": "f1", "name": "2026-03-25_카카오.md", "modifiedTime": "2026-03-25T15:00:00Z"},
            {"id": "f2", "name": "2026-03-24_네이버.md", "modifiedTime": "2026-03-24T10:00:00Z"},
        ]
        svc.files().list().execute.return_value = {"files": files}

        with patch("tools.drive._service", return_value=svc):
            result = list_minutes(MagicMock(), "minutes_folder_id")

        assert len(result) == 2
        assert result[0]["name"] == "2026-03-25_카카오.md"

    def test_empty_folder(self):
        """폴더에 파일 없으면 빈 리스트"""
        svc = _mock_svc()
        svc.files().list().execute.return_value = {"files": []}

        with patch("tools.drive._service", return_value=svc):
            result = list_minutes(MagicMock(), "minutes_folder_id")

        assert result == []
