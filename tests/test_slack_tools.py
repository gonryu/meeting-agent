"""tools/slack_tools.py 단위 테스트"""
import pytest
from tools.slack_tools import format_time, build_briefing_message


# ── format_time ───────────────────────────────────────────────

class TestFormatTime:
    def test_afternoon(self):
        assert format_time("2026-03-24T15:30:00+09:00") == "오후 3:30"

    def test_morning(self):
        assert format_time("2026-03-24T09:05:00+09:00") == "오전 9:05"

    def test_noon(self):
        assert format_time("2026-03-24T12:00:00+09:00") == "오후 12:00"

    def test_midnight(self):
        assert format_time("2026-03-24T00:00:00+09:00") == "오전 0:00"

    def test_all_day_event(self):
        """날짜만 있으면 '월/일 종일' 반환"""
        assert format_time("2026-03-24") == "3/24 종일"

    def test_empty_string(self):
        assert format_time("") == ""

    def test_utc_z_notation(self):
        """UTC 'Z' 표기도 처리"""
        result = format_time("2026-03-24T06:00:00Z")
        assert "오전" in result or "오후" in result

    def test_minute_zero_padding(self):
        """분 두 자리 패딩 (예: :05)"""
        assert format_time("2026-03-24T14:05:00+09:00") == "오후 2:05"


# ── build_briefing_message ────────────────────────────────────

def _get_text(blocks: list) -> str:
    """블록 킷에서 mrkdwn 텍스트 추출"""
    for block in blocks:
        if block.get("type") == "section":
            return block["text"]["text"]
    return ""


def _base_meeting():
    return {
        "summary": "카카오 미팅",
        "start_time": "2026-03-24T15:00:00+09:00",
        "meet_link": "https://meet.google.com/abc",
        "location": "",
        "description": "",
    }


class TestBuildBriefingMessage:
    def test_returns_list_of_blocks(self):
        blocks = build_briefing_message(
            meeting=_base_meeting(),
            company_name="카카오",
            company_news=["뉴스1"],
            persons=[],
            service_connections=[],
            previous_context={"trello": [], "emails": []},
        )
        assert isinstance(blocks, list)
        assert len(blocks) > 0
        assert blocks[0]["type"] == "section"

    def test_news_limited_to_3(self):
        """뉴스 5개 전달해도 최대 3개만 표시"""
        news = ["뉴스1", "뉴스2", "뉴스3", "뉴스4", "뉴스5"]
        text = _get_text(build_briefing_message(
            meeting=_base_meeting(), company_name="카카오",
            company_news=news, persons=[], service_connections=[],
            previous_context={"trello": [], "emails": []},
        ))
        assert "뉴스4" not in text
        assert "뉴스5" not in text
        assert "뉴스3" in text

    def test_no_persons_section_hidden(self):
        """담당자 없으면 '담당자' 섹션 미표시"""
        text = _get_text(build_briefing_message(
            meeting=_base_meeting(), company_name="카카오",
            company_news=[], persons=[],
            service_connections=[],
            previous_context={"trello": [], "emails": []},
        ))
        assert "담당자" not in text

    def test_persons_with_linkedin(self):
        """담당자에 LinkedIn URL 포함"""
        persons = [{"name": "김민환", "role": "팀장", "linkedin": "https://linkedin.com/in/kim", "memo": ""}]
        text = _get_text(build_briefing_message(
            meeting=_base_meeting(), company_name="카카오",
            company_news=[], persons=persons,
            service_connections=[],
            previous_context={"trello": [], "emails": []},
        ))
        assert "LinkedIn" in text
        assert "linkedin.com/in/kim" in text

    def test_no_previous_context(self):
        """이전 맥락 없으면 '이전 미팅 기록 없음'"""
        text = _get_text(build_briefing_message(
            meeting=_base_meeting(), company_name="카카오",
            company_news=[], persons=[],
            service_connections=[],
            previous_context={"trello": [], "emails": []},
        ))
        assert "이전 미팅 기록 없음" in text

    def test_email_context_shown(self):
        """이메일 맥락 있으면 snippet 표시"""
        emails = [{"snippet": "안녕하세요 파트너십 관련 논의드립니다", "date": "2026-03-01", "subject": "Re: 미팅"}]
        text = _get_text(build_briefing_message(
            meeting=_base_meeting(), company_name="카카오",
            company_news=[], persons=[],
            service_connections=[],
            previous_context={"trello": [], "emails": emails},
        ))
        assert "안녕하세요 파트너십" in text

    def test_location_shown(self):
        """미팅 장소 있으면 표시"""
        meeting = {**_base_meeting(), "location": "서울 강남"}
        text = _get_text(build_briefing_message(
            meeting=meeting, company_name="카카오",
            company_news=[], persons=[],
            service_connections=[],
            previous_context={"trello": [], "emails": []},
        ))
        assert "서울 강남" in text

    def test_no_service_connections(self):
        """서비스 연결점 없으면 '분석 정보 없음'"""
        text = _get_text(build_briefing_message(
            meeting=_base_meeting(), company_name="카카오",
            company_news=[], persons=[],
            service_connections=[],
            previous_context={"trello": [], "emails": []},
        ))
        assert "분석 정보 없음" in text

    def test_no_news_shows_placeholder(self):
        """뉴스 없으면 '최근 동향 정보 없음'"""
        text = _get_text(build_briefing_message(
            meeting=_base_meeting(), company_name="카카오",
            company_news=[], persons=[],
            service_connections=[],
            previous_context={"trello": [], "emails": []},
        ))
        assert "최근 동향 정보 없음" in text

    def test_meeting_title_in_header(self):
        """미팅 제목과 업체명이 헤더에 포함"""
        text = _get_text(build_briefing_message(
            meeting=_base_meeting(), company_name="카카오",
            company_news=[], persons=[],
            service_connections=[],
            previous_context={"trello": [], "emails": []},
        ))
        assert "카카오 미팅" in text
        assert "카카오" in text
