import io, os, zipfile
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock, patch
import tools.drive as drive


def test_search_files_uses_fulltext_recursive_shared():
    calls = []
    def _list(q=None, fields=None, pageSize=None, **kw):
        calls.append(q)
        m = MagicMock()
        # 하위폴더 열거(첫 호출들)는 빈 결과, 최종 검색은 파일 반환
        if "mimeType='application/vnd.google-apps.folder'" in (q or ""):
            m.execute.return_value = {"files": []}
        else:
            m.execute.return_value = {"files": [
                {"id": "f1", "name": "2026-06-15_견적서_최종.pdf", "mimeType": "application/pdf"}]}
        return m
    with patch.object(drive, "_service") as msvc:
        msvc.return_value.files.return_value.list.side_effect = _list
        out = drive.search_files(MagicMock(), "디안트보르트", folder_id="FOLDER1")
    assert out and out[0]["name"].endswith(".pdf")
    final_q = calls[-1]
    assert "fullText contains '디안트보르트'" in final_q
    assert "sharedWithMe = true" in final_q
    assert "'FOLDER1' in parents" in final_q


def test_extract_hwpx_text():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Contents/section0.xml",
                   "<hml><p><run><t>총 55,040,000원</t></run></p></hml>")
    text = drive._extract_hwpx(buf.getvalue())
    assert "55,040,000" in text


def test_read_file_text_routes_by_extension():
    with patch.object(drive, "_service") as msvc, \
         patch.object(drive, "_extract_pdf", return_value="PDF본문") as mp:
        msvc.return_value.files.return_value.get_media.return_value.execute.return_value = b"%PDF.."
        out = drive.read_file_text(MagicMock(), "f1", mime_type="application/pdf", name="견적서.pdf")
    mp.assert_called_once()
    assert out == "PDF본문"
