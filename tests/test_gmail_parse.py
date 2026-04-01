"""tools/gmail.py — parse_address_header 단위 테스트"""
import pytest
from unittest.mock import patch

with patch("tools.gmail._service"):
    from tools.gmail import parse_address_header


class TestParseAddressHeader:
    def test_name_and_email(self):
        """'이름 <email>' 형식 파싱"""
        result = parse_address_header("김민환 <kim@kakao.com>")
        assert len(result) == 1
        assert result[0]["name"] == "김민환"
        assert result[0]["email"] == "kim@kakao.com"

    def test_email_only(self):
        """이메일만 있는 경우 name은 빈 문자열"""
        result = parse_address_header("kim@kakao.com")
        assert len(result) == 1
        assert result[0]["name"] == ""
        assert result[0]["email"] == "kim@kakao.com"

    def test_multiple_addresses(self):
        """쉼표 구분 복수 주소"""
        result = parse_address_header("김민환 <kim@kakao.com>, 이영희 <lee@naver.com>")
        assert len(result) == 2
        assert result[0]["name"] == "김민환"
        assert result[1]["name"] == "이영희"
        assert result[1]["email"] == "lee@naver.com"

    def test_mixed_format(self):
        """이름+이메일 혼합, 이메일만 혼합"""
        result = parse_address_header("홍길동 <hong@co.com>, plain@test.com")
        assert len(result) == 2
        assert result[0]["name"] == "홍길동"
        assert result[1]["name"] == ""
        assert result[1]["email"] == "plain@test.com"

    def test_empty_string(self):
        """빈 문자열 → 빈 리스트"""
        assert parse_address_header("") == []

    def test_no_at_sign(self):
        """@가 없는 문자열 → 무시"""
        result = parse_address_header("not-an-email")
        assert result == []

    def test_whitespace_trimmed(self):
        """앞뒤 공백 제거"""
        result = parse_address_header("  김민환  <  kim@kakao.com  >  ")
        assert result[0]["email"] == "kim@kakao.com"

    def test_quoted_name(self):
        """따옴표 없는 이름도 정상 파싱"""
        result = parse_address_header("John Doe <john@example.com>")
        assert result[0]["name"] == "John Doe"
        assert result[0]["email"] == "john@example.com"
