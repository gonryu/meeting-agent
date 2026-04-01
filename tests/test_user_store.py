"""store/user_store.py 단위 테스트 — minutes_folder_id 컬럼 포함"""
import os
import tempfile
import pytest

os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")

import store.user_store as user_store


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """각 테스트마다 임시 DB 사용"""
    db_path = str(tmp_path / "test_users.db")
    monkeypatch.setattr(user_store, "_DB_PATH", db_path)
    user_store.init_db()


_TOKEN = {
    "token": "tok",
    "refresh_token": "ref",
    "token_uri": "https://oauth2.googleapis.com/token",
    "client_id": "cid",
    "client_secret": "csec",
    "scopes": ["https://www.googleapis.com/auth/calendar"],
}


class TestInitDb:
    def test_minutes_folder_id_column_exists(self):
        """minutes_folder_id 컬럼이 테이블에 존재"""
        import sqlite3
        with sqlite3.connect(user_store._DB_PATH) as conn:
            cursor = conn.execute("PRAGMA table_info(users)")
            columns = [row[1] for row in cursor.fetchall()]
        assert "minutes_folder_id" in columns

    def test_init_db_idempotent(self):
        """init_db 중복 호출해도 오류 없음"""
        user_store.init_db()
        user_store.init_db()


class TestRegisterAndGet:
    def test_register_new_user(self):
        user_store.register("U001", _TOKEN)
        assert user_store.is_registered("U001")

    def test_not_registered(self):
        assert not user_store.is_registered("U999")

    def test_register_overwrites_existing(self):
        """재등록 시 토큰 갱신 (ON CONFLICT UPDATE)"""
        user_store.register("U001", _TOKEN)
        new_token = {**_TOKEN, "token": "new_tok"}
        user_store.register("U001", new_token)
        assert user_store.is_registered("U001")  # 중복 없이 정상

    def test_get_user_returns_dict(self):
        user_store.register("U001", _TOKEN)
        user = user_store.get_user("U001")
        assert user["slack_user_id"] == "U001"

    def test_get_unregistered_raises(self):
        with pytest.raises(ValueError, match="등록되지 않은"):
            user_store.get_user("U999")


class TestUpdateDriveConfig:
    def test_update_with_minutes_folder(self):
        """minutes_folder_id 포함 업데이트"""
        user_store.register("U001", _TOKEN)
        user_store.update_drive_config(
            "U001",
            contacts_folder_id="contacts_id",
            knowledge_file_id="knowledge_id",
            minutes_folder_id="minutes_id",
        )
        user = user_store.get_user("U001")
        assert user["contacts_folder_id"] == "contacts_id"
        assert user["knowledge_file_id"] == "knowledge_id"
        assert user["minutes_folder_id"] == "minutes_id"

    def test_update_without_minutes_folder(self):
        """minutes_folder_id 없이 업데이트 (기본값 None)"""
        user_store.register("U001", _TOKEN)
        user_store.update_drive_config(
            "U001",
            contacts_folder_id="c_id",
            knowledge_file_id="k_id",
        )
        user = user_store.get_user("U001")
        assert user["minutes_folder_id"] is None


class TestUpdateMinutesFolder:
    def test_update_minutes_folder_id(self):
        """minutes_folder_id 단독 업데이트"""
        user_store.register("U001", _TOKEN)
        user_store.update_minutes_folder("U001", "new_minutes_folder_id")
        user = user_store.get_user("U001")
        assert user["minutes_folder_id"] == "new_minutes_folder_id"


class TestAllUsers:
    def test_returns_all_registered(self):
        user_store.register("U001", _TOKEN)
        user_store.register("U002", _TOKEN)
        users = user_store.all_users()
        ids = [u["slack_user_id"] for u in users]
        assert "U001" in ids
        assert "U002" in ids

    def test_empty_db(self):
        assert user_store.all_users() == []
