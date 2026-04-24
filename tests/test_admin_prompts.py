"""server/admin.py — 프롬프트 템플릿 관리 API 단위 테스트"""
import base64
import os
from pathlib import Path

import pytest
from unittest.mock import patch

# 테스트용 환경변수 — import 전에 설정
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3ODk=")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ["ADMIN_PASSWORD"] = "test-admin-pw"

with patch("anthropic.Anthropic"):
    from fastapi.testclient import TestClient
    from server.oauth import app
    from server import admin as admin_module


_AUTH_HEADER = "Basic " + base64.b64encode(b"admin:test-admin-pw").decode()


@pytest.fixture
def tmp_prompts_dir(tmp_path, monkeypatch):
    """_PROMPTS_DIR를 임시 폴더로 교체 + 샘플 파일 2개 배치"""
    d = (tmp_path / "templates").resolve()
    d.mkdir()
    (d / "briefing_summary.md").write_text("# 브리핑 요약\n{{company}}\n", encoding="utf-8")
    (d / "minutes_internal.md").write_text("# 내부용 회의록\n{{agenda}}\n", encoding="utf-8")
    monkeypatch.setattr(admin_module, "_PROMPTS_DIR", d)
    return d


@pytest.fixture
def client(monkeypatch):
    """TestClient fixture — 다른 테스트 파일이 ADMIN_PASSWORD 환경변수를
    삭제/변경하는 경우를 대비해 매 테스트마다 명시적으로 설정.
    """
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-pw")
    return TestClient(app)


# ── 인증 ────────────────────────────────────────────────────────


class TestAuth:
    def test_list_requires_auth(self, client, tmp_prompts_dir):
        r = client.get("/admin/api/prompts")
        assert r.status_code == 401

    def test_get_requires_auth(self, client, tmp_prompts_dir):
        r = client.get("/admin/api/prompts/briefing_summary.md")
        assert r.status_code == 401

    def test_put_requires_auth(self, client, tmp_prompts_dir):
        r = client.put(
            "/admin/api/prompts/briefing_summary.md",
            json={"content": "new"},
        )
        assert r.status_code == 401

    def test_wrong_password(self, client, tmp_prompts_dir):
        r = client.get(
            "/admin/api/prompts",
            headers={"Authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()},
        )
        assert r.status_code == 401


# ── GET /prompts (목록) ───────────────────────────────────────


class TestList:
    def test_returns_md_files_sorted(self, client, tmp_prompts_dir):
        r = client.get("/admin/api/prompts", headers={"Authorization": _AUTH_HEADER})
        assert r.status_code == 200
        items = r.json()
        names = [it["name"] for it in items]
        assert names == ["briefing_summary.md", "minutes_internal.md"]
        # 각 항목에 name/size/modified_at
        for it in items:
            assert "name" in it
            assert "size" in it and it["size"] > 0
            assert "modified_at" in it

    def test_excludes_backup_files(self, client, tmp_prompts_dir):
        """.bak.{ts} 백업 파일은 목록에서 제외 (확장자가 .md 아님)"""
        (tmp_prompts_dir / "briefing_summary.md.bak.20260424_120000").write_text(
            "old content", encoding="utf-8"
        )
        r = client.get("/admin/api/prompts", headers={"Authorization": _AUTH_HEADER})
        names = [it["name"] for it in r.json()]
        assert "briefing_summary.md.bak.20260424_120000" not in names
        assert "briefing_summary.md" in names

    def test_empty_dir(self, client, tmp_path, monkeypatch):
        empty = (tmp_path / "empty").resolve()
        empty.mkdir()
        monkeypatch.setattr(admin_module, "_PROMPTS_DIR", empty)
        r = client.get("/admin/api/prompts", headers={"Authorization": _AUTH_HEADER})
        assert r.status_code == 200
        assert r.json() == []


# ── GET /prompts/{name} (단일 조회) ────────────────────────────


class TestGet:
    def test_returns_content(self, client, tmp_prompts_dir):
        r = client.get(
            "/admin/api/prompts/briefing_summary.md",
            headers={"Authorization": _AUTH_HEADER},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "briefing_summary.md"
        assert "{{company}}" in body["content"]

    def test_not_found(self, client, tmp_prompts_dir):
        r = client.get(
            "/admin/api/prompts/nonexistent.md",
            headers={"Authorization": _AUTH_HEADER},
        )
        assert r.status_code == 404

    def test_invalid_name_rejected(self, client, tmp_prompts_dir):
        """대문자·한글·비-md 확장자는 400"""
        for bad in ("Upper.md", "한글.md", "file.txt", "no_ext"):
            r = client.get(
                f"/admin/api/prompts/{bad}",
                headers={"Authorization": _AUTH_HEADER},
            )
            assert r.status_code == 400, f"{bad} 쿨 통과됨"

    def test_path_traversal_rejected(self, client, tmp_prompts_dir):
        """경로 탈출 시도 차단 (FastAPI가 정규화하지만 이중 안전망 확인)"""
        # FastAPI가 경로 정규화 후에도 _PROMPT_NAME_RE에서 걸림
        r = client.get(
            "/admin/api/prompts/..%2Fetc%2Fpasswd",
            headers={"Authorization": _AUTH_HEADER},
        )
        # 파일명에 슬래시·점이 포함되어 name regex 차단 또는 404
        assert r.status_code in (400, 404)


# ── PUT /prompts/{name} (저장) ─────────────────────────────────


class TestUpdate:
    def test_saves_new_content(self, client, tmp_prompts_dir):
        new_content = "# 새 브리핑\n{{company}}에 대한 업데이트된 요약\n"
        r = client.put(
            "/admin/api/prompts/briefing_summary.md",
            json={"content": new_content},
            headers={"Authorization": _AUTH_HEADER},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # 파일 실제 내용 확인
        saved = (tmp_prompts_dir / "briefing_summary.md").read_text(encoding="utf-8")
        assert saved == new_content

    def test_creates_backup(self, client, tmp_prompts_dir):
        """저장 전 이전 내용을 {name}.bak.{ts}로 백업"""
        original = (tmp_prompts_dir / "briefing_summary.md").read_text(encoding="utf-8")
        r = client.put(
            "/admin/api/prompts/briefing_summary.md",
            json={"content": "new stuff"},
            headers={"Authorization": _AUTH_HEADER},
        )
        assert r.status_code == 200
        backup_name = r.json()["backup"]
        assert backup_name.startswith("briefing_summary.md.bak.")
        backup_path = tmp_prompts_dir / backup_name
        assert backup_path.is_file()
        assert backup_path.read_text(encoding="utf-8") == original

    def test_not_found_returns_404(self, client, tmp_prompts_dir):
        """존재하지 않는 파일은 새로 생성하지 않고 404 — 화이트리스트 강제"""
        r = client.put(
            "/admin/api/prompts/new_file.md",
            json={"content": "whatever"},
            headers={"Authorization": _AUTH_HEADER},
        )
        assert r.status_code == 404
        # 실제로 파일이 안 만들어졌는지 확인
        assert not (tmp_prompts_dir / "new_file.md").exists()

    def test_oversized_rejected(self, client, tmp_prompts_dir):
        """50KB 초과는 413"""
        huge = "x" * 60_000
        r = client.put(
            "/admin/api/prompts/briefing_summary.md",
            json={"content": huge},
            headers={"Authorization": _AUTH_HEADER},
        )
        assert r.status_code == 413

    def test_invalid_name_rejected(self, client, tmp_prompts_dir):
        r = client.put(
            "/admin/api/prompts/Bad.md",
            json={"content": "x"},
            headers={"Authorization": _AUTH_HEADER},
        )
        assert r.status_code == 400
