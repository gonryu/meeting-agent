"""Slack 발송 메시지 로깅 — WebClient send 메서드 in-place 래핑.

app.client(및 Bolt 리스너 주입 client)의 chat_postMessage/chat_update/
chat_postEphemeral를 감싸 store.user_store.message_log에 적재한 뒤 원본을 그대로
위임한다. 로깅 실패는 절대 실제 발송을 막지 않는다(best-effort).
"""
import json
import logging

from slack_sdk.errors import SlackApiError

from store import user_store

log = logging.getLogger(__name__)

# send 메서드명 → message_log.method 값
_LOGGED_METHODS = {
    "chat_postMessage": "post",
    "chat_update": "update",
    "chat_postEphemeral": "ephemeral",
}
_MAX_BLOCKS_BYTES = 20_000
_INSTALLED_FLAG = "_meeting_agent_logged"


def _recipient_kind(channel):
    """channel ID 접두사로 수신 유형 판정. Returns: (kind, user_id)"""
    if not channel:
        return None, None
    if channel.startswith("U"):
        return "dm", channel            # DM: channel == user_id
    if channel.startswith("D"):
        return "dm", None               # DM 채널 ID만 — user_id 미상
    return "channel", None              # C…(채널) 등


def _infer_category(text, blocks):
    """text/blocks 마커로 메시지 유형 추정 (best-effort)."""
    hay = text or ""
    try:
        if blocks:
            hay += " " + json.dumps(blocks, ensure_ascii=False)
    except Exception:
        pass
    if "브리핑" in hay or "오늘의 미팅" in hay:
        return "briefing"
    if "회의록" in hay:
        return "minutes"
    if "액션" in hay or "할 일" in hay or "리마인더" in hay:
        return "action_item"
    if "미팅 시작" in hay or "분 후" in hay:
        return "meeting_alarm"
    if "회의실" in hay or "예약" in hay:
        return "room"
    if "제안서" in hay:
        return "proposal"
    if "피드백" in hay:
        return "feedback"
    return "other"


def _truncate_blocks(blocks):
    if not blocks:
        return None
    try:
        s = json.dumps(blocks, ensure_ascii=False)
    except Exception:
        return None
    if len(s.encode("utf-8")) > _MAX_BLOCKS_BYTES:
        return s[:_MAX_BLOCKS_BYTES] + "…(truncated)"
    return s


def _record(method_label, kwargs, ok, error):
    """발송 1건 기록 — best-effort. 예외는 삼킨다(발송에 영향 금지)."""
    try:
        channel = kwargs.get("channel")
        kind, uid = _recipient_kind(channel)
        text = kwargs.get("text")
        blocks = kwargs.get("blocks")
        user_store.log_message(
            method=method_label,
            channel=channel,
            recipient_user_id=uid,
            recipient_kind=kind,
            thread_ts=kwargs.get("thread_ts"),
            text=text,
            blocks_json=_truncate_blocks(blocks),
            category=_infer_category(text, blocks),
            ok=ok,
            error=error,
        )
    except Exception as e:
        log.warning(f"메시지 로깅 실패(발송에는 영향 없음): {e}")


def _make_wrapper(original, method_label):
    def wrapped(*args, **kwargs):
        try:
            resp = original(*args, **kwargs)
        except SlackApiError as e:
            try:
                err = e.response["error"]
            except Exception:
                err = str(e)
            _record(method_label, kwargs, ok=False, error=err)
            raise
        _record(method_label, kwargs, ok=True, error=None)
        return resp
    return wrapped


def install_logging(client):
    """WebClient 인스턴스의 send 3종을 in-place로 감싼다 (idempotent).

    인스턴스 속성으로 메서드를 덮어써 클래스 메서드를 가린다. app.client 및 Bolt
    리스너 주입 client에 동일 호출해 양쪽 발송을 모두 포착한다.
    """
    if getattr(client, _INSTALLED_FLAG, False):
        return client
    for name, label in _LOGGED_METHODS.items():
        original = getattr(client, name)
        setattr(client, name, _make_wrapper(original, label))
    setattr(client, _INSTALLED_FLAG, True)
    return client
