"""Dreamplus 강남 회의실 예약 시스템 API 클라이언트

인증 흐름:
  1. POST /auth/publickey  → RSA 공개키 수령
  2. 비밀번호를 RSA-PKCS1v15로 암호화
  3. POST /auth/login      → jwtToken 수령
  4. 이후 모든 요청 헤더: Authorization: Bearer <jwtToken>

JWT 만료 시 code=301 반환 → 호출 측에서 재로그인 처리
"""
import base64
import logging
import re
from datetime import datetime, timedelta

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger(__name__)

_BASE = "https://gangnam.dreamplus.asia"
_HEADERS = {
    "Content-Type": "application/json",
    "Origin": _BASE,
    "Referer": f"{_BASE}/login",
}
_TIMEOUT = 15


# ── 내부 헬퍼 ────────────────────────────────────────────────

def _post(path: str, data: dict, jwt: str = None) -> dict:
    headers = dict(_HEADERS)
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    resp = requests.post(
        f"{_BASE}{path}",
        json={"data": data},
        headers=headers,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _get(path: str, jwt: str = None) -> dict:
    headers = dict(_HEADERS)
    if jwt:
        headers["Authorization"] = f"Bearer {jwt}"
    resp = requests.get(
        f"{_BASE}{path}",
        headers=headers,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _fmt(dt: datetime) -> str:
    """datetime → 'YYYY.MM.DD HH:MM:SS' (Dreamplus 날짜 형식)"""
    return dt.strftime("%Y.%m.%d %H:%M:%S")


def _day_range(date_str: str) -> tuple[str, str]:
    """'YYYY-MM-DD' → (start, end) Dreamplus 형식"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    start = _fmt(d.replace(hour=0, minute=0, second=0))
    end = _fmt(d.replace(hour=23, minute=59, second=59))
    return start, end


# ── 인증 ─────────────────────────────────────────────────────

def get_public_key() -> str:
    """RSA 공개키 PEM 문자열 반환"""
    resp = requests.post(
        f"{_BASE}/auth/publickey",
        json={},
        headers=_HEADERS,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    if not body.get("result"):
        raise RuntimeError(f"공개키 조회 실패: {body.get('message')}")
    return body["data"]["publicKey"]


def _encrypt_password(password: str, public_key_pem: str) -> str:
    """RSA PKCS1v15로 비밀번호 암호화 → Base64 반환"""
    # PEM 형식 정규화 (헤더/푸터 없는 경우 추가)
    pem = public_key_pem.strip()
    if not pem.startswith("-----"):
        pem = f"-----BEGIN PUBLIC KEY-----\n{pem}\n-----END PUBLIC KEY-----"
    pub_key = serialization.load_pem_public_key(pem.encode())
    encrypted = pub_key.encrypt(password.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode("utf-8")


def login(email: str, password: str) -> str:
    """로그인 후 jwtToken 반환. 실패 시 RuntimeError."""
    pub_key_pem = get_public_key()
    log.info(f"Dreamplus 공개키 수령 완료 (길이={len(pub_key_pem)})")

    enc_password = _encrypt_password(password, pub_key_pem)
    log.info(f"Dreamplus 비밀번호 암호화 완료 (encrypted 길이={len(enc_password)})")

    payload = {
        "email": email,
        "password": enc_password,
        "publicKey": pub_key_pem,
    }
    log.info(f"Dreamplus /auth/login 요청 → email={email}, publicKey 길이={len(pub_key_pem)}")

    # /auth/login 은 {"data": {...}} 래핑 없이 직접 전송
    resp = requests.post(
        f"{_BASE}/auth/login",
        json=payload,
        headers=_HEADERS,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    log.info(f"Dreamplus /auth/login 응답 → result={body.get('result')}, code={body.get('code')}, message={body.get('message')}")

    if not body.get("result"):
        raise RuntimeError(f"로그인 실패: {body.get('message', '알 수 없는 오류')}")

    jwt = body.get("data", {}).get("jwtToken")
    if not jwt:
        raise RuntimeError("jwtToken을 응답에서 찾을 수 없습니다.")

    log.info(f"Dreamplus 로그인 성공: {email}")
    return jwt


def is_token_expired(response_body: dict) -> bool:
    """응답 코드 301이면 JWT 만료"""
    return str(response_body.get("code")) == "301"


# ── 회의실 API ────────────────────────────────────────────────

def get_rooms(jwt: str, date_str: str) -> list[dict]:
    """날짜별 회의실 목록 반환.
    date_str: 'YYYY-MM-DD'
    """
    body = _post("/api2/meetingrooms", {"date": date_str}, jwt=jwt)
    if is_token_expired(body):
        raise TokenExpiredError()
    if not body.get("result"):
        raise RuntimeError(f"회의실 목록 조회 실패: {body.get('message')}")
    return body.get("data") or []


def get_reservations(jwt: str, date_str: str) -> list[dict]:
    """날짜별 전체 예약 목록 반환.
    date_str: 'YYYY-MM-DD'
    """
    start, end = _day_range(date_str)
    body = _post("/api2/meetingroom/reservations", {
        "searchType": "startTime",
        "cancelDate": start,
        "startTime": start,
        "endTime": end,
    }, jwt=jwt)
    if is_token_expired(body):
        raise TokenExpiredError()
    if not body.get("result"):
        raise RuntimeError(f"예약 목록 조회 실패: {body.get('message')}")
    return body.get("data") or []


def make_reservation(jwt: str, room_id: str, start_dt: datetime,
                     end_dt: datetime, title: str = "회의") -> dict:
    """회의실 예약 생성. 예약 결과 dict 반환."""
    body = _post("/api2/meetingroom/reservation", {
        "meetingRoomId": room_id,
        "centerId": "1",
        "startTime": _fmt(start_dt),
        "endTime": _fmt(end_dt),
        "title": title,
    }, jwt=jwt)
    if is_token_expired(body):
        raise TokenExpiredError()
    if not body.get("result"):
        raise RuntimeError(f"예약 실패: {body.get('message')}")
    return body.get("data") or {}


def cancel_reservation(jwt: str, reservation_id: str) -> bool:
    """예약 취소. 성공 시 True."""
    body = _post(f"/api2/meetingroom/refund/{reservation_id}", {}, jwt=jwt)
    if is_token_expired(body):
        raise TokenExpiredError()
    if not body.get("result"):
        raise RuntimeError(f"예약 취소 실패: {body.get('message')}")
    return True


def get_credits(jwt: str) -> dict:
    """남은 크레딧(포인트) 조회."""
    body = _get("/api2/invoice/point/meetingroom", jwt=jwt)
    if is_token_expired(body):
        raise TokenExpiredError()
    if not body.get("result"):
        raise RuntimeError(f"크레딧 조회 실패: {body.get('message')}")
    return body.get("data") or {}


# ── 예외 ─────────────────────────────────────────────────────

class TokenExpiredError(Exception):
    """JWT 만료 — 재로그인 필요"""
    pass
