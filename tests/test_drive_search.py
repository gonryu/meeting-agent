import io, os, zipfile
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock, patch
import tools.drive as drive


def test_search_files_scopes_folder_owner_shared():
    captured = {}
    def _list(q=None, fields=None, pageSize=None, **kw):
        captured["q"] = q
        m = MagicMock(); m.execute.return_value = {"files": [
            {"id": "f1", "name": "KOMSA 견적서.pdf", "mimeType": "application/pdf"}]}
        return m
    with patch.object(drive, "_service") as msvc:
        msvc.return_value.files.return_value.list.side_effect = _list
        out = drive.search_files(MagicMock(), "KOMSA 견적", folder_id="FOLDER1")
    assert out and out[0]["name"] == "KOMSA 견적서.pdf"
    assert "FOLDER1" in captured["q"]
    assert "sharedWithMe" in captured["q"] or "'me' in owners" in captured["q"]


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
