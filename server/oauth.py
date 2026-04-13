"""OAuth 콜백 서버 — FastAPI (Google OAuth + Trello 인증)"""
import json
import os
import logging
import uuid
from threading import Thread
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

from store import user_store

log = logging.getLogger(__name__)

app = FastAPI()

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/documents.readonly",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/meetings.space.created",
]

_COMPANY_KNOWLEDGE_TEMPLATE = """# 회사 지식 베이스

## 회사 소개
- 회사명: 파라메타 (Parametacorp) / ICONLOOP
- 주요 서비스:
  - loopchain: 엔터프라이즈 블록체인 코어 플랫폼
  - MyID: 분산신원인증(DID) 플랫폼
  - K-BTF: 공공기관용 블록체인 공동인프라 서비스

## 주요 고객 및 파트너
- 공공기관, 금융기관, 대기업

## 영업 포인트
- 국내 최고 수준의 블록체인 기술력
- 정부/공공 레퍼런스 다수 보유
- DID/신원인증 분야 선도
"""

# Slack client는 main.py에서 주입
_slack_client = None

# build_auth_url에서 생성한 Flow를 콜백까지 보관 (code verifier 유지)
# 키: "{slack_user_id}|{uuid}" — Slack retry로 중복 호출돼도 서로 덮어쓰지 않음
_pending_flows: dict[str, "Flow"] = {}

def set_slack_client(client):
    global _slack_client
    _slack_client = client


def build_auth_url(slack_user_id: str) -> str:
    """Google OAuth 인증 URL 생성. 매 호출마다 고유 state 생성."""
    state = f"{slack_user_id}|{uuid.uuid4().hex[:12]}"
    flow = Flow.from_client_secrets_file(
        "credentials.json",
        scopes=SCOPES,
        redirect_uri=os.getenv("OAUTH_CALLBACK_URL"),
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=state,
        prompt="consent",
    )
    _pending_flows[state] = flow
    log.info(f"OAuth URL 생성: state={state}")
    return auth_url


@app.get("/oauth/callback")
async def oauth_callback(request: Request):
    """Google OAuth 콜백 처리"""
    code = request.query_params.get("code")
    state = request.query_params.get("state", "")

    if not code or not state:
        return HTMLResponse("<h2>❌ 잘못된 요청입니다.</h2>", status_code=400)

    # state = "{slack_user_id}|{session_token}"
    slack_user_id = state.split("|")[0]

    try:
        flow = _pending_flows.pop(state, None)
        if not flow:
            log.warning(f"OAuth 세션 없음: state={state}, 보관 중인 세션: {list(_pending_flows.keys())}")
            return HTMLResponse(
                "<h2>❌ 인증 세션이 만료되었습니다.</h2>"
                "<p>Slack에서 <b>/재등록</b> 을 다시 입력하고 새 링크를 클릭해주세요.</p>",
                status_code=400,
            )
        flow.fetch_token(code=code)
        creds = flow.credentials

        # 토큰 저장
        token_dict = json.loads(creds.to_json())
        user_store.register(slack_user_id, token_dict)
        log.info(f"사용자 등록 완료: {slack_user_id}")

        # Drive 폴더 자동 생성 (백그라운드)
        Thread(
            target=_setup_drive_for_user,
            args=(slack_user_id, creds),
            daemon=True,
        ).start()

        return HTMLResponse("""
            <html><body style="font-family:sans-serif;text-align:center;padding:60px">
            <h2>✅ 인증이 완료되었습니다!</h2>
            <p>Slack으로 돌아가 봇을 사용해보세요.</p>
            </body></html>
        """)

    except Exception as e:
        log.error(f"OAuth 콜백 오류: {e}")
        return HTMLResponse(f"<h2>❌ 인증 실패: {e}</h2>", status_code=500)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/deploy")
async def deploy_webhook(request: Request):
    """GitHub Actions 웹훅 → git pull + 서비스 재시작"""
    import hmac
    import hashlib
    import subprocess
    from fastapi.responses import JSONResponse

    secret = os.getenv("DEPLOY_SECRET", "")
    if not secret:
        return JSONResponse({"error": "DEPLOY_SECRET not configured"}, status_code=403)

    # 시그니처 검증
    signature = request.headers.get("X-Deploy-Signature", "")
    body = await request.body()
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        log.warning("배포 웹훅: 시그니처 불일치")
        return JSONResponse({"error": "invalid signature"}, status_code=403)

    log.info("배포 웹훅 수신 — git pull + 재시작 시작")
    try:
        pull = subprocess.run(
            ["git", "pull", "origin", "main"],
            capture_output=True, text=True, timeout=30,
        )
        log.info(f"git pull: {pull.stdout.strip()}")

        pip = subprocess.run(
            [".venv/bin/pip", "install", "-r", "requirements.txt", "--quiet"],
            capture_output=True, text=True, timeout=120,
        )
        log.info(f"pip install: {pip.returncode}")

        subprocess.Popen(
            ["sudo", "systemctl", "restart", "meeting-agent"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return {"status": "deploying", "git": pull.stdout.strip()}
    except Exception as e:
        log.exception(f"배포 실패: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def _setup_drive_for_user(slack_user_id: str, creds):
    """Drive 폴더 구조 생성/확인 후 DB 업데이트 (재등록 시에도 안전하게 동작)"""
    try:
        from tools import drive as drive_tools

        # create_folder는 이미 존재하면 기존 ID 반환 → 재등록 시 폴더 재생성 없음
        root_id = drive_tools.create_folder(creds, "MeetingAgent")
        contacts_id = drive_tools.create_folder(creds, "Contacts", root_id)
        drive_tools.create_folder(creds, "Companies", contacts_id)
        drive_tools.create_folder(creds, "People", contacts_id)
        minutes_id = drive_tools.create_folder(creds, "Minutes", root_id)

        # company_knowledge.md — 없을 때만 생성
        svc = build("drive", "v3", credentials=creds)
        q = (f"name='company_knowledge.md' and '{root_id}' in parents "
             f"and trashed=false")
        existing = svc.files().list(q=q, fields="files(id)").execute().get("files", [])
        if existing:
            knowledge_file_id = existing[0]["id"]
        else:
            media = MediaInMemoryUpload(
                _COMPANY_KNOWLEDGE_TEMPLATE.encode("utf-8"), mimetype="text/plain"
            )
            metadata = {
                "name": "company_knowledge.md",
                "parents": [root_id],
                "mimeType": "text/plain",
            }
            knowledge_file_id = svc.files().create(
                body=metadata, media_body=media, fields="id"
            ).execute()["id"]

        user_store.update_drive_config(
            slack_user_id,
            contacts_folder_id=contacts_id,
            knowledge_file_id=knowledge_file_id,
            minutes_folder_id=minutes_id,
        )

        log.info(f"Drive 셋업 완료: {slack_user_id}")

        if _slack_client:
            _slack_client.chat_postMessage(
                channel=slack_user_id,
                text="✅ 인증이 완료되었습니다! `/brief` 로 오늘 미팅 브리핑을 받아보세요.",
            )

    except Exception as e:
        log.error(f"Drive 셋업 실패 ({slack_user_id}): {e}")
        if _slack_client:
            _slack_client.chat_postMessage(
                channel=slack_user_id,
                text=f"⚠️ Drive 폴더 설정 중 오류가 발생했습니다: {e}",
            )


# ── Trello 인증 ──────────────────────────────────────────────

# Trello 인증 세션 보관 (state → slack_user_id)
_pending_trello_states: dict[str, str] = {}
# 토큰 DM 입력 대기 (return_url 실패 시 폴백)
_pending_trello_token: set[str] = set()


def build_trello_auth_url(slack_user_id: str) -> str:
    """Trello 인증용 리다이렉트 URL 생성.
    Slack에서 클릭 시 우리 서버(/trello/auth)를 거쳐 Trello로 302 리다이렉트."""
    api_key = os.getenv("TRELLO_API_KEY", "")
    if not api_key:
        raise ValueError("TRELLO_API_KEY 환경변수가 설정되지 않았습니다")

    state = f"{slack_user_id}|{uuid.uuid4().hex[:12]}"
    _pending_trello_states[state] = slack_user_id
    _pending_trello_token.add(slack_user_id)

    # Slack에 보낼 URL: 우리 서버 경유 (Slack이 쿼리 파라미터를 깨뜨리는 것 방지)
    base_url = os.getenv("OAUTH_CALLBACK_URL", "").rsplit("/oauth/callback", 1)[0]
    redirect_url = f"{base_url}/trello/auth?state={state}"
    log.info(f"Trello 인증 URL 생성: state={state}")
    return redirect_url


@app.get("/trello/auth")
async def trello_auth_redirect(request: Request):
    """Slack에서 클릭 → 여기서 Trello 인증 페이지로 302 리다이렉트"""
    from fastapi.responses import RedirectResponse

    state = request.query_params.get("state", "")
    api_key = os.getenv("TRELLO_API_KEY", "")
    base_url = os.getenv("OAUTH_CALLBACK_URL", "").rsplit("/oauth/callback", 1)[0]
    return_url = f"{base_url}/trello/callback?state={state}"

    params = {
        "key": api_key,
        "name": "meeting agent",
        "scope": "read,write",
        "expiration": "never",
        "response_type": "token",
        "callback_method": "fragment",
        "return_url": return_url,
    }
    trello_url = f"https://trello.com/1/authorize?{urlencode(params)}"
    return RedirectResponse(url=trello_url)


def is_pending_trello_token(slack_user_id: str) -> bool:
    """사용자가 Trello 토큰 입력 대기 중인지 확인"""
    return slack_user_id in _pending_trello_token


def save_trello_token_from_dm(slack_user_id: str, token: str) -> bool:
    """DM으로 받은 Trello 토큰 저장. 성공 시 True."""
    _pending_trello_token.discard(slack_user_id)
    try:
        user_store.save_trello_token(slack_user_id, token)
        log.info(f"Trello 토큰 저장 완료 (DM): {slack_user_id}")
        return True
    except Exception as e:
        log.error(f"Trello 토큰 저장 실패: {e}")
        return False


@app.get("/trello/callback")
async def trello_callback(request: Request):
    """Trello 인증 콜백 — token이 URL fragment(#token=xxx)로 전달되므로
    JS가 추출하여 /trello/save 로 POST"""
    state = request.query_params.get("state", "")
    if not state or state not in _pending_trello_states:
        return HTMLResponse(
            "<h2>❌ 인증 세션이 만료되었습니다.</h2>"
            "<p>Slack에서 <b>/trello</b> 를 다시 입력해주세요.</p>",
            status_code=400,
        )

    return HTMLResponse(f"""
    <html>
    <body style="font-family:sans-serif;text-align:center;padding:60px">
        <h2>⏳ Trello 인증 처리 중...</h2>
        <p id="status">토큰을 저장하고 있습니다.</p>
        <script>
            (function() {{
                var hash = window.location.hash;
                var match = hash.match(/token=([^&]+)/);
                if (!match) {{
                    document.getElementById('status').textContent =
                        '❌ 토큰을 찾을 수 없습니다. 다시 시도해주세요.';
                    return;
                }}
                var token = match[1];
                fetch('/trello/save', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{state: '{state}', token: token}})
                }})
                .then(function(r) {{ return r.json(); }})
                .then(function(data) {{
                    if (data.ok) {{
                        document.getElementById('status').innerHTML =
                            '<span style="color:green">✅ Trello 연결이 완료되었습니다!</span>'
                            + '<br>Slack으로 돌아가세요.';
                    }} else {{
                        document.getElementById('status').textContent =
                            '❌ 저장 실패: ' + (data.error || '알 수 없는 오류');
                    }}
                }})
                .catch(function(e) {{
                    document.getElementById('status').textContent = '❌ 오류: ' + e;
                }});
            }})();
        </script>
    </body>
    </html>
    """)


class _TrelloSaveRequest(BaseModel):
    state: str
    token: str


@app.post("/trello/save")
async def trello_save(req: _TrelloSaveRequest):
    """JS에서 전송된 Trello token + state를 받아 DB에 암호화 저장"""
    slack_user_id = _pending_trello_states.pop(req.state, None)
    if not slack_user_id:
        return {"ok": False, "error": "세션 만료"}

    _pending_trello_token.discard(slack_user_id)
    try:
        user_store.save_trello_token(slack_user_id, req.token)
        log.info(f"Trello 토큰 저장 완료: {slack_user_id}")

        if _slack_client:
            _slack_client.chat_postMessage(
                channel=slack_user_id,
                text="✅ Trello 계정이 연결되었습니다! 이제 브리핑에서 Trello 카드 정보를 볼 수 있습니다.",
            )

        return {"ok": True}
    except Exception as e:
        log.error(f"Trello 토큰 저장 실패: {e}")
        return {"ok": False, "error": str(e)}
