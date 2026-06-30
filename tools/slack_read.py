"""Slack 채널 history 읽기 — allowlist된 biz 채널 한정(전역 search 아님)."""
import logging
import os

log = logging.getLogger(__name__)


def biz_channel_list() -> list[dict]:
    """SLACK_BIZ_CHANNELS 파싱. 'C1:이름,C2:이름' 또는 'C1,C2' → [{id, name}]."""
    out = []
    for tok in os.getenv("SLACK_BIZ_CHANNELS", "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" in tok:
            cid, name = tok.split(":", 1)
            out.append({"id": cid.strip(), "name": name.strip()})
        else:
            out.append({"id": tok, "name": ""})
    return out


def allowed_channels() -> set[str]:
    return {c["id"] for c in biz_channel_list()}


def _is_member(client, channel_id: str, user_id: str) -> bool:
    """요청 사용자가 채널 멤버인지(ACL 게이트). 페이지네이션. 확인 불가/실패 시 False(fail-closed: 비노출)."""
    try:
        cursor = None
        for _ in range(20):   # 대형 채널 대비 페이지네이션, 무한방지 상한
            resp = client.conversations_members(channel=channel_id, limit=200, cursor=cursor)
            if user_id in (resp.get("members") or []):
                return True
            cursor = (resp.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
        return False
    except Exception as e:
        log.warning(f"slack 멤버십 확인 실패({channel_id}): {e}")
        return False   # 안전 우선 — 확인 못 하면 노출하지 않음


def channel_history(client, channel_id: str, requesting_user_id: str = "",
                    limit: int = 30) -> list[dict]:
    """allowlist 채널의 최근 메시지. **요청 사용자가 멤버인 채널만** 반환(ACL 누수 방지).

    봇 토큰은 봇이 들어간 모든 채널을 읽을 수 있으므로, 요청자가 접근권 없는 채널 내용이
    새지 않도록 ① allowlist + ② 요청자 멤버십 2중 게이트. requesting_user_id 없으면 fail-closed."""
    if channel_id not in allowed_channels():
        log.info(f"slack channel_history 차단(allowlist 외): {channel_id}")
        return []
    if not requesting_user_id:
        log.info(f"slack channel_history 차단(요청자 미상): {channel_id}")
        return []
    if not _is_member(client, channel_id, requesting_user_id):
        log.info(f"slack channel_history 차단(요청자 비멤버): {requesting_user_id}@{channel_id}")
        return []
    try:
        resp = client.conversations_history(channel=channel_id, limit=limit)
        return [{"text": m.get("text", ""), "ts": m.get("ts", ""), "user": m.get("user", "")}
                for m in (resp.get("messages") or []) if m.get("text")]
    except Exception as e:
        log.warning(f"slack channel_history 실패({channel_id}): {e}")
        return []
