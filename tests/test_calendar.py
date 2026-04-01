"""tools/calendar.py 단위 테스트"""
import os
os.environ.setdefault("INTERNAL_DOMAINS", "parametacorp.com,iconloop.com")

import pytest
from unittest.mock import patch, MagicMock

# _service() 호출 차단 (token.json 불필요)
with patch("tools.calendar._service"):
    from tools.calendar import parse_event, classify_meeting


# ── parse_event ──────────────────────────────────────────────

class TestParseEvent:
    def test_full_event(self):
        event = {
            "id": "evt001",
            "summary": "카카오 미팅",
            "start": {"dateTime": "2026-03-24T15:00:00+09:00"},
            "location": "강남구 역삼동",
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
            "description": "파트너십 논의",
            "attendees": [
                {"email": "kim@kakao.com", "displayName": "김민환"},
                {"email": "me@parametacorp.com", "self": True},
            ],
        }
        result = parse_event(event)
        assert result["id"] == "evt001"
        assert result["summary"] == "카카오 미팅"
        assert result["start_time"] == "2026-03-24T15:00:00+09:00"
        assert result["location"] == "강남구 역삼동"
        assert result["meet_link"] == "https://meet.google.com/abc-defg-hij"
        assert result["description"] == "파트너십 논의"
        assert len(result["attendees"]) == 1
        assert result["attendees"][0]["email"] == "kim@kakao.com"

    def test_self_attendee_excluded(self):
        """self=True 참석자는 attendees에서 제외"""
        event = {
            "start": {"dateTime": "2026-03-24T10:00:00+09:00"},
            "attendees": [
                {"email": "me@parametacorp.com", "self": True},
                {"email": "other@kakao.com", "displayName": "홍길동"},
            ],
        }
        result = parse_event(event)
        assert len(result["attendees"]) == 1
        assert result["attendees"][0]["email"] == "other@kakao.com"

    def test_all_day_event(self):
        """종일 이벤트: start.date만 있을 때 start_time에 날짜 문자열"""
        event = {"start": {"date": "2026-03-24"}}
        result = parse_event(event)
        assert result["start_time"] == "2026-03-24"

    def test_no_summary(self):
        """제목 없는 이벤트 → 기본값 '(제목 없음)'"""
        event = {"start": {"dateTime": "2026-03-24T09:00:00+09:00"}}
        result = parse_event(event)
        assert result["summary"] == "(제목 없음)"

    def test_no_attendees(self):
        """참석자 없는 이벤트 → 빈 리스트"""
        event = {"start": {"dateTime": "2026-03-24T09:00:00+09:00"}}
        result = parse_event(event)
        assert result["attendees"] == []

    def test_missing_optional_fields(self):
        """location, hangoutLink, description 없을 때 빈 문자열"""
        event = {"start": {"dateTime": "2026-03-24T09:00:00+09:00"}}
        result = parse_event(event)
        assert result["location"] == ""
        assert result["meet_link"] == ""
        assert result["description"] == ""


# ── classify_meeting ─────────────────────────────────────────

class TestClassifyMeeting:
    def _make_event(self, summary="팀 회의", attendees=None):
        return {
            "summary": summary,
            "start": {"dateTime": "2026-03-24T10:00:00+09:00"},
            "attendees": attendees or [],
        }

    def test_external_attendee_domain(self):
        """외부 참석자 도메인 → external"""
        event = self._make_event(attendees=[
            {"email": "user@kakao.com", "displayName": "김민환"}
        ])
        assert classify_meeting(event, []) == "external"

    def test_internal_attendee_only(self):
        """내부 참석자만 → internal"""
        event = self._make_event(attendees=[
            {"email": "user@parametacorp.com"},
            {"email": "user2@iconloop.com"},
        ])
        assert classify_meeting(event, []) == "internal"

    def test_title_keyword_match(self):
        """제목에 알려진 업체명 포함 → external"""
        event = self._make_event(summary="카카오 파트너 미팅")
        assert classify_meeting(event, ["카카오"]) == "external"

    def test_no_attendees_no_company(self):
        """참석자 없고 업체명 없음 → internal"""
        event = self._make_event()
        assert classify_meeting(event, []) == "internal"

    def test_case_insensitive_title_match(self):
        """제목 매칭은 대소문자 무관"""
        event = self._make_event(summary="KAKAO 미팅")
        assert classify_meeting(event, ["kakao"]) == "external"

    def test_mixed_internal_external_attendees(self):
        """내부+외부 혼합 → external (외부 1명이라도 있으면)"""
        event = self._make_event(attendees=[
            {"email": "internal@parametacorp.com"},
            {"email": "external@naver.com"},
        ])
        assert classify_meeting(event, []) == "external"

    def test_self_true_excluded_from_domain_check(self):
        """self=True 참석자는 도메인 체크에서 제외"""
        event = self._make_event(attendees=[
            {"email": "me@parametacorp.com", "self": True},
        ])
        assert classify_meeting(event, []) == "internal"
