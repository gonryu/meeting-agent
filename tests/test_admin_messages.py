"""server/admin.py — 메시지 로그 조회 API 단위 테스트"""
import base64
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ["ADMIN_PASSWORD"] = "test-admin-pw"

import pytest
from unittest.mock import patch

with patch("anthropic.Anthropic"):
    from fastapi.testclient import TestClient
    from server.oauth import app
    import store.user_store as user_store

_AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:test-admin-pw").decode()}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-pw")
    db_path = str(tmp_path / "test_admin_msg.db")
    monkeypatch.setattr(user_store, "_DB_PATH", db_path)
    user_store.init_db()
    user_store.log_message(method="post", channel="U1", recipient_user_id="U1",
                           recipient_kind="dm", text="아침 브리핑", category="briefing", ok=True)
    user_store.log_message(method="post", channel="U2", recipient_user_id="U2",
                           recipient_kind="dm", text="회의록 초안", category="minutes", ok=True)
    user_store.log_message(method="post", channel="C9", recipient_kind="channel",
                           text="발송 실패건", category="other", ok=False, error="channel_not_found")
    return TestClient(app)


class TestAuth:
    def test_requires_auth(self, client):
        assert client.get("/admin/api/messages").status_code == 401


class TestMessagesFeed:
    def test_list_all_newest_first(self, client):
        r = client.get("/admin/api/messages", headers=_AUTH)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 3
        assert items[0]["text"] == "발송 실패건"

    def test_filter_by_user(self, client):
        r = client.get("/admin/api/messages?user=U2", headers=_AUTH)
        items = r.json()["items"]
        assert len(items) == 1 and items[0]["category"] == "minutes"

    def test_filter_by_category(self, client):
        r = client.get("/admin/api/messages?category=briefing", headers=_AUTH)
        assert len(r.json()["items"]) == 1

    def test_filter_failures(self, client):
        r = client.get("/admin/api/messages?ok=0", headers=_AUTH)
        items = r.json()["items"]
        assert len(items) == 1 and items[0]["error"] == "channel_not_found"

    def test_search_text(self, client):
        r = client.get("/admin/api/messages?q=브리핑", headers=_AUTH)
        assert len(r.json()["items"]) == 1


class TestMessageDetail:
    def test_detail_ok(self, client):
        feed = client.get("/admin/api/messages", headers=_AUTH).json()["items"]
        mid = feed[0]["id"]
        r = client.get(f"/admin/api/messages/{mid}", headers=_AUTH)
        assert r.status_code == 200 and r.json()["text"] == "발송 실패건"

    def test_detail_404(self, client):
        assert client.get("/admin/api/messages/99999", headers=_AUTH).status_code == 404


class TestUserMessages:
    def test_user_messages(self, client):
        r = client.get("/admin/api/users/U1/messages", headers=_AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["user_id"] == "U1"
        assert len(body["items"]) == 1


class TestDashboardStats:
    def test_dashboard_includes_message_stats(self, client):
        r = client.get("/admin/api/dashboard", headers=_AUTH)
        assert r.status_code == 200
        stats = r.json()["message_stats"]
        assert stats["total"] == 3
        assert stats["failures"] == 1
        assert stats["by_category"]["briefing"] == 1
