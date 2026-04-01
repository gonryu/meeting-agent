"""OAuth 콜백 서버 — FastAPI"""
import json
import os
import logging
import uuid
from threading import Thread

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
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
