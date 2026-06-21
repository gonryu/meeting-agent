"""store/user_store.py — ontology_token_enc 토큰 저장 테스트"""
import base64, os
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())
import pytest
import store.user_store as user_store


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(user_store, "_DB_PATH", str(tmp_path / "t.db"))
    user_store.init_db()
    user_store.register("U1", '{"token":"t","refresh_token":"r","token_uri":"u","client_id":"c","client_secret":"s","scopes":[]}')


def test_default_none():
    assert user_store.get_ontology_token("U1") is None


def test_save_get_clear_roundtrip():
    user_store.save_ontology_token("U1", "eyJabc.def.ghi")
    assert user_store.get_ontology_token("U1") == "eyJabc.def.ghi"
    user_store.clear_ontology_token("U1")
    assert user_store.get_ontology_token("U1") is None
