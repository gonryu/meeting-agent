"""server/oauth.py 단위 테스트 — state 고유성 및 콜백 처리"""
import os
os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
os.environ.setdefault("OAUTH_CALLBACK_URL", "https://test.ngrok.io/oauth/callback")

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


# Flow, Drive, user_store 모두 mock 처리 후 import
with patch("google_auth_oauthlib.flow.Flow.from_client_secrets_file"), \
     patch("store.user_store.init_db"):
    from server.oauth import app, _pending_flows, build_auth_url

client = TestClient(app)


def _clear_flows():
    _pending_flows.clear()


class TestBuildAuthUrl:
    def setup_method(self):
        _clear_flows()

    def test_state_contains_user_id(self):
        """생성된 URL의 state가 user_id를 포함"""
        mock_flow = MagicMock()
        mock_flow.authorization_url.return_value = ("https://accounts.google.com/o/oauth2/auth?state=U001-abc", None)

        with patch("server.oauth.Flow.from_client_secrets_file", return_value=mock_flow):
            url = build_auth_url("U001")

        call_kwargs = mock_flow.authorization_url.call_args[1]
        assert call_kwargs["state"].startswith("U001-")

    def test_unique_state_per_call(self):
        """같은 user_id로 두 번 호출해도 state가 다름 (retry 중복 방지)"""
        mock_flow = MagicMock()
        states = []

        def capture_state(**kwargs):
            states.append(kwargs["state"])
            return (f"https://accounts.google.com/auth?state={kwargs['state']}", None)

        mock_flow.authorization_url.side_effect = capture_state

        with patch("server.oauth.Flow.from_client_secrets_file", return_value=mock_flow):
            build_auth_url("U001")
            build_auth_url("U001")

        assert states[0] != states[1]
        assert len(_pending_flows) == 2  # 두 flow가 각각 저장됨

    def test_pending_flows_stored_by_state(self):
        """_pending_flows에 state 키로 flow 저장"""
        mock_flow = MagicMock()
        mock_flow.authorization_url.return_value = ("https://auth.url?state=U002-xyz", None)

        with patch("server.oauth.Flow.from_client_secrets_file", return_value=mock_flow):
            build_auth_url("U002")

        # U002-??? 형태의 키가 저장되어 있어야 함
        matching_keys = [k for k in _pending_flows if k.startswith("U002-")]
        assert len(matching_keys) == 1


class TestOAuthCallback:
    def setup_method(self):
        _clear_flows()

    def test_missing_code_returns_400(self):
        """code 없으면 400"""
        resp = client.get("/oauth/callback?state=U001-abc")
        assert resp.status_code == 400

    def test_missing_state_returns_400(self):
        """state 없으면 400"""
        resp = client.get("/oauth/callback?code=authcode123")
        assert resp.status_code == 400

    def test_unknown_state_returns_400(self):
        """_pending_flows에 없는 state → 400 + 안내 메시지"""
        resp = client.get("/oauth/callback?code=authcode&state=U001-unknown_session")
        assert resp.status_code == 400
        assert "재등록" in resp.text or "만료" in resp.text

    def test_valid_state_completes_oauth(self):
        """유효한 state → 토큰 저장 + Drive 셋업 트리거"""
        mock_flow = MagicMock()
        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token": "fake_token", "refresh_token": "r", "token_uri": "u", "client_id": "c", "client_secret": "s", "scopes": []}'
        mock_flow.credentials = fake_creds

        _pending_flows["U003-session1"] = mock_flow

        with patch("server.oauth.user_store") as mock_store, \
             patch("server.oauth.Thread") as mock_thread:
            resp = client.get("/oauth/callback?code=authcode123&state=U003-session1")

        assert resp.status_code == 200
        assert "완료" in resp.text
        mock_store.register.assert_called_once()
        mock_thread.assert_called_once()

    def test_state_removed_after_use(self):
        """콜백 처리 후 _pending_flows에서 state 삭제 (재사용 방지)"""
        mock_flow = MagicMock()
        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token": "t", "refresh_token": "r", "token_uri": "u", "client_id": "c", "client_secret": "s", "scopes": []}'
        mock_flow.credentials = fake_creds

        _pending_flows["U004-sess"] = mock_flow

        with patch("server.oauth.user_store"), \
             patch("server.oauth.Thread"):
            client.get("/oauth/callback?code=code&state=U004-sess")

        assert "U004-sess" not in _pending_flows

    def test_user_id_extracted_from_state(self):
        """state = 'U005-uuid' → slack_user_id='U005'로 register 호출"""
        mock_flow = MagicMock()
        fake_creds = MagicMock()
        fake_creds.to_json.return_value = '{"token": "t", "refresh_token": "r", "token_uri": "u", "client_id": "c", "client_secret": "s", "scopes": []}'
        mock_flow.credentials = fake_creds

        _pending_flows["U005-abcdef"] = mock_flow

        with patch("server.oauth.user_store") as mock_store, \
             patch("server.oauth.Thread"):
            client.get("/oauth/callback?code=code&state=U005-abcdef")

        call_args = mock_store.register.call_args[0]
        assert call_args[0] == "U005"


# ── /deploy 웹훅 (회귀: 조용한 실패 방지) ────────────────────────

class TestDeployWebhook:
    """fetch+reset로 dirty 트리 대응, pip 실패 시 재시작 중단, 시그니처 검증."""

    def _sign(self, body: bytes, secret: str) -> str:
        import hmac, hashlib
        return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    def _fake_run_factory(self, pip_rc: int = 0):
        def _run(cmd, **kw):
            m = MagicMock()
            m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            if cmd[:2] == ["git", "rev-parse"]:
                m.stdout = "abcdef1234567890"
            elif cmd[:2] == ["git", "log"]:
                m.stdout = "• fix something"
            elif cmd and cmd[0].endswith("pip"):
                m.returncode = pip_rc
                m.stderr = "pip boom" if pip_rc else ""
            return m
        return _run

    def test_uses_reset_not_pull_and_restarts(self, monkeypatch):
        monkeypatch.setenv("DEPLOY_SECRET", "s3cr3t")
        body = b'{"ref":"refs/heads/main"}'
        sig = self._sign(body, "s3cr3t")
        with patch("subprocess.run", side_effect=self._fake_run_factory()) as mrun, \
             patch("subprocess.Popen") as mpopen:
            r = client.post("/deploy", content=body,
                            headers={"X-Deploy-Signature": sig})
        assert r.status_code == 200
        cmds = [c.args[0] for c in mrun.call_args_list]
        assert ["git", "reset", "--hard", "origin/main"] in cmds   # reset 사용
        assert ["git", "pull", "origin", "main"] not in cmds       # 옛 pull 제거
        mpopen.assert_called_once()                                # 성공 시 재시작

    def test_pip_failure_blocks_restart(self, monkeypatch):
        monkeypatch.setenv("DEPLOY_SECRET", "s3cr3t")
        body = b'{}'
        sig = self._sign(body, "s3cr3t")
        with patch("subprocess.run", side_effect=self._fake_run_factory(pip_rc=1)), \
             patch("subprocess.Popen") as mpopen:
            r = client.post("/deploy", content=body,
                            headers={"X-Deploy-Signature": sig})
        assert r.status_code == 500
        mpopen.assert_not_called()                                 # 의존성 실패 → 재시작 안 함

    def test_bad_signature_rejected(self, monkeypatch):
        monkeypatch.setenv("DEPLOY_SECRET", "s3cr3t")
        with patch("subprocess.Popen") as mpopen:
            r = client.post("/deploy", content=b'{}',
                            headers={"X-Deploy-Signature": "wrong"})
        assert r.status_code == 403
        mpopen.assert_not_called()
