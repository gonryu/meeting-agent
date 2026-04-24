"""관리자 API — JSON 엔드포인트 (HTTP Basic Auth)

프론트엔드는 별도로 관리되며 `frontend/` 디렉터리에서 로컬로 실행합니다.
백엔드는 `/admin/api/*` 경로로 JSON만 반환합니다.
"""
import logging
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from store import user_store

VALID_RESOLUTIONS = {"pending", "applied", "on_hold"}

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
    kwargs: dict = {"limit": 300}
    if filter_param == "pending":
        kwargs["notified"] = 0
    elif filter_param == "notified":
        kwargs["notified"] = 1
    elif filter_param == "unresolved":
        kwargs["resolution"] = "pending"
    elif filter_param == "applied":
        kwargs["resolution"] = "applied"
    elif filter_param == "on_hold":
        kwargs["resolution"] = "on_hold"
    else:
        filter_param = "all"
    items = user_store.list_all_feedback(**kwargs)
    return {"filter": filter_param, "items": _enrich_feedback(items)}


class _ResolutionPayload(BaseModel):
    status: str  # pending | applied | on_hold


@router.post("/feedback/{feedback_id}/resolution")
def api_feedback_resolution(
    feedback_id: int,
    payload: _ResolutionPayload,
    _: str = Depends(_require_admin),
):
    if payload.status not in VALID_RESOLUTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"허용되지 않은 status: {payload.status}",
        )
    ok = user_store.update_feedback_resolution(feedback_id, payload.status)
    if not ok:
        raise HTTPException(status_code=404, detail="피드백을 찾을 수 없습니다")
    return {"ok": True, "id": feedback_id, "status": payload.status}


# ── 프롬프트 템플릿 관리 ─────────────────────────────────────────
# prompts/templates/*.md 파일만 대상. 인라인 프롬프트(prompts/briefing.py 등)는 제외.
# 저장 시 이전 내용을 {name}.bak.{timestamp}로 같은 폴더에 보관 (.md 확장자가 아니므로
# 목록 쿼리에 잡히지 않음). 자동 삭제는 하지 않음 — 용량이 문제되면 운영자가 정리.

_PROMPTS_DIR = (
    Path(__file__).resolve().parent.parent / "prompts" / "templates"
).resolve()
_PROMPT_NAME_RE = re.compile(r"^[a-z0-9_\-]+\.md$")
_PROMPT_MAX_BYTES = 50_000  # 50KB 상한 — 실수로 바이너리 붙여넣기 방지


def _resolve_prompt_path(name: str) -> Path:
    """파일명 화이트리스트 검증 후 절대 경로 반환.
    경로 탈출·비-md·디렉터리 이탈은 400.
    """
    if not _PROMPT_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"허용되지 않는 파일명: {name}")
    target = (_PROMPTS_DIR / name).resolve()
    # 이중 안전망: resolve 후에도 _PROMPTS_DIR 하위인지 확인 (심볼릭 링크 등 대응)
    try:
        target.relative_to(_PROMPTS_DIR)
    except ValueError:
        raise HTTPException(status_code=400, detail="허용되지 않는 경로")
    return target


@router.get("/prompts")
def api_prompts_list(_: str = Depends(_require_admin)):
    """prompts/templates/ 하위 .md 파일 목록."""
    if not _PROMPTS_DIR.is_dir():
        return []
    out = []
    for p in sorted(_PROMPTS_DIR.glob("*.md")):
        if not p.is_file():
            continue
        stat = p.stat()
        out.append({
            "name": p.name,
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime)
                .isoformat(timespec="seconds"),
        })
    return out


@router.get("/prompts/{name}")
def api_prompts_get(name: str, _: str = Depends(_require_admin)):
    path = _resolve_prompt_path(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="템플릿을 찾을 수 없습니다")
    stat = path.stat()
    return {
        "name": path.name,
        "content": path.read_text(encoding="utf-8"),
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime)
            .isoformat(timespec="seconds"),
    }


class _PromptPayload(BaseModel):
    content: str


@router.put("/prompts/{name}")
def api_prompts_update(
    name: str,
    payload: _PromptPayload,
    _: str = Depends(_require_admin),
):
    path = _resolve_prompt_path(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="템플릿을 찾을 수 없습니다")
    if len(payload.content.encode("utf-8")) > _PROMPT_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"내용이 {_PROMPT_MAX_BYTES // 1000}KB를 초과합니다",
        )
    # 이전 내용 백업 — {name}.bak.{timestamp} (확장자 .md 아님 → 목록에 안 잡힘)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{path.name}.bak.{ts}"
    backup_path = _PROMPTS_DIR / backup_name
    backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    # 저장
    path.write_text(payload.content, encoding="utf-8")
    stat = path.stat()
    log.info(f"프롬프트 템플릿 갱신: {path.name} (backup: {backup_name})")
    return {
        "ok": True,
        "name": path.name,
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime)
            .isoformat(timespec="seconds"),
        "backup": backup_name,
    }
