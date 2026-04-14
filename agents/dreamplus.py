"""Dreamplus Agent — 드림플러스 강남 회의실 예약 자동화

기능:
  - /드림플러스  : 이메일·비밀번호 Slack 모달로 입력 → DB 암호화 저장
  - /회의실예약      : 자연어 → 가용 회의실 추천 → 선택 버튼 → 예약
  - /회의실조회      : 이번 달 내 예약 목록 표시
  - /회의실취소      : 예약 목록 중 선택 취소
  - /크레딧조회      : 잔여 포인트 표시
  - auto_book_room() : 미팅 생성(before.py) 직후 자동 회의실 추천 버튼 제안
"""
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from store import user_store
from tools import dreamplus as dp

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# 드림플러스 설정 모달 view_id
_MODAL_CALLBACK = "dreamplus_settings_modal"


# ── 인증 헬퍼 ─────────────────────────────────────────────────

def _get_session(user_id: str, force_refresh: bool = False) -> tuple[str, str, int, int]:
    """(jwt, public_key, member_id, company_id) 반환. 캐시 우선, 만료/없음 시 재로그인.
    드림플러스 자격증명 미설정 시 ValueError.
    """
    if not force_refresh:
        cached = user_store.get_dreamplus_jwt(user_id)
        if cached:
            return cached

    creds = user_store.get_dreamplus_credentials(user_id)
    if not creds:
        raise ValueError("드림플러스 계정이 설정되지 않았습니다. `/드림플러스`으로 먼저 설정해주세요.")

    email, password = creds
    jwt, pub_key, member_id, company_id = dp.login(email, password)
    user_store.save_dreamplus_jwt(user_id, jwt, pub_key, member_id, company_id)
    return jwt, pub_key, member_id, company_id


def _post(slack_client, user_id: str, text: str, blocks=None,
          channel: str = None, thread_ts: str = None):
    kwargs = {"channel": channel or user_id, "text": text}
    if blocks:
        kwargs["blocks"] = blocks
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    slack_client.chat_postMessage(**kwargs)


# ── 날짜 파싱 ─────────────────────────────────────────────────

def _parse_datetime_range(text: str) -> tuple[datetime, datetime] | None:
    """'내일 2시~3시', '오늘 14:00-15:00', '4/5 오후2시 1시간' 등 파싱.
    Returns (start_dt, end_dt) in KST, or None.
    """
    now = datetime.now(KST)
    text = text.strip()

    # 날짜 파싱
    if "오늘" in text:
        base = now.date()
    elif "내일" in text:
        base = (now + timedelta(days=1)).date()
    else:
        m = re.search(r"(\d{1,2})[/.-](\d{1,2})", text)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            base = now.date().replace(month=month, day=day)
        else:
            base = now.date()

    # 시간 파싱 (오전/오후, 24시간 모두 처리)
    times = re.findall(r"(?:오전|오후)?\s*(\d{1,2})(?::(\d{2}))?(?:시)?", text)
    hours = []
    parts = list(re.finditer(r"(오전|오후)?\s*(\d{1,2})(?::(\d{2}))?(?:시)?", text))
    for p in parts:
        ampm = p.group(1) or ""
        h = int(p.group(2))
        m_ = int(p.group(3)) if p.group(3) else 0
        if ampm == "오후" and h < 12:
            h += 12
        elif ampm == "" and 1 <= h <= 8:
            # 오전/오후 미지정 + 업무시간 범위 밖(1~8시) → 오후로 간주
            h += 12
        hours.append((h, m_))

    if not hours:
        return None

    start_h, start_m = hours[0]
    start_dt = datetime(base.year, base.month, base.day, start_h, start_m, tzinfo=KST)

    if len(hours) >= 2:
        end_h, end_m = hours[1]
        end_dt = datetime(base.year, base.month, base.day, end_h, end_m, tzinfo=KST)
    else:
        # 기간 언급 없으면 1시간
        m_dur = re.search(r"(\d+)\s*시간", text)
        hours_dur = int(m_dur.group(1)) if m_dur else 1
        m_min = re.search(r"(\d+)\s*분", text)
        mins_dur = int(m_min.group(1)) if m_min else 0
        end_dt = start_dt + timedelta(hours=hours_dur, minutes=mins_dur)

    return start_dt, end_dt


def _parse_attendee_count(text: str) -> int:
    """'4명', '5인' 등에서 인원수 추출. 기본값 2."""
    m = re.search(r"(\d+)\s*(?:명|인|people|person)", text)
    return int(m.group(1)) if m else 2


def _parse_preferred_floor(text: str) -> int | None:
    """'8층', '2층' 등에서 층수 추출. 없으면 None."""
    m = re.search(r"(\d+)\s*층", text)
    return int(m.group(1)) if m else None


def _parse_preferred_capacity(text: str) -> int | None:
    """'4인실', '8인 회의실', '수용인원 6' 등에서 희망 수용인원 추출. 없으면 None."""
    m = re.search(r"(\d+)\s*인\s*(?:실|회의실|짜리)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"수용\s*인원\s*(\d+)", text)
    if m:
        return int(m.group(1))
    return None


# ── 회의실 추천 로직 ──────────────────────────────────────────

_FLOOR_PRIORITY = {8: 0, 2: 1, 3: 2}  # 기본 층 우선순위: 8층 > 2층 > 3층 > 나머지

def _recommend_rooms(rooms: list[dict], attendee_count: int,
                     start_dt: datetime, end_dt: datetime,
                     preferred_floor: int | None = None,
                     preferred_capacity: int | None = None) -> list[dict]:
    """수용인원 >= attendee_count인 회의실 중 최대 3개 추천.

    정렬 기준:
    1. preferred_floor 지정 시 해당 층 최우선, 없으면 기본 우선순위(8>2>3)
    2. preferred_capacity 지정 시 해당 수용인원과 가장 가까운 방 우선, 없으면 4인 우선
    3. 포인트 낮은 순
    """
    effective_count = max(attendee_count, 1)
    duration_slots = max(1, int((end_dt - start_dt).total_seconds() / 1800))
    candidates = [r for r in rooms if r.get("maxMember", 0) >= effective_count]

    def sort_key(r):
        floor = r.get("floor", 99)
        capacity = r.get("maxMember", 99)

        # 층 우선순위
        if preferred_floor is not None:
            floor_rank = 0 if floor == preferred_floor else 1
        else:
            floor_rank = _FLOOR_PRIORITY.get(floor, 3)

        # 수용인원 우선순위
        if preferred_capacity is not None:
            cap_rank = abs(capacity - preferred_capacity)
        else:
            cap_rank = 0 if capacity == 4 else 1  # 기본: 4인 우선

        return (floor_rank, cap_rank, r.get("point", 0))

    candidates.sort(key=sort_key)
    for r in candidates:
        r["_total_point"] = r.get("point", 0) * duration_slots
    return candidates  # 전체 반환, 잘라내기는 호출부에서


def _room_block(room: dict, start_dt: datetime, end_dt: datetime,
                meeting_title: str = "") -> dict:
    """회의실 선택 버튼 Block (section + button)"""
    duration_min = int((end_dt - start_dt).total_seconds() / 60)
    total_pt = room.get("_total_point", room.get("point", 0) * duration_min // 30)
    pt_str = f"{total_pt:,}pt" if total_pt else "무료"
    time_str = f"{start_dt.strftime('%H:%M')}~{end_dt.strftime('%H:%M')}"
    room_name = room.get("roomName", "")
    value = f"{room['roomCode']}|{start_dt.isoformat()}|{end_dt.isoformat()}|{meeting_title}|{room_name}"
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                f"*{room['roomName']}* ({room.get('floor', '?')}층 · "
                f"최대 {room.get('maxMember', '?')}인)\n"
                f"{time_str} · {pt_str}"
            ),
        },
        "accessory": {
            "type": "button",
            "text": {"type": "plain_text", "text": "예약하기"},
            "style": "primary",
            "action_id": "dreamplus_book_room",
            "value": value,
        },
    }


# ── /드림플러스 ───────────────────────────────────────────

def open_settings_modal(slack_client, trigger_id: str, user_id: str):
    """드림플러스 계정 설정 모달 열기"""
    existing = user_store.get_dreamplus_credentials(user_id)
    initial_email = existing[0] if existing else ""

    slack_client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": _MODAL_CALLBACK,
            "title": {"type": "plain_text", "text": "드림플러스 계정 설정"},
            "submit": {"type": "plain_text", "text": "저장"},
            "close": {"type": "plain_text", "text": "취소"},
            "blocks": [
                {
                    "type": "input",
                    "block_id": "dp_email",
                    "label": {"type": "plain_text", "text": "드림플러스 이메일"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "email_input",
                        "initial_value": initial_email,
                        "placeholder": {"type": "plain_text", "text": "example@company.com"},
                    },
                },
                {
                    "type": "input",
                    "block_id": "dp_password",
                    "label": {"type": "plain_text", "text": "비밀번호"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "password_input",
                        "placeholder": {"type": "plain_text", "text": "드림플러스 로그인 비밀번호"},
                    },
                },
            ],
        },
    )


def handle_settings_modal(slack_client, body: dict):
    """드림플러스 설정 모달 제출 처리"""
    user_id = body["user"]["id"]
    values = body["view"]["state"]["values"]
    email = values["dp_email"]["email_input"]["value"].strip()
    password = values["dp_password"]["password_input"]["value"].strip()

    if not email or not password:
        return

    # 로그인 테스트 후 저장
    try:
        jwt, pub_key, member_id, company_id = dp.login(email, password)
        user_store.save_dreamplus_credentials(user_id, email, password)
        user_store.save_dreamplus_jwt(user_id, jwt, pub_key, member_id, company_id)
        _post(slack_client, user_id, "✅ 드림플러스 계정이 설정되었습니다.")
        log.info(f"드림플러스 계정 설정 완료: {user_id} ({email})")
    except Exception as e:
        _post(slack_client, user_id, f"❌ 드림플러스 로그인 실패: {e}\n이메일·비밀번호를 확인해주세요.")


# ── /회의실예약 ───────────────────────────────────────────────

def book_room(slack_client, user_id: str, text: str,
              channel: str = None, thread_ts: str = None):
    """/회의실예약 {자연어} — 가용 회의실 추천 후 선택 버튼 발송"""
    try:
        jwt, pub_key, member_id, company_id = _get_session(user_id)
    except ValueError as e:
        _post(slack_client, user_id, f"⚠️ {e}", channel=channel, thread_ts=thread_ts)
        return

    result = _parse_datetime_range(text)
    if not result:
        _post(slack_client, user_id,
              "⚠️ 시간을 인식하지 못했습니다.\n"
              "예: `내일 오후 2시~3시 4명`, `오늘 14시 2시간 8층 6인실`",
              channel=channel, thread_ts=thread_ts)
        return
    start_dt, end_dt = result
    attendee_count = _parse_attendee_count(text)
    preferred_floor = _parse_preferred_floor(text)
    preferred_capacity = _parse_preferred_capacity(text)

    try:
        rooms = dp.get_rooms(jwt)
    except (dp.TokenExpiredError, RuntimeError):
        # JWT 만료 또는 세션 오류 → 강제 재로그인 후 1회 재시도
        try:
            jwt, pub_key, member_id, company_id = _get_session(user_id, force_refresh=True)
            rooms = dp.get_rooms(jwt)
        except Exception as e:
            _post(slack_client, user_id, f"❌ 회의실 목록 조회 실패: {e}",
                  channel=channel, thread_ts=thread_ts)
            return

    all_candidates = _recommend_rooms(rooms, attendee_count, start_dt, end_dt,
                                      preferred_floor=preferred_floor,
                                      preferred_capacity=preferred_capacity)
    if not all_candidates:
        _post(slack_client, user_id,
              f"😔 {attendee_count}인 이상 수용 가능한 회의실을 찾지 못했습니다.",
              channel=channel, thread_ts=thread_ts)
        return

    page = 0
    batch = all_candidates[:3]
    date_str = start_dt.strftime("%m월 %d일")
    cond_str = f"{attendee_count}인 이상"
    if preferred_floor:
        cond_str += f" · {preferred_floor}층"
    if preferred_capacity:
        cond_str += f" · {preferred_capacity}인실 우선"
    nav_value = f"{start_dt.isoformat()}|{end_dt.isoformat()}|{text}|{attendee_count}|{{page}}|{preferred_floor or ''}|{preferred_capacity or ''}"
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🏢 *{date_str} {start_dt.strftime('%H:%M')}~{end_dt.strftime('%H:%M')}* "
                    f"({cond_str}) 가용 회의실"
                ),
            },
        },
        {"type": "divider"},
    ]
    for room in batch:
        blocks.append(_room_block(room, start_dt, end_dt, meeting_title="회의"))
    blocks.append(_nav_buttons(page, has_next=len(all_candidates) > 3, nav_value=nav_value))
    _post(slack_client, user_id,
          f"드림플러스 회의실 추천 ({date_str})", blocks=blocks,
          channel=channel, thread_ts=thread_ts)


def confirm_room_booking(slack_client, body: dict):
    """dreamplus_book_room 버튼 핸들러 — 예약 확정"""
    user_id = body["user"]["id"]
    value = body["actions"][0]["value"]

    try:
        parts = value.split("|", 4)
        room_code = int(parts[0])
        start_dt = datetime.fromisoformat(parts[1])
        end_dt = datetime.fromisoformat(parts[2])
        meeting_title = parts[3] if len(parts) > 3 else "회의"
        room_name = parts[4] if len(parts) > 4 else f"드림플러스 Room {room_code}"
    except Exception:
        _post(slack_client, user_id, "⚠️ 예약 정보를 파싱하지 못했습니다.")
        return

    try:
        jwt, pub_key, member_id, company_id = _get_session(user_id)
        dp.make_reservation(jwt, room_code, start_dt, end_dt, meeting_title)
    except ValueError as e:
        _post(slack_client, user_id, f"⚠️ {e}")
        return
    except dp.TokenExpiredError:
        user_store.save_dreamplus_jwt(user_id, "", "")
        _post(slack_client, user_id, "⚠️ 세션이 만료되었습니다. 다시 시도해주세요.")
        return
    except Exception as e:
        _post(slack_client, user_id, f"❌ 예약 실패: {e}")
        return

    time_str = f"{start_dt.strftime('%m/%d %H:%M')}~{end_dt.strftime('%H:%M')}"
    location_str = f"드림플러스 강남 {room_name}"
    _post(slack_client, user_id,
          f"✅ 드림플러스 회의실 예약 완료!\n*{room_name}* | {time_str} | {meeting_title}")

    # 캘린더 이벤트에 장소 업데이트
    if event_id:
        try:
            import tools.calendar as cal
            creds = user_store.get_credentials(user_id)
            cal.update_event(creds, event_id, location=location_str)
            _post(slack_client, user_id,
                  f"📍 캘린더 일정 장소가 *{location_str}* 으로 업데이트되었습니다.")
        except Exception as e:
            log.warning(f"캘린더 location 업데이트 실패: {e}")


# ── /회의실조회 ───────────────────────────────────────────────

def list_reservations(slack_client, user_id: str,
                      channel: str = None, thread_ts: str = None):
    """/회의실조회 — 이번 달 내 예약 목록"""
    try:
        jwt, pub_key, member_id, company_id = _get_session(user_id)
    except ValueError as e:
        _post(slack_client, user_id, f"⚠️ {e}", channel=channel, thread_ts=thread_ts)
        return

    try:
        items = dp.get_reservations(jwt, company_id=company_id or None)
    except (dp.TokenExpiredError, RuntimeError):
        try:
            jwt, pub_key, member_id, company_id = _get_session(user_id, force_refresh=True)
            items = dp.get_reservations(jwt, company_id=company_id or None)
        except Exception as e:
            _post(slack_client, user_id, f"❌ 예약 조회 실패: {e}",
                  channel=channel, thread_ts=thread_ts)
            return

    # 내 예약만 필터링
    if not member_id:
        _post(slack_client, user_id, "⚠️ 사용자 정보를 확인할 수 없습니다. 잠시 후 다시 시도해주세요.",
              channel=channel, thread_ts=thread_ts)
        return
    items = [i for i in items if i.get("memberId") == member_id]

    # 예약 완료(531) 항목만 표시 (사용완료 534 제외)
    active = [i for i in items if i.get("reservationState") == 531]
    if not active:
        _post(slack_client, user_id, "📋 예약된 회의실이 없습니다.",
              channel=channel, thread_ts=thread_ts)
        return

    _STATES = {531: "예약완료", 532: "취소", 534: "사용완료"}
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "📋 *이번 달 드림플러스 예약 내역*"}},
        {"type": "divider"},
    ]
    for item in active[:10]:  # 최대 10개
        state = _STATES.get(item.get("reservationState", 0), "?")
        pt_str = f"{item.get('point', 0):,}pt" if item.get("point") else "무료"
        cancel_btn = None
        if item.get("reservationState") == 531:  # 예약완료만 취소 버튼
            cancel_btn = {
                "type": "button",
                "text": {"type": "plain_text", "text": "취소"},
                "style": "danger",
                "action_id": "dreamplus_cancel_confirm",
                "value": str(item["id"]),
                "confirm": {
                    "title": {"type": "plain_text", "text": "예약 취소"},
                    "text": {"type": "mrkdwn",
                             "text": f"*{item.get('roomName')}* {item.get('startTime')} 예약을 취소할까요?"},
                    "confirm": {"type": "plain_text", "text": "취소하기"},
                    "deny": {"type": "plain_text", "text": "돌아가기"},
                },
            }

        section = {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{item.get('roomName', '?')}* | {state}\n"
                    f"{item.get('startTime', '')} ~ {item.get('endTime', '')} · {pt_str}\n"
                    f"_{item.get('title', '')}_"
                ),
            },
        }
        if cancel_btn:
            section["accessory"] = cancel_btn
        blocks.append(section)

    _post(slack_client, user_id, "드림플러스 예약 내역", blocks=blocks,
          channel=channel, thread_ts=thread_ts)


# ── 이전/다음 회의실 네비게이션 ──────────────────────────────

def _nav_buttons(page: int, has_next: bool, nav_value: str) -> dict:
    """이전/다음 버튼 actions 블록 생성.
    nav_value: '{page}' 플레이스홀더 포함된 포맷 문자열
    """
    elements = []
    if page > 0:
        elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "이전"},
            "action_id": "dreamplus_prev_rooms",
            "value": nav_value.format(page=page - 1),
        })
    if has_next:
        elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "다음"},
            "action_id": "dreamplus_next_rooms",
            "value": nav_value.format(page=page + 1),
        })
    return {"type": "actions", "elements": elements}


def _navigate_rooms(slack_client, body: dict):
    """이전/다음 버튼 공통 핸들러"""
    user_id = body["user"]["id"]
    value = body["actions"][0]["value"]

    try:
        parts = value.split("|")
        start_dt = datetime.fromisoformat(parts[0])
        end_dt = datetime.fromisoformat(parts[1])
        meeting_title = parts[2]
        attendee_count = int(parts[3]) if parts[3].isdigit() else 2
        page = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0
        preferred_floor = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else None
        preferred_capacity = int(parts[6]) if len(parts) > 6 and parts[6].isdigit() else None
    except Exception:
        _post(slack_client, user_id, "⚠️ 회의실 목록을 불러오지 못했습니다.")
        return

    try:
        jwt, pub_key, member_id, company_id = _get_session(user_id)
        rooms = dp.get_rooms(jwt)
    except (dp.TokenExpiredError, RuntimeError):
        try:
            jwt, pub_key, member_id, company_id = _get_session(user_id, force_refresh=True)
            rooms = dp.get_rooms(jwt)
        except Exception as e:
            _post(slack_client, user_id, f"❌ 회의실 목록 조회 실패: {e}")
            return
    except ValueError as e:
        _post(slack_client, user_id, f"⚠️ {e}")
        return

    all_candidates = _recommend_rooms(rooms, attendee_count, start_dt, end_dt,
                                      preferred_floor=preferred_floor,
                                      preferred_capacity=preferred_capacity)
    batch = all_candidates[page * 3:(page + 1) * 3]

    date_str = start_dt.strftime("%m월 %d일")
    cond_str = f"{attendee_count}인 이상"
    if preferred_floor:
        cond_str += f" · {preferred_floor}층"
    if preferred_capacity:
        cond_str += f" · {preferred_capacity}인실 우선"

    nav_value = f"{start_dt.isoformat()}|{end_dt.isoformat()}|{meeting_title}|{attendee_count}|{{page}}|{preferred_floor or ''}|{preferred_capacity or ''}"

    if not batch:
        # 범위 벗어난 경우 (이전 누르다가 0 미만 등) 첫 페이지로
        page = 0
        batch = all_candidates[:3]

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🏢 *{date_str} {start_dt.strftime('%H:%M')}~{end_dt.strftime('%H:%M')}* "
                    f"({cond_str}) 추천 회의실"
                ),
            },
        },
        {"type": "divider"},
    ]
    for room in batch:
        blocks.append(_room_block(room, start_dt, end_dt, meeting_title))
    has_next = len(all_candidates) > (page + 1) * 3
    blocks.append(_nav_buttons(page, has_next=has_next, nav_value=nav_value))

    try:
        slack_client.chat_update(
            channel=body["container"]["channel_id"],
            ts=body["container"]["message_ts"],
            text=f"드림플러스 회의실 추천 ({date_str})",
            blocks=blocks,
        )
    except Exception:
        _post(slack_client, user_id, f"드림플러스 회의실 추천 ({date_str})", blocks=blocks)


def next_rooms(slack_client, body: dict):
    """dreamplus_next_rooms 버튼 핸들러"""
    _navigate_rooms(slack_client, body)


def prev_rooms(slack_client, body: dict):
    """dreamplus_prev_rooms 버튼 핸들러"""
    _navigate_rooms(slack_client, body)


# ── /회의실취소 ───────────────────────────────────────────────

def cancel_room(slack_client, user_id: str, text: str,
                channel: str = None, thread_ts: str = None):
    """/회의실취소 — 예약 목록 표시 후 선택 취소 (버튼은 list_reservations에서 처리)"""
    list_reservations(slack_client, user_id, channel=channel, thread_ts=thread_ts)


def confirm_cancel(slack_client, body: dict):
    """dreamplus_cancel_confirm 버튼 핸들러 — 예약 취소 확정"""
    user_id = body["user"]["id"]
    reservation_id = int(body["actions"][0]["value"])

    try:
        jwt, pub_key, member_id, company_id = _get_session(user_id)
    except ValueError as e:
        _post(slack_client, user_id, f"⚠️ {e}")
        return

    try:
        refund = dp.get_refund_info(jwt, reservation_id)
        refund_pt = refund.get("refund", 0)
        dp.cancel_reservation(jwt, pub_key, reservation_id)
    except (dp.TokenExpiredError, RuntimeError):
        try:
            jwt, pub_key, member_id, company_id = _get_session(user_id, force_refresh=True)
            refund = dp.get_refund_info(jwt, reservation_id)
            refund_pt = refund.get("refund", 0)
            dp.cancel_reservation(jwt, pub_key, reservation_id)
        except Exception as e:
            _post(slack_client, user_id, f"❌ 취소 실패: {e}")
            return
    except Exception as e:
        _post(slack_client, user_id, f"❌ 취소 실패: {e}")
        return

    refund_str = f" (환불 예정: {refund_pt:,}pt)" if refund_pt else ""
    _post(slack_client, user_id, f"✅ 예약이 취소되었습니다.{refund_str}")


# ── /크레딧조회 ───────────────────────────────────────────────

def show_credits(slack_client, user_id: str,
                 channel: str = None, thread_ts: str = None):
    """/크레딧조회 — 드림플러스 잔여 포인트"""
    try:
        jwt, pub_key, member_id, company_id = _get_session(user_id)
        data = dp.get_credits(jwt, pub_key)
    except ValueError as e:
        _post(slack_client, user_id, f"⚠️ {e}", channel=channel, thread_ts=thread_ts)
        return
    except dp.TokenExpiredError:
        user_store.save_dreamplus_jwt(user_id, "", "")
        _post(slack_client, user_id, "⚠️ 세션이 만료되었습니다. 다시 시도해주세요.",
              channel=channel, thread_ts=thread_ts)
        return
    except RuntimeError as e:
        msg = str(e)
        if "시스템 오류" in msg:
            _post(slack_client, user_id,
                  "⚠️ 드림플러스 포인트 조회 API가 현재 지원되지 않습니다.\n"
                  "잔여 포인트는 드림플러스 앱 또는 웹사이트에서 직접 확인해주세요.",
                  channel=channel, thread_ts=thread_ts)
        else:
            _post(slack_client, user_id, f"❌ 크레딧 조회 실패: {msg}",
                  channel=channel, thread_ts=thread_ts)
        return
    except Exception as e:
        _post(slack_client, user_id, f"❌ 크레딧 조회 실패: {e}",
              channel=channel, thread_ts=thread_ts)
        return

    balance = (data.get("balance") or data.get("point") or
               data.get("totalPoint") or data.get("remainPoint") or 0)
    _post(slack_client, user_id, f"💳 드림플러스 잔여 포인트: *{balance:,} pt*",
          channel=channel, thread_ts=thread_ts)


# ── 미팅 생성 연동 (auto_book_room) ──────────────────────────

def auto_book_room(slack_client, *, user_id: str, start_dt: datetime,
                   end_dt: datetime, title: str, attendee_count: int = 2,
                   channel: str = None, thread_ts: str = None,
                   event_id: str = None):
    """미팅 생성 직후 자동 호출 — 회의실 추천 버튼 Slack 발송.
    드림플러스 계정 미설정 또는 오류 시 조용히 스킵.
    """
    creds = user_store.get_dreamplus_credentials(user_id)
    if not creds:
        return  # 계정 미설정 → 조용히 스킵

    try:
        jwt, pub_key, member_id, company_id = _get_session(user_id)
        rooms = dp.get_rooms(jwt)
    except (dp.TokenExpiredError, RuntimeError):
        # JWT 만료 또는 세션 오류 → 강제 재로그인 후 1회 재시도
        try:
            jwt, pub_key, member_id, company_id = _get_session(user_id, force_refresh=True)
            rooms = dp.get_rooms(jwt)
        except Exception as e:
            log.warning(f"auto_book_room 회의실 조회 재시도 실패 (스킵): {e}")
            return
    except Exception as e:
        log.warning(f"auto_book_room 회의실 조회 실패 (스킵): {e}")
        return

    all_candidates = _recommend_rooms(rooms, attendee_count, start_dt, end_dt)
    recommended = all_candidates[:3]
    if not recommended:
        return

    date_str = start_dt.strftime("%m월 %d일")
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🏢 *{title}* 미팅에 맞는 드림플러스 회의실을 예약할까요?\n"
                    f"{date_str} {start_dt.strftime('%H:%M')}~{end_dt.strftime('%H:%M')} · {attendee_count}인 이상"
                ),
            },
        },
        {"type": "divider"},
    ]
    page = 0
    nav_value = f"{start_dt.isoformat()}|{end_dt.isoformat()}|{title}|{attendee_count}|{{page}}||"
    for room in recommended:
        blocks.append(_room_block(room, start_dt, end_dt, title))
    blocks.append(_nav_buttons(page, has_next=len(all_candidates) > 3, nav_value=nav_value))
    _post(slack_client, user_id,
          f"드림플러스 회의실 예약 제안: {title}", blocks=blocks,
          channel=channel, thread_ts=thread_ts)
