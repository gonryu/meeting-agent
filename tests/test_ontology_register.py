"""server/oauth.py — 온톨로지 등록 엔드포인트 테스트"""
import os
os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
os.environ.setdefault("OAUTH_CALLBACK_URL", "https://test.ngrok.io/oauth/callback")
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

with patch("google_auth_oauthlib.flow.Flow.from_client_secrets_file"), \
     patch("store.user_store.init_db"):
    from server.oauth import app, _pending_ontology_states, build_ontology_register_url

client = TestClient(app)
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig"


def test_build_url_stores_state():
    _pending_ontology_states.clear()
    url = build_ontology_register_url("U1")
    assert "/ontology/register?state=U1-" in url
    assert any(k.startswith("U1-") for k in _pending_ontology_states)


def test_register_form_unknown_state_400():
    assert client.get("/ontology/register?state=nope").status_code == 400


def test_save_extracts_validates_and_stores():
    _pending_ontology_states.clear()
    _pending_ontology_states["U2-x"] = "U2"
    cfg = '{"headers":{"Authorization":"Bearer %s"}}' % _JWT
    fake_oc = MagicMock(); fake_oc.__enter__ = lambda s: s; fake_oc.__exit__ = lambda *a: None
    fake_oc.validate.return_value = True
    with patch("tools.ontology.OntologyClient", return_value=fake_oc), \
         patch("server.oauth.user_store") as store:
        r = client.post("/ontology/save", json={"state": "U2-x", "config": cfg})
    assert r.json()["ok"] is True
    store.save_ontology_token.assert_called_once_with("U2", _JWT)


def test_save_rejects_config_without_token():
    _pending_ontology_states.clear()
    _pending_ontology_states["U3-x"] = "U3"
    r = client.post("/ontology/save", json={"state": "U3-x", "config": "토큰 없음"})
    assert r.json()["ok"] is False
