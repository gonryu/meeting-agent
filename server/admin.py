"""관리자 API — JSON 엔드포인트 (HTTP Basic Auth)

프론트엔드는 별도로 관리되며 `frontend/` 디렉터리에서 로컬로 실행합니다.
백엔드는 `/admin/api/*` 경로로 JSON만 반환합니다.
"""
import logging
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from store import user_store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/api", tags=["admin"])

# auto_error=False: Authorization 헤더 없을 때 FastAPI가 자체 401을 던지지 않음 →
# 우리가 WWW-Authenticate 헤더 없이 401을 반환해 브라우저 기본 프롬프트를 막음 (SPA가 직접 관리)
_security = HTTPBasic(auto_error=False)

# Slack client는 main.py에서 주입 (프로필 조회용)
_slack_client = None

# 프로세스 수명 캐시 (Slack users_info 결과) — 키: slack_user_id, 값: {name, email}
_profile_cache: dict[str, dict] = {}


def set_slack_client(client):
    global _slack_client
    _slack_client = client


def _lookup_profile(slack_user_id: str) -> dict:
    """Slack 프로필에서 이름·이메일 조회. 실패 시 빈 값 반환."""
    cached = _profile_cache.get(slack_user_id)
    if cached is not None:
        return cached
    result = {"name": "", "email": ""}
    if _slack_client:
        try:
            info = _slack_client.users_info(user=slack_user_id)
            profile = info.get("user", {}).get("profile", {}) or {}
            result["name"] = (
                profile.get("display_name")
                or profile.get("real_name")
                or ""
            ).strip()
            result["email"] = (profile.get("email") or "").strip()
        except Exception as e:
            log.warning(f"Slack users_info 실패 ({slack_user_id}): {e}")
    _profile_cache[slack_user_id] = result
    return result


def _require_admin(credentials: HTTPBasicCredentials | None = Depends(_security)) -> str:
    expected = os.getenv("ADMIN_PASSWORD", "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADMIN_PASSWORD 환경변수가 설정되지 않아 관리자 API가 비활성화되었습니다.",
        )
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증 필요")
    ok = secrets.compare_digest(credentials.password.encode(), expected.encode())
    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="인증 실패")
    return credentials.username


def _enrich_feedback(items: list[dict]) -> list[dict]:
    """피드백 항목에 Slack 프로필 이름 주입 (캐시 사용)"""
    enriched = []
    for it in items:
        profile = _lookup_profile(it["user_id"])
        enriched.append({**it, "user_name": profile["name"]})
    return enriched


@router.get("/dashboard")
def api_dashboard(_: str = Depends(_require_admin)):
    return {
        "counts": user_store.admin_counts(),
        "recent_feedback": _enrich_feedback(user_store.list_all_feedback(limit=5)),
    }


@router.get("/users")
def api_users(_: str = Depends(_require_admin)):
    rows = user_store.all_users()
    # 민감 필드(암호화 토큰·비밀번호·JWT) 제외하고 연결 상태만 노출
    result = []
    for r in rows:
        profile = _lookup_profile(r["slack_user_id"])
        result.append({
            "slack_user_id": r["slack_user_id"],
            "name": profile["name"],
            "email": profile["email"],
            "registered_at": r.get("registered_at"),
            "last_active": r.get("last_active"),
            "briefing_hour": r.get("briefing_hour") or 9,
            "has_drive": bool(r.get("minutes_folder_id")),
            "has_trello": bool(r.get("trello_token_enc")),
            "has_dreamplus": bool(r.get("dreamplus_email")),
        })
    return result


@router.get("/feedback")
def api_feedback(request: Request, _: str = Depends(_require_admin)):
    filter_param = request.query_params.get("filter", "all")
    if filter_param == "pending":
        items = user_store.list_all_feedback(notified=0, limit=300)
    elif filter_param == "notified":
        items = user_store.list_all_feedback(notified=1, limit=300)
    else:
        filter_param = "all"
        items = user_store.list_all_feedback(limit=300)
    return {"filter": filter_param, "items": _enrich_feedback(items)}
