"""Dreamplus Agent — 드림플러스 강남 회의실 예약 자동화

기능:
  - /드림플러스설정  : 이메일·비밀번호 Slack 모달로 입력 → DB 암호화 저장
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

def _get_session(user_id: str) -> tuple[str, str]:
    """(jwt, public_key) 반환. 캐시 우선, 만료/없음 시 재로그인.
    드림플러스 자격증명 미설정 시 ValueError.
    """
    cached = user_store.get_dreamplus_jwt(user_id)
    if cached:
        return cached

    creds = user_store.get_dreamplus_credentials(user_id)
    if not creds:
        raise ValueError("드림플러스 계정이 설정되지 않았습니다. `/드림플러스설정`으로 먼저 설정해주세요.")

    email, password = creds
    jwt, pub_key = dp.login(email, password)
    user_store.save_dreamplus_jwt(user_id, jwt, pub_key)
    return jwt, pub_key


def _post(slack_client, user_id: str, text: str, blocks=None):
    kwargs = {"channel": user_id, "text": text}
    if blocks:
        kwargs["blocks"] = blocks
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


# ── 회의실 추천 로직 ──────────────────────────────────────────

def _recommend_rooms(rooms: list[dict], attendee_count: int,
                     start_dt: datetime, end_dt: datetime) -> list[dict]:
    """수용인원 >= attendee_count인 회의실 중 포인트 낮은 순으로 최대 3개 추천.
    같은 포인트면 층 낮은 순.
    """
    duration_slots = max(1, int((end_dt - start_dt).total_seconds() / 1800))
    candidates = [r for r in rooms if r.get("maxMember", 0) >= attendee_count]
    candidates.sort(key=lambda r: (r.get("point", 0), r.get("floor", 99)))
    for r in candidates:
        r["_total_point"] = r.get("point", 0) * duration_slots
    return candidates[:3]


def _room_block(room: dict, start_dt: datetime, end_dt: datetime,
                meeting_title: str = "") -> dict:
    """회의실 선택 버튼 Block (section + button)"""
    duration_min = int((end_dt - start_dt).total_seconds() / 60)
    total_pt = room.get("_total_point", room.get("point", 0) * duration_min // 30)
    pt_str = f"{total_pt:,}pt" if total_pt else "무료"
    time_str = f"{start_dt.strftime('%H:%M')}~{end_dt.strftime('%H:%M')}"
    value = f"{room['roomCode']}|{start_dt.isoformat()}|{end_dt.isoformat()}|{meeting_title}"
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


# ── /드림플러스설정 ───────────────────────────────────────────

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
        jwt, pub_key = dp.login(email, password)
        user_store.save_dreamplus_credentials(user_id, email, password)
        user_store.save_dreamplus_jwt(user_id, jwt, pub_key)
        _post(slack_client, user_id, "✅ 드림플러스 계정이 설정되었습니다.")
        log.info(f"드림플러스 계정 설정 완료: {user_id} ({email})")
    except Exception as e:
        _post(slack_client, user_id, f"❌ 드림플러스 로그인 실패: {e}\n이메일·비밀번호를 확인해주세요.")


# ── /회의실예약 ───────────────────────────────────────────────

def book_room(slack_client, user_id: str, text: str):
    """/회의실예약 {자연어} — 가용 회의실 추천 후 선택 버튼 발송"""
    try:
        jwt, pub_key = _get_session(user_id)
    except ValueError as e:
        _post(slack_client, user_id, f"⚠️ {e}")
        return

    result = _parse_datetime_range(text)
    if not result:
        _post(slack_client, user_id,
              "⚠️ 시간을 인식하지 못했습니다.\n예: `/회의실예약 내일 오후 2시~3시 4명`")
        return
    start_dt, end_dt = result
    attendee_count = _parse_attendee_count(text)

    try:
        rooms = dp.get_rooms(jwt)
    except dp.TokenExpiredError:
        user_store.save_dreamplus_jwt(user_id, "", "")
        _post(slack_client, user_id, "⚠️ 세션이 만료되었습니다. 다시 시도해주세요.")
        return
    except Exception as e:
        _post(slack_client, user_id, f"❌ 회의실 목록 조회 실패: {e}")
        return

    recommended = _recommend_rooms(rooms, attendee_count, start_dt, end_dt)
    if not recommended:
        _post(slack_client, user_id,
              f"😔 {attendee_count}인 이상 수용 가능한 회의실을 찾지 못했습니다.")
        return

    date_str = start_dt.strftime("%m월 %d일")
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🏢 *{date_str} {start_dt.strftime('%H:%M')}~{end_dt.strftime('%H:%M')}* "
                    f"({attendee_count}인 이상) 가용 회의실"
                ),
            },
        },
        {"type": "divider"},
    ]
    for room in recommended:
        blocks.append(_room_block(room, start_dt, end_dt))
    blocks.append({
        "type": "actions",
        "elements": [{
            "type": "button",
            "text": {"type": "plain_text", "text": "건너뜀"},
            "action_id": "dreamplus_skip_booking",
            "value": "skip",
        }],
    })
    _post(slack_client, user_id,
          f"드림플러스 회의실 추천 ({date_str})", blocks=blocks)


def confirm_room_booking(slack_client, body: dict):
    """dreamplus_book_room 버튼 핸들러 — 예약 확정"""
    user_id = body["user"]["id"]
    value = body["actions"][0]["value"]

    try:
        parts = value.split("|", 3)
        room_code = int(parts[0])
        start_dt = datetime.fromisoformat(parts[1])
        end_dt = datetime.fromisoformat(parts[2])
        meeting_title = parts[3] if len(parts) > 3 else "회의"
    except Exception:
        _post(slack_client, user_id, "⚠️ 예약 정보를 파싱하지 못했습니다.")
        return

    try:
        jwt, pub_key = _get_session(user_id)
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
    _post(slack_client, user_id,
          f"✅ 드림플러스 회의실 예약 완료!\n*roomCode {room_code}* | {time_str} | {meeting_title}")


# ── /회의실조회 ───────────────────────────────────────────────

def list_reservations(slack_client, user_id: str):
    """/회의실조회 — 이번 달 내 예약 목록"""
    try:
        jwt, pub_key = _get_session(user_id)
    except ValueError as e:
        _post(slack_client, user_id, f"⚠️ {e}")
        return

    try:
        items = dp.get_reservations(jwt)
    except dp.TokenExpiredError:
        user_store.save_dreamplus_jwt(user_id, "", "")
        _post(slack_client, user_id, "⚠️ 세션이 만료되었습니다. 다시 시도해주세요.")
        return
    except Exception as e:
        _post(slack_client, user_id, f"❌ 예약 조회 실패: {e}")
        return

    # 예약 완료(531)·사용 완료(534) 항목만 표시
    active = [i for i in items if i.get("reservationState") in (531, 534)]
    if not active:
        _post(slack_client, user_id, "📋 이번 달 예약 내역이 없습니다.")
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

    _post(slack_client, user_id, "드림플러스 예약 내역", blocks=blocks)


# ── /회의실취소 ───────────────────────────────────────────────

def cancel_room(slack_client, user_id: str, text: str):
    """/회의실취소 — 예약 목록 표시 후 선택 취소 (버튼은 list_reservations에서 처리)"""
    list_reservations(slack_client, user_id)


def confirm_cancel(slack_client, body: dict):
    """dreamplus_cancel_confirm 버튼 핸들러 — 예약 취소 확정"""
    user_id = body["user"]["id"]
    reservation_id = int(body["actions"][0]["value"])

    try:
        jwt, pub_key = _get_session(user_id)
        # 환불 정보 먼저 확인
        refund = dp.get_refund_info(jwt, reservation_id)
        refund_pt = refund.get("refund", 0)
        dp.cancel_reservation(jwt, pub_key, reservation_id)
    except ValueError as e:
        _post(slack_client, user_id, f"⚠️ {e}")
        return
    except dp.TokenExpiredError:
        user_store.save_dreamplus_jwt(user_id, "", "")
        _post(slack_client, user_id, "⚠️ 세션이 만료되었습니다. 다시 시도해주세요.")
        return
    except Exception as e:
        _post(slack_client, user_id, f"❌ 취소 실패: {e}")
        return

    refund_str = f" (환불 예정: {refund_pt:,}pt)" if refund_pt else ""
    _post(slack_client, user_id, f"✅ 예약이 취소되었습니다.{refund_str}")


# ── /크레딧조회 ───────────────────────────────────────────────

def show_credits(slack_client, user_id: str):
    """/크레딧조회 — 드림플러스 잔여 포인트"""
    try:
        jwt, pub_key = _get_session(user_id)
        data = dp.get_credits(jwt, pub_key)
    except ValueError as e:
        _post(slack_client, user_id, f"⚠️ {e}")
        return
    except dp.TokenExpiredError:
        user_store.save_dreamplus_jwt(user_id, "", "")
        _post(slack_client, user_id, "⚠️ 세션이 만료되었습니다. 다시 시도해주세요.")
        return
    except RuntimeError as e:
        msg = str(e)
        if "시스템 오류" in msg:
            _post(slack_client, user_id,
                  "⚠️ 드림플러스 포인트 조회 API가 현재 지원되지 않습니다.\n"
                  "잔여 포인트는 드림플러스 앱 또는 웹사이트에서 직접 확인해주세요.")
        else:
            _post(slack_client, user_id, f"❌ 크레딧 조회 실패: {msg}")
        return
    except Exception as e:
        _post(slack_client, user_id, f"❌ 크레딧 조회 실패: {e}")
        return

    # 응답 구조에 따라 포인트 필드 탐색
    balance = (data.get("balance") or data.get("point") or
               data.get("totalPoint") or data.get("remainPoint") or 0)
    _post(slack_client, user_id, f"💳 드림플러스 잔여 포인트: *{balance:,} pt*")


# ── 미팅 생성 연동 (auto_book_room) ──────────────────────────

def auto_book_room(slack_client, *, user_id: str, start_dt: datetime,
                   end_dt: datetime, title: str, attendee_count: int = 2):
    """미팅 생성 직후 자동 호출 — 회의실 추천 버튼 Slack 발송.
    드림플러스 계정 미설정 또는 오류 시 조용히 스킵.
    """
    creds = user_store.get_dreamplus_credentials(user_id)
    if not creds:
        return  # 계정 미설정 → 조용히 스킵

    try:
        jwt, pub_key = _get_session(user_id)
        rooms = dp.get_rooms(jwt)
    except Exception as e:
        log.warning(f"auto_book_room 회의실 조회 실패 (스킵): {e}")
        return

    recommended = _recommend_rooms(rooms, attendee_count, start_dt, end_dt)
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
    for room in recommended:
        blocks.append(_room_block(room, start_dt, end_dt, title))
    blocks.append({
        "type": "actions",
        "elements": [{
            "type": "button",
            "text": {"type": "plain_text", "text": "건너뜀"},
            "action_id": "dreamplus_skip_booking",
            "value": "skip",
        }],
    })
    _post(slack_client, user_id,
          f"드림플러스 회의실 예약 제안: {title}", blocks=blocks)
