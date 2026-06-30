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


_CHANNEL_NAME_CACHE: dict = {}


def _resolve_channel_name(client, channel_id: str) -> str:
    """conversations.info로 채널 이름 해석(프로세스 캐시). 실패 시 빈 문자열."""
    if channel_id in _CHANNEL_NAME_CACHE:
        return _CHANNEL_NAME_CACHE[channel_id]
    name = ""
    try:
        resp = client.conversations_info(channel=channel_id)
        name = (resp.get("channel") or {}).get("name", "") or ""
    except Exception as e:
        log.warning(f"채널 이름 조회 실패({channel_id}): {e}")
    _CHANNEL_NAME_CACHE[channel_id] = name
    return name


def biz_channels_resolved(client) -> list[dict]:
    """biz_channel_list에 이름 보강 — env에 이름 없으면 conversations.info로 해석(캐시).
    에이전트가 'C0A7E50DMGW(nh-biz)'처럼 보고 관련 채널을 고를 수 있게."""
    out = []
    for c in biz_channel_list():
        name = c["name"] or (_resolve_channel_name(client, c["id"]) if client else "")
        out.append({"id": c["id"], "name": name})
    return out


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


def _resolve_to_allowed_id(client, channel_arg: str) -> str:
    """에이전트가 채널을 ID로 주든 이름('parasta_biz'·'#parasta_biz')으로 주든 allowlist 정식 ID로 해석.
    매칭 없으면 빈 문자열(allowlist 외)."""
    arg = (channel_arg or "").strip().lstrip("#")
    if arg in allowed_channels():
        return arg
    for c in biz_channels_resolved(client):
        if c["name"] and arg == c["name"]:
            return c["id"]
    return ""


def _membership_gate_enabled() -> bool:
    """요청자 멤버십 게이트 토글. **기본 ON(안전)** — 채널이 분산돼 있고 사용자가 봇을 자기 방에
    초대할 수 있으므로, 요청자가 멤버인 채널만 노출해 크로스유저 누수를 막는다(channels:read/groups:read 필요).
    모든 봇 사용자가 동일 채널을 공유하는 폐쇄 환경에서만 SLACK_MEMBERSHIP_GATE=false로 끌 수 있다."""
    return os.getenv("SLACK_MEMBERSHIP_GATE", "true").lower() != "false"


def channel_history(client, channel_id: str, requesting_user_id: str = "",
                    limit: int = 30) -> list[dict]:
    """allowlist된 biz 채널의 최근 메시지. 게이트 OFF(기본)면 allowlist만으로 노출
    (봇이 들어간 팀 공유 채널 = 관리자가 의도한 공유 정책). 게이트 ON이면 요청자 멤버십까지 확인
    (봇 사용자 간 접근이 다른 GA 환경의 ACL 누수 방지). 미허용/비멤버 시 빈 리스트."""
    cid = _resolve_to_allowed_id(client, channel_id)
    if not cid:
        log.info(f"slack channel_history 차단(allowlist 외/미해석): {channel_id}")
        return []
    if _membership_gate_enabled():
        if not requesting_user_id:
            log.info(f"slack channel_history 차단(게이트 ON·요청자 미상): {cid}")
            return []
        if not _is_member(client, cid, requesting_user_id):
            log.info(f"slack channel_history 차단(요청자 비멤버): {requesting_user_id}@{cid}")
            return []
    try:
        resp = client.conversations_history(channel=cid, limit=limit)
        return [{"text": m.get("text", ""), "ts": m.get("ts", ""), "user": m.get("user", "")}
                for m in (resp.get("messages") or []) if m.get("text")]
    except Exception as e:
        log.warning(f"slack channel_history 실패({channel_id}): {e}")
        return []
