"""Slack 발송 메시지 로깅 — WebClient send 메서드 in-place 래핑.

app.client(및 Bolt 리스너 주입 client)의 chat_postMessage/chat_update/
chat_postEphemeral를 감싸 store.user_store.message_log에 적재한 뒤 원본을 그대로
위임한다. 로깅 실패는 절대 실제 발송을 막지 않는다(best-effort).
"""
import json
import logging
import re

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

# OAuth 인증 안내 DM 등에 박히는 비밀 파라미터(state/code/token) 값 마스킹.
# 키는 남기고 값만 가려 관리자 평문 로그에 CSRF state·토큰이 보존되지 않게 한다.
_SECRET_RE = re.compile(
    r'((?:state|code|access_token|refresh_token|token)=)[^&\s"\'<>)]+',
    re.IGNORECASE,
)


def _redact_secrets(s):
    """비밀 URL 파라미터 값을 ***REDACTED***로 치환. 일반 본문은 그대로 둔다."""
    if not s:
        return s
    return _SECRET_RE.sub(r"\1***REDACTED***", s)


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
        # ephemeral 발송은 수신자가 channel이 아니라 user kwarg에 담긴다 → 사용자 귀속 보정
        if uid is None and kwargs.get("user"):
            uid = kwargs.get("user")
            if kind is None:
                kind = "dm"
        text = kwargs.get("text")
        blocks = kwargs.get("blocks")
        user_store.log_message(
            method=method_label,
            channel=channel,
            recipient_user_id=uid,
            recipient_kind=kind,
            thread_ts=kwargs.get("thread_ts"),
            text=_redact_secrets(text),
            blocks_json=_redact_secrets(_truncate_blocks(blocks)),
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


def _inbound_text_from_event(event):
    """message/app_mention 이벤트에서 기록할 텍스트 추출."""
    if event.get("subtype") == "file_share":
        files = event.get("files") or []
        if not files:
            return "[파일 업로드]"
        f0 = files[0]
        more = f" 외 {len(files) - 1}건" if len(files) > 1 else ""
        return f"[파일 업로드: {f0.get('name', '')} ({f0.get('mimetype', '')})]{more}"
    text = event.get("text", "") or ""
    # @멘션 토큰(<@U…>) 제거 — handle_mention과 동일 정리
    return " ".join(w for w in text.split() if not w.startswith("<@")).strip()


def record_inbound(body):
    """사용자 인바운드(DM·@멘션·슬래시) 1건을 message_log에 적재 — best-effort.

    버튼 action·봇 메시지·메시지 수정/삭제는 기록하지 않는다.
    인바운드 행은 recipient_user_id에 '발신자'를 담아 per-user 타임라인에 잡히게 한다.
    예외는 삼킨다(이벤트 처리를 절대 막지 않음)."""
    try:
        if not isinstance(body, dict):
            return
        event = body.get("event")
        if isinstance(event, dict):
            etype = event.get("type")
            if etype not in ("message", "app_mention"):
                return
            if event.get("bot_id"):
                return
            if event.get("subtype") not in (None, "file_share"):
                return  # message_changed/deleted 등 skip
            channel = event.get("channel")
            kind, _ = _recipient_kind(channel)
            text = _inbound_text_from_event(event)
            user_store.log_message(
                method=etype, channel=channel,
                recipient_user_id=event.get("user"),
                recipient_kind=kind or "dm", thread_ts=event.get("thread_ts"),
                text=_redact_secrets(text), blocks_json=None,
                category=_infer_category(text, None), ok=True,
                error=None, direction="inbound",
            )
            return
        if body.get("command"):  # 슬래시 커맨드
            channel = body.get("channel_id")
            kind, _ = _recipient_kind(channel)
            text = f"{body.get('command', '')} {body.get('text', '') or ''}".strip()
            user_store.log_message(
                method="command", channel=channel,
                recipient_user_id=body.get("user_id"),
                recipient_kind=kind or "dm", thread_ts=None,
                text=_redact_secrets(text), blocks_json=None,
                category=_infer_category(text, None), ok=True,
                error=None, direction="inbound",
            )
            return
        # actions(버튼) 및 그 외 → skip
    except Exception as e:
        log.warning(f"인바운드 로깅 실패(이벤트 처리에는 영향 없음): {e}")


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
