"""Google Calendar API 래퍼"""
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import logging
import os

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]

INTERNAL_DOMAINS = set(os.getenv("INTERNAL_DOMAINS", "parametacorp.com,iconloop.com").split(","))


def _service(creds: Credentials):
    return build("calendar", "v3", credentials=creds)


def get_upcoming_meetings(creds: Credentials, days: int = 1, from_now: bool = False) -> list[dict]:
    """캘린더 이벤트 조회.

    Args:
        days: 조회 범위 (일 수). from_now=True 시 현재 시각 기준, False 시 오늘 자정 기준.
        from_now: True면 지금 이 순간부터 days*24h 이내 이벤트 반환.
    """
    from zoneinfo import ZoneInfo
    kst = ZoneInfo("Asia/Seoul")
    now_kst = datetime.now(kst)

    if from_now:
        time_min = now_kst
        time_max = now_kst + timedelta(days=days)
    else:
        time_min = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
        time_max = time_min + timedelta(days=days)

    result = _service(creds).events().list(
        calendarId="primary",
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()
    items = result.get("items", [])

    # 종일 이벤트이면서 제목이 '집' 또는 '사무실'인 것은 근무위치 표시용 → 처리 제외
    _LOCATION_TITLES = {"집", "사무실"}
    items = [
        ev for ev in items
        if not (
            ev.get("start", {}).get("dateTime") is None       # 종일 이벤트
            and ev.get("summary", "").strip() in _LOCATION_TITLES
        )
    ]

    # from_now=True 시 Google API는 종료시각 기준으로 필터링하므로
    # 이미 시작된 이벤트가 포함될 수 있음 → 시작 시각 기준으로 재필터링
    if from_now:
        filtered = []
        for ev in items:
            start_str = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
            try:
                start_dt = datetime.fromisoformat(start_str) if start_str else None
                if start_dt is None:
                    filtered.append(ev)
                    continue
                if start_dt.tzinfo is None:
                    start_dt = start_dt.replace(tzinfo=now_kst.tzinfo)
                log.info(f"get_upcoming_meetings filter: '{ev.get('summary','')}' start={start_dt} now={now_kst} include={start_dt >= now_kst}")
                if start_dt >= now_kst:
                    filtered.append(ev)
            except (ValueError, TypeError):
                filtered.append(ev)
        items = filtered

    return items


def parse_event(event: dict) -> dict:
    """이벤트에서 필요한 필드 추출"""
    attendees = event.get("attendees", [])
    start = event.get("start", {})
    return {
        "id": event.get("id"),
        "summary": event.get("summary", "(제목 없음)"),
        "start_time": start.get("dateTime", start.get("date")),
        "location": event.get("location", ""),
        "meet_link": event.get("hangoutLink", ""),
        "description": event.get("description", ""),
        "attendees": [
            {"email": a.get("email", ""), "name": a.get("displayName", "")}
            for a in attendees
            if not a.get("self", False)
        ],
    }


def classify_meeting(event: dict, company_names: list[str]) -> str:
    """
    외부/내부 미팅 분류
    Returns: 'external' | 'internal'
    """
    parsed = parse_event(event)

    # 1순위: 참석자 이메일 도메인
    for attendee in parsed["attendees"]:
        domain = attendee["email"].split("@")[-1]
        if domain and domain not in INTERNAL_DOMAINS:
            return "external"

    # 2순위: 제목에 알려진 업체명 포함
    summary_lower = parsed["summary"].lower()
    for company in company_names:
        if company.lower() in summary_lower:
            return "external"

    return "internal"


def create_event(creds: Credentials, summary: str, start_dt: datetime,
                 end_dt: datetime, attendee_emails: list[str], description: str = "") -> dict:
    """캘린더 이벤트 생성"""
    event_body = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Seoul"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Seoul"},
        "attendees": [{"email": e} for e in attendee_emails],
        "conferenceData": {
            "createRequest": {"requestId": f"meet-{start_dt.timestamp()}"}
        },
    }
    return _service(creds).events().insert(
        calendarId="primary",
        body=event_body,
        conferenceDataVersion=1,
        sendUpdates="all",
    ).execute()


def get_recently_ended_meetings(creds: Credentials,
                                min_minutes_ago: int = 10,
                                max_minutes_ago: int = 90) -> list[dict]:
    """
    종료된 지 min~max분 사이의 미팅 목록 반환 (트랜스크립트 폴링용).
    Returns: [parsed_event, ...]
    """
    from zoneinfo import ZoneInfo
    kst = ZoneInfo("Asia/Seoul")
    now = datetime.now(kst)

    time_max = now - timedelta(minutes=min_minutes_ago)
    time_min = now - timedelta(minutes=max_minutes_ago)

    result = _service(creds).events().list(
        calendarId="primary",
        timeMin=time_min.isoformat(),
        timeMax=time_max.isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    meetings = []
    for ev in result.get("items", []):
        end = ev.get("end", {})
        end_str = end.get("dateTime", end.get("date"))
        if not end_str:
            continue
        parsed = parse_event(ev)
        parsed["end_time"] = end_str
        meetings.append(parsed)
    return meetings


def get_event_attendees(creds: Credentials, event_id: str) -> list[dict]:
    """캘린더 이벤트에서 외부 참석자 이름+이메일 반환 (내부 도메인 제외)
    Returns: [{"name": "...", "email": "..."}]
    """
    event = _service(creds).events().get(
        calendarId="primary", eventId=event_id
    ).execute()
    result = []
    for a in event.get("attendees", []):
        if a.get("self", False):
            continue
        email = a.get("email", "")
        domain = email.split("@")[-1] if "@" in email else ""
        if domain and domain in INTERNAL_DOMAINS:
            continue
        result.append({"name": a.get("displayName", ""), "email": email})
    return result


def update_event_description(creds: Credentials, event_id: str, description: str) -> dict:
    """이벤트 설명란 업데이트 (어젠다 등록)"""
    return _service(creds).events().patch(
        calendarId="primary",
        eventId=event_id,
        body={"description": description},
        sendUpdates="all",
    ).execute()


def enable_meet_transcription(creds: Credentials, conference_id: str) -> bool:
    """Google Meet 스페이스의 트랜스크립트 + Gemini 회의록 자동 작성을 활성화.
    meetings.space.created 스코프 필요 — 미부여 시 False 반환.

    google-api-python-client 대신 requests 직접 호출 (회사 방화벽 SSL 우회).
    1차 시도: 트랜스크립트 + Gemini 회의록(meetingNoteConfig) 동시 활성화
    2차 시도(fallback): 트랜스크립트만 활성화
    """
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # 액세스 토큰 갱신
    try:
        import google.auth.transport.requests as ga_requests
        creds.refresh(ga_requests.Request())
    except Exception as e:
        log.warning(f"Meet 토큰 갱신 실패: {e}")
        return False

    token = creds.token
    space_name = f"spaces/{conference_id}"
    url = f"https://meet.googleapis.com/v2beta/{space_name}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # 1차: 트랜스크립트 + Gemini 자동 회의록 동시 활성화
    try:
        resp = requests.patch(
            url,
            params={"updateMask": "config.transcriptionConfig,config.meetingNoteConfig"},
            json={"config": {
                "transcriptionConfig": {"state": "ON"},
                "meetingNoteConfig": {"state": "ON"},
            }},
            headers=headers,
            verify=False,
            timeout=15,
        )
        if resp.ok:
            log.info(f"Meet 트랜스크립트 + Gemini 회의록 자동 작성 활성화: {space_name}")
            return True
        log.warning(f"Gemini 회의록 설정 실패 ({resp.status_code}), 트랜스크립트만 시도: {resp.text}")
    except Exception as e1:
        log.warning(f"Gemini 회의록 설정 요청 실패: {e1}")

    # 2차 fallback: 트랜스크립트만 활성화
    try:
        resp2 = requests.patch(
            url,
            params={"updateMask": "config.transcriptionConfig"},
            json={"config": {"transcriptionConfig": {"state": "ON"}}},
            headers=headers,
            verify=False,
            timeout=15,
        )
        if resp2.ok:
            log.info(f"Meet 트랜스크립트 활성화 (Gemini 회의록 미지원): {space_name}")
            return True
        log.warning(f"Meet 트랜스크립트 활성화 실패 ({resp2.status_code}): {resp2.text}")
    except Exception as e2:
        log.warning(f"Meet 트랜스크립트 요청 실패: {e2}")

    return False


def update_event(creds: Credentials, event_id: str, *,
                 summary: str = None, start_dt: datetime = None,
                 end_dt: datetime = None, attendee_emails: list[str] = None,
                 description: str = None, location: str = None) -> dict:
    """캘린더 이벤트 부분 업데이트 (patch). None인 필드는 변경하지 않음."""
    body = {}
    if summary is not None:
        body["summary"] = summary
    if start_dt is not None:
        body["start"] = {"dateTime": start_dt.isoformat(), "timeZone": "Asia/Seoul"}
    if end_dt is not None:
        body["end"] = {"dateTime": end_dt.isoformat(), "timeZone": "Asia/Seoul"}
    if attendee_emails is not None:
        body["attendees"] = [{"email": e} for e in attendee_emails]
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    return _service(creds).events().patch(
        calendarId="primary",
        eventId=event_id,
        body=body,
        sendUpdates="all",
    ).execute()
