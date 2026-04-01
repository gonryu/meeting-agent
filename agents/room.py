"""Room 에이전트 — Dreamplus 강남 회의실 예약 관리

주요 기능:
  - /dreamplus       : Slack Modal로 계정 등록
  - /회의실          : 날짜별 예약 현황 조회
  - /내예약          : 내 예약 목록
  - /회의실예약      : 자연어 예약 (LLM 파싱)
  - /예약취소        : 내 예약 선택 취소
  - /크레딧          : 남은 크레딧 조회

JWT 캐시:
  - 인메모리 dict(_jwt_cache)에 user_id → jwtToken 보관
  - API 호출 시 TokenExpiredError(code 301) 발생하면 자동 재로그인 1회 재시도
"""
import logging
import threading
from datetime import datetime, timedelta

import pytz

from store import user_store
from tools import dreamplus as dp

log = logging.getLogger(__name__)

KST = pytz.timezone("Asia/Seoul")

# 인메모리 JWT 캐시 (서버 재시작 시 초기화 → 자동 재로그인)
_jwt_cache: dict[str, str] = {}
_jwt_lock = threading.Lock()


# ── 내부 헬퍼 ────────────────────────────────────────────────

def _post(slack_client, *, user_id: str, text: str = None, blocks=None, **kw):
    kwargs = {"channel": user_id}
    if text:
        kwargs["text"] = text
    if blocks:
        kwargs["blocks"] = blocks
    kwargs.update(kw)
    return slack_client.chat_postMessage(**kwargs)


def _get_jwt(user_id: str) -> str:
    """JWT 반환. 캐시 없으면 DB 복호화 후 재로그인."""
    with _jwt_lock:
        if user_id in _jwt_cache:
            return _jwt_cache[user_id]

    creds = user_store.get_dreamplus_credentials(user_id)
    if not creds:
        raise NoDreamplusAccountError()

    email, password = creds
    jwt = dp.login(email, password)
    with _jwt_lock:
        _jwt_cache[user_id] = jwt
    return jwt


def _call(user_id: str, fn, *args, **kwargs):
    """JWT 자동갱신 래퍼. TokenExpiredError 시 재로그인 1회 재시도."""
    jwt = _get_jwt(user_id)
    try:
        return fn(jwt, *args, **kwargs)
    except dp.TokenExpiredError:
        log.info(f"JWT 만료, 재로그인: {user_id}")
        with _jwt_lock:
            _jwt_cache.pop(user_id, None)
        jwt = _get_jwt(user_id)
        return fn(jwt, *args, **kwargs)


def _check_dreamplus(slack_client, user_id: str) -> bool:
    """Dreamplus 미등록 시 안내 메시지 후 False 반환."""
    if user_store.has_dreamplus_credentials(user_id):
        return True
    _post(slack_client, user_id=user_id,
          text="⚠️ Dreamplus 계정이 연결되지 않았습니다.\n"
               "`/dreamplus` 명령으로 먼저 계정을 연결해주세요.")
    return False


def _today() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


# ── 계정 등록 ────────────────────────────────────────────────

def open_register_modal(slack_client, trigger_id: str):
    """Dreamplus 계정 등록 Modal 오픈"""
    slack_client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": "dreamplus_register_modal",
            "title": {"type": "plain_text", "text": "Dreamplus 계정 등록"},
            "submit": {"type": "plain_text", "text": "연결"},
            "close": {"type": "plain_text", "text": "취소"},
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Dreamplus 강남 로그인 계정을 입력해주세요.\n"
                                "비밀번호는 암호화되어 DB에 저장됩니다.",
                    },
                },
                {
                    "type": "input",
                    "block_id": "email_block",
                    "label": {"type": "plain_text", "text": "이메일"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "email_input",
                        "placeholder": {"type": "plain_text", "text": "user@company.com"},
                    },
                },
                {
                    "type": "input",
                    "block_id": "password_block",
                    "label": {"type": "plain_text", "text": "비밀번호"},
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "password_input",
                        "placeholder": {"type": "plain_text", "text": "비밀번호 입력"},
                    },
                    "hint": {
                        "type": "plain_text",
                        "text": "입력값은 Modal에서만 처리되며 채팅에 노출되지 않습니다.",
                    },
                },
            ],
        },
    )


def handle_register_modal(slack_client, user_id: str, view: dict):
    """Modal 제출 처리 — 로그인 테스트 후 저장"""
    values = view["state"]["values"]
    email = values["email_block"]["email_input"]["value"].strip()
    password = values["password_block"]["password_input"]["value"]

    def _register_bg():
        try:
            _post(slack_client, user_id=user_id,
                  text=f"🔐 *{email}* 계정으로 Dreamplus 로그인 테스트 중...")
            jwt = dp.login(email, password)
            user_store.save_dreamplus_credentials(user_id, email, password)
            with _jwt_lock:
                _jwt_cache[user_id] = jwt
            _post(slack_client, user_id=user_id,
                  text=f"✅ Dreamplus 계정 등록 완료!\n"
                       f"이메일: *{email}*\n"
                       f"이제 `/회의실`, `/내예약`, `/크레딧` 등을 사용할 수 있습니다.")
        except Exception as e:
            _post(slack_client, user_id=user_id,
                  text=f"❌ Dreamplus 로그인 실패: {e}\n"
                       f"이메일/비밀번호를 확인 후 `/dreamplus` 를 다시 시도해주세요.")

    threading.Thread(target=_register_bg, daemon=True).start()


# ── 예약 현황 조회 ───────────────────────────────────────────

def cmd_rooms(slack_client, user_id: str, date_str: str = None):
    """날짜별 회의실 예약 현황 조회"""
    if not _check_dreamplus(slack_client, user_id):
        return
    date_str = date_str or _today()

    _post(slack_client, user_id=user_id,
          text=f"🏢 *{date_str}* 회의실 현황을 조회 중입니다...")

    def _bg():
        try:
            rooms = _call(user_id, dp.get_rooms, date_str)
            reservations = _call(user_id, dp.get_reservations, date_str)

            # 예약된 room_id set
            reserved_ids = {str(r.get("meetingRoomId") or r.get("id", ""))
                            for r in reservations}

            # 층별 그룹핑
            by_floor: dict[str, list] = {}
            for room in rooms:
                floor = str(room.get("floor", "?"))
                by_floor.setdefault(floor, []).append(room)

            blocks = [
                {
                    "type": "header",
                    "text": {"type": "plain_text",
                             "text": f"🏢 {date_str} 회의실 예약 현황"},
                },
                {"type": "divider"},
            ]

            for floor in sorted(by_floor.keys(), key=lambda x: int(x) if x.isdigit() else 99):
                floor_rooms = by_floor[floor]
                lines = []
                for r in floor_rooms:
                    rid = str(r.get("id", ""))
                    name = r.get("name", rid)
                    cap = r.get("capacity", "?")
                    status = "🔴 예약됨" if rid in reserved_ids else "🟢 예약가능"
                    lines.append(f"{status}  *{name}* ({cap}명)")

                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn",
                             "text": f"*{floor}F*\n" + "\n".join(lines)},
                })
                blocks.append({"type": "divider"})

            if not rooms:
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "조회된 회의실이 없습니다."},
                })

            _post(slack_client, user_id=user_id,
                  text=f"{date_str} 회의실 현황", blocks=blocks)

        except Exception as e:
            log.error(f"회의실 현황 조회 실패 ({user_id}): {e}")
            _post(slack_client, user_id=user_id,
                  text=f"⚠️ 회의실 현황 조회 실패: {e}")

    threading.Thread(target=_bg, daemon=True).start()


# ── 내 예약 조회 ─────────────────────────────────────────────

def cmd_my_reservations(slack_client, user_id: str):
    """내 예약 목록 조회 (오늘 기준 향후 7일)"""
    if not _check_dreamplus(slack_client, user_id):
        return

    _post(slack_client, user_id=user_id, text="📋 내 예약 목록을 조회 중입니다...")

    def _bg():
        try:
            creds = user_store.get_dreamplus_credentials(user_id)
            my_email = creds[0] if creds else ""
            today = datetime.now(KST)

            all_reservations = []
            for i in range(7):
                date_str = (today + timedelta(days=i)).strftime("%Y-%m-%d")
                try:
                    reservations = _call(user_id, dp.get_reservations, date_str)
                    for r in reservations:
                        # 내 이메일로 예약된 건만 필터
                        reserver = (r.get("reserverEmail") or
                                    r.get("email") or
                                    r.get("memberEmail") or "")
                        if my_email and reserver and my_email.lower() != reserver.lower():
                            continue
                        all_reservations.append(r)
                except Exception:
                    pass

            if not all_reservations:
                _post(slack_client, user_id=user_id,
                      text="📋 향후 7일 내 예약이 없습니다.")
                return

            blocks = [
                {"type": "header",
                 "text": {"type": "plain_text", "text": "📋 내 예약 목록 (7일)"}},
                {"type": "divider"},
            ]
            for r in all_reservations:
                rid = str(r.get("id", ""))
                room_name = r.get("meetingRoomName") or r.get("roomName") or "?"
                start = r.get("startTime", "")
                end = r.get("endTime", "")
                title = r.get("title") or r.get("name") or "제목 없음"
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn",
                             "text": f"*{room_name}*\n"
                                     f"📅 {start} ~ {end}\n"
                                     f"제목: {title}"},
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "취소"},
                        "style": "danger",
                        "action_id": "room_cancel_reservation",
                        "value": rid,
                        "confirm": {
                            "title": {"type": "plain_text", "text": "예약 취소"},
                            "text": {"type": "mrkdwn",
                                     "text": f"*{room_name}* {start} 예약을 취소하시겠습니까?"},
                            "confirm": {"type": "plain_text", "text": "취소"},
                            "deny": {"type": "plain_text", "text": "돌아가기"},
                        },
                    },
                })
                blocks.append({"type": "divider"})

            _post(slack_client, user_id=user_id,
                  text="내 예약 목록", blocks=blocks)

        except Exception as e:
            log.error(f"내 예약 조회 실패 ({user_id}): {e}")
            _post(slack_client, user_id=user_id,
                  text=f"⚠️ 내 예약 조회 실패: {e}")

    threading.Thread(target=_bg, daemon=True).start()


# ── 예약 ─────────────────────────────────────────────────────

def cmd_reserve(slack_client, user_id: str, params: dict):
    """회의실 예약.
    params: {date, start_time, end_time, floor, capacity, title}
    """
    if not _check_dreamplus(slack_client, user_id):
        return

    date_str = params.get("date") or _today()
    start_time = params.get("start_time", "09:00")
    end_time = params.get("end_time", "10:00")
    title = params.get("title", "회의")
    floor = params.get("floor")
    capacity = params.get("capacity", 0)

    _post(slack_client, user_id=user_id,
          text=f"🔍 *{date_str} {start_time}~{end_time}* 가용 회의실 탐색 중...")

    def _bg():
        try:
            rooms = _call(user_id, dp.get_rooms, date_str)
            reservations = _call(user_id, dp.get_reservations, date_str)

            # 요청 시간대에 예약된 room_id set
            req_start = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M")
            req_end = datetime.strptime(f"{date_str} {end_time}", "%Y-%m-%d %H:%M")

            def _overlaps(r):
                try:
                    rs = datetime.strptime(r.get("startTime", ""), "%Y.%m.%d %H:%M:%S")
                    re_ = datetime.strptime(r.get("endTime", ""), "%Y.%m.%d %H:%M:%S")
                    return rs < req_end and re_ > req_start
                except Exception:
                    return False

            booked_ids = {str(r.get("meetingRoomId") or r.get("id", ""))
                          for r in reservations if _overlaps(r)}

            # 가용 회의실 필터
            available = []
            for room in rooms:
                rid = str(room.get("id", ""))
                if rid in booked_ids:
                    continue
                if floor and str(room.get("floor", "")) != str(floor):
                    continue
                if capacity and (room.get("capacity") or 0) < capacity:
                    continue
                available.append(room)

            if not available:
                _post(slack_client, user_id=user_id,
                      text=f"😞 *{date_str} {start_time}~{end_time}* 에 사용 가능한 회의실이 없습니다.")
                return

            # 예약 선택 버튼 표시
            blocks = [
                {"type": "header",
                 "text": {"type": "plain_text",
                          "text": f"🟢 가용 회의실 — {date_str} {start_time}~{end_time}"}},
                {"type": "divider"},
            ]
            for room in available[:10]:  # 최대 10개
                rid = str(room.get("id", ""))
                name = room.get("name", rid)
                cap = room.get("capacity", "?")
                floor_label = room.get("floor", "?")
                value = f"{rid}|{date_str}|{start_time}|{end_time}|{title}"
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn",
                             "text": f"*{name}* ({floor_label}F, {cap}명)"},
                    "accessory": {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "예약"},
                        "style": "primary",
                        "action_id": "room_confirm_reservation",
                        "value": value,
                    },
                })

            _post(slack_client, user_id=user_id,
                  text="가용 회의실 목록", blocks=blocks)

        except Exception as e:
            log.error(f"가용 회의실 탐색 실패 ({user_id}): {e}")
            _post(slack_client, user_id=user_id,
                  text=f"⚠️ 가용 회의실 탐색 실패: {e}")

    threading.Thread(target=_bg, daemon=True).start()


def handle_confirm_reservation(slack_client, user_id: str, value: str):
    """예약 확정 버튼 핸들러"""
    try:
        parts = value.split("|")
        room_id, date_str, start_time, end_time, title = parts
    except ValueError:
        _post(slack_client, user_id=user_id, text="⚠️ 잘못된 예약 정보입니다.")
        return

    def _bg():
        try:
            start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y-%m-%d %H:%M")
            end_dt = datetime.strptime(f"{date_str} {end_time}", "%Y-%m-%d %H:%M")
            result = _call(user_id, dp.make_reservation, room_id, start_dt, end_dt, title)
            room_name = result.get("meetingRoomName") or result.get("name") or room_id
            _post(slack_client, user_id=user_id,
                  text=f"✅ 예약 완료!\n"
                       f"📍 회의실: *{room_name}*\n"
                       f"📅 일시: {date_str} {start_time} ~ {end_time}\n"
                       f"📋 제목: {title}")
        except Exception as e:
            log.error(f"예약 확정 실패 ({user_id}): {e}")
            _post(slack_client, user_id=user_id, text=f"⚠️ 예약 실패: {e}")

    threading.Thread(target=_bg, daemon=True).start()


def handle_cancel_reservation(slack_client, user_id: str, reservation_id: str):
    """예약 취소 버튼 핸들러"""
    def _bg():
        try:
            _call(user_id, dp.cancel_reservation, reservation_id)
            _post(slack_client, user_id=user_id,
                  text=f"✅ 예약(ID: {reservation_id})이 취소되었습니다.")
        except Exception as e:
            log.error(f"예약 취소 실패 ({user_id}): {e}")
            _post(slack_client, user_id=user_id, text=f"⚠️ 예약 취소 실패: {e}")

    threading.Thread(target=_bg, daemon=True).start()


# ── 크레딧 조회 ──────────────────────────────────────────────

def cmd_credits(slack_client, user_id: str):
    """남은 크레딧(포인트) 조회"""
    if not _check_dreamplus(slack_client, user_id):
        return

    def _bg():
        try:
            data = _call(user_id, dp.get_credits)
            point = data.get("point") or data.get("balance") or data.get("remainPoint") or 0
            used = data.get("usedPoint") or data.get("used") or 0
            _post(slack_client, user_id=user_id,
                  text=f"💳 *Dreamplus 회의실 크레딧*\n"
                       f"• 잔여: *{point:,}* 포인트\n"
                       f"• 사용: {used:,} 포인트")
        except Exception as e:
            log.error(f"크레딧 조회 실패 ({user_id}): {e}")
            _post(slack_client, user_id=user_id, text=f"⚠️ 크레딧 조회 실패: {e}")

    threading.Thread(target=_bg, daemon=True).start()


# ── 예외 ─────────────────────────────────────────────────────

class NoDreamplusAccountError(Exception):
    """Dreamplus 계정 미등록"""
    pass
