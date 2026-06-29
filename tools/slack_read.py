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


def channel_history(client, channel_id: str, limit: int = 30) -> list[dict]:
    """allowlist 채널의 최근 메시지. 미허용/실패 시 빈 리스트(graceful)."""
    if channel_id not in allowed_channels():
        log.info(f"slack channel_history 차단(allowlist 외): {channel_id}")
        return []
    try:
        resp = client.conversations_history(channel=channel_id, limit=limit)
        return [{"text": m.get("text", ""), "ts": m.get("ts", ""), "user": m.get("user", "")}
                for m in (resp.get("messages") or []) if m.get("text")]
    except Exception as e:
        log.warning(f"slack channel_history 실패({channel_id}): {e}")
        return []
