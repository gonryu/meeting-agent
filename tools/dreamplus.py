"""Dreamplus 강남 회의실 예약 시스템 API 클라이언트

API 문서 기준 (lib/dreamplus-apis.md):
  - Authorization 헤더: jwtToken 직접 전달 (Bearer 접두사 없음)
  - 날짜 형식: yyyy.MM.dd HH:mm:ss
  - 취소/크레딧 등 일부 API는 ek/ed 하이브리드 암호화 사용

인증 흐름:
  1. POST /auth/publickey  → RSA 공개키 수령
  2. 비밀번호를 RSA-PKCS1v15로 암호화
  3. POST /auth/login      → jwtToken + 공개키 수령
  4. 이후 모든 요청 헤더: authorization: <jwtToken>

JWT 만료 시 code=301 반환 → 호출 측에서 재로그인 처리
"""
import base64
import json
import logging
import os

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from datetime import datetime

log = logging.getLogger(__name__)

_BASE = "https://gangnam.dreamplus.asia"
_HEADERS = {
    "Content-Type": "application/json; charset=UTF-8",
    "Origin": _BASE,
    "Referer": f"{_BASE}/login",
}
_TIMEOUT = 15

# 서버 환경용 고정 핑거프린트 (브라우저 없는 환경에서 재사용 가능한 고정값)
_FINGERPRINT = "4745c59ebd0b08cd01973b42fe0d3db3"


# ── 날짜 형식 ─────────────────────────────────────────────────

def _fmt(dt: datetime) -> str:
    """datetime → 'YYYY.MM.DD HH:MM:SS'"""
    return dt.strftime("%Y.%m.%d %H:%M:%S")


def _day_range(date_str: str) -> tuple[str, str]:
    """'YYYY-MM-DD' → (start, end) Dreamplus 형식"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return (
        _fmt(d.replace(hour=0, minute=0, second=0)),
        _fmt(d.replace(hour=23, minute=59, second=59)),
    )


# ── HTTP 헬퍼 ────────────────────────────────────────────────

def _post(path: str, data: dict, jwt: str = None) -> dict:
    headers = dict(_HEADERS)
    if jwt:
        headers["authorization"] = jwt
    resp = requests.post(
        f"{_BASE}{path}",
        json=data,
        headers=headers,
        timeout=_TIMEOUT,
        verify=False,
    )
    resp.raise_for_status()
    # 일부 API는 빈 body 반환
    if not resp.content.strip():
        return {"result": True, "code": "200"}
    return resp.json()


def _delete(path: str, data: dict, jwt: str) -> dict:
    headers = dict(_HEADERS)
    headers["authorization"] = jwt
    resp = requests.delete(
        f"{_BASE}{path}",
        json=data,
        headers=headers,
        timeout=_TIMEOUT,
        verify=False,
    )
    resp.raise_for_status()
    if not resp.content.strip():
        return {"result": True, "code": "200"}
    return resp.json()


def _get(path: str, jwt: str = None) -> dict:
    headers = dict(_HEADERS)
    if jwt:
        headers["authorization"] = jwt
    resp = requests.get(
        f"{_BASE}{path}",
        headers=headers,
        timeout=_TIMEOUT,
        verify=False,
    )
    resp.raise_for_status()
    return resp.json()


# ── ek/ed 하이브리드 암호화 ───────────────────────────────────

def _encrypt_ek_ed(data: dict, public_key_b64: str) -> dict:
    """AES-256-CTR + RSA-1024 PKCS1v15 하이브리드 암호화.
    API 문서 lib/dreamplus-apis.md 암호화 섹션 참조.

    Returns: {"ek": str, "ed": str}
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    # 1. AES 키 생성: 24 random bytes → base64 → 32자 문자열
    key_bytes = os.urandom(24)
    key_str = base64.b64encode(key_bytes).decode("ascii")  # 32자

    # 2. ek: RSA PKCS1v15로 AES 키 암호화
    pem = f"-----BEGIN PUBLIC KEY-----\n{public_key_b64}\n-----END PUBLIC KEY-----"
    pub_key = serialization.load_pem_public_key(pem.encode())
    ek = base64.b64encode(
        pub_key.encrypt(key_str.encode("ascii"), padding.PKCS1v15())
    ).decode("ascii")

    # 3. ed: AES-256-CTR으로 데이터 암호화
    iv = os.urandom(16)
    plaintext = json.dumps(data, ensure_ascii=False).encode("utf-8")
    cipher = Cipher(
        algorithms.AES(key_str.encode("ascii")),
        modes.CTR(iv),
        backend=default_backend(),
    )
    enc = cipher.encryptor()
    ct = enc.update(plaintext) + enc.finalize()
    ed = base64.b64encode(iv + ct).decode("ascii")

    return {"ek": ek, "ed": ed}


# ── 인증 ─────────────────────────────────────────────────────

class TokenExpiredError(Exception):
    """JWT 만료 (code=301) — 재로그인 필요"""
    pass


def _check(body: dict) -> dict:
    """응답 오류 체크. 301이면 TokenExpiredError, 그 외 실패면 RuntimeError."""
    if str(body.get("code")) == "301":
        raise TokenExpiredError("JWT 만료")
    if body.get("result") is False:
        raise RuntimeError(body.get("message", "API 오류"))
    return body


def get_public_key() -> str:
    """RSA 공개키 Base64 문자열 반환"""
    resp = requests.post(
        f"{_BASE}/auth/publickey",
        data=b"",
        headers=_HEADERS,
        timeout=_TIMEOUT,
        verify=False,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("result"):
        raise RuntimeError(f"공개키 조회 실패: {body.get('message')}")
    return body["data"]["publicKey"]


def _encrypt_password(password: str, public_key_b64: str) -> str:
    """RSA PKCS1v15로 비밀번호 암호화 → Base64"""
    pem = f"-----BEGIN PUBLIC KEY-----\n{public_key_b64}\n-----END PUBLIC KEY-----"
    pub_key = serialization.load_pem_public_key(pem.encode())
    encrypted = pub_key.encrypt(password.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("utf-8")


def login(email: str, password: str) -> tuple[str, str]:
    """로그인 → (jwtToken, publicKey) 반환. 실패 시 RuntimeError.

    API 문서 필드: email, password(RSA 암호화), finger_print, decryptRSA, publicKey
    """
    pub_key_b64 = get_public_key()
    enc_password = _encrypt_password(password, pub_key_b64)

    payload = {
        "email": email,
        "password": enc_password,
        "finger_print": _FINGERPRINT,
        "decryptRSA": 1,
        "publicKey": pub_key_b64,
    }
    resp = requests.post(
        f"{_BASE}/auth/login",
        json=payload,
        headers=_HEADERS,
        timeout=_TIMEOUT,
        verify=False,
    )
    resp.raise_for_status()
    body = resp.json()

    if not body.get("result"):
        raise RuntimeError(f"로그인 실패: {body.get('message', '알 수 없는 오류')}")

    jwt = body["data"]["jwtToken"]
    if not jwt:
        raise RuntimeError("jwtToken을 응답에서 찾을 수 없습니다.")

    member_id = body["data"].get("id", 0)
    company_id = body["data"].get("companyId", 0)
    log.info(f"Dreamplus 로그인 성공: {email} (memberId={member_id}, companyId={company_id})")
    return jwt, pub_key_b64, member_id, company_id


# ── 회의실 API ────────────────────────────────────────────────

def get_rooms(jwt: str) -> list[dict]:
    """전체 회의실 목록 반환 (roomCode, roomName, floor, maxMember, point 포함)"""
    body = _check(_post("/api2/meetingrooms", {}, jwt=jwt))
    return body.get("list") or []


def get_available(jwt: str, start_dt: datetime, end_dt: datetime) -> list[dict]:
    """시간대별 가용 현황 반환 (maxMember, count, usedMinutes, totalMinutes)"""
    body = _check(_post("/api2/meetingroom/daily", {
        "startTime": _fmt(start_dt),
        "endTime": _fmt(end_dt),
    }, jwt=jwt))
    return body.get("list") or []


def make_reservation(jwt: str, room_code: int, start_dt: datetime,
                     end_dt: datetime, title: str = "회의") -> bool:
    """회의실 예약 생성. 성공 시 True.

    API: POST /api2/meetingroom/reservation (평문, 암호화 없음)
    Fields: roomCode, startTime, endTime, title
    """
    _check(_post("/api2/meetingroom/reservation", {
        "roomCode": room_code,
        "startTime": _fmt(start_dt),
        "endTime": _fmt(end_dt),
        "title": title,
    }, jwt=jwt))
    return True


def get_reservations(jwt: str, date_str: str = None,
                     company_id: int = None) -> list[dict]:
    """예약 목록 조회.

    date_str 지정: 당일 전체 예약 (패턴 A)
    company_id 지정: 회사별 이번 달 예약 (패턴 B)
    """
    if date_str:
        start, end = _day_range(date_str)
        payload = {
            "data": {
                "searchType": "startTime",
                "cancelDate": start,
                "startTime": start,
                "endTime": end,
            }
        }
    else:
        from datetime import date
        today = date.today()
        month_start = today.replace(day=1).strftime("%Y.%m.%d")
        month_end = today.strftime("%Y.%m.%d")
        payload = {
            "page": 1,
            "size": 1000000,
            "date1": month_start,
            "date2": month_end,
            "order": "mr.start_time asc",
            "data": {
                "companyId": company_id,
                "searchType": "insertDate",
            },
            "searchType": "startTime",
        }
    raw = _post("/api2/meetingroom/reservations", payload, jwt=jwt)
    body = _check(raw)
    return body.get("list") or []


def get_refund_info(jwt: str, reservation_id: int) -> dict:
    """환불 정보 조회 (취소 전 환불 금액 확인용)"""
    body = _check(_get(f"/api2/meetingroom/refund/{reservation_id}", jwt=jwt))
    return body.get("data") or {}


def cancel_reservation(jwt: str, public_key_b64: str, reservation_id: int) -> bool:
    """예약 취소. ek/ed 암호화 사용.

    API: DELETE /api2/meetingroom/reservation
    Body: ek/ed 암호화된 {"id": reservation_id}
    """
    # 암호화 없이 시도
    raw = _delete("/api2/meetingroom/reservation", {"id": reservation_id}, jwt=jwt)
    log.info(f"[cancel_reservation] plain id={reservation_id} raw={raw}")
    if raw.get("result") is not False:
        _check(raw)
        return True
    # 실패 시 ek/ed 암호화 시도
    encrypted = _encrypt_ek_ed({"id": reservation_id}, public_key_b64)
    raw = _delete("/api2/meetingroom/reservation", encrypted, jwt=jwt)
    log.info(f"[cancel_reservation] encrypted id={reservation_id} raw={raw}")
    _check(raw)
    return True


def get_credits(jwt: str, public_key_b64: str) -> dict:
    """남은 크레딧(포인트) 조회.

    API: POST /api2/invoice/point  (ek/ed 암호화)
    """
    encrypted = _encrypt_ek_ed({}, public_key_b64)
    raw = _post("/api2/invoice/point", encrypted, jwt=jwt)
    body = _check(raw)
    return body.get("data") or body
