"""v2 Phase 2.3 테스트 — Wiki 구조 전환

대상:
- CM-07: [[링크]] 상호 참조 삽입
- CM-08: 미팅 히스토리 자동 갱신
- CM-09: 출처 태그 부착
- CM-10: Sources/ 원본 보관
- FR-D13: 자연어 회의록 검색 (인텐트 분류 경유)
- FR-D15: 복수 미팅 대기열
"""
import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3ODk=")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("TRELLO_API_KEY", "test-trello-key")
os.environ.setdefault("TRELLO_BOARD_ID", "test-board-id")
os.environ.setdefault("FEEDBACK_CHANNEL", "C_FEEDBACK")

with patch("anthropic.Anthropic"), \
     patch("slack_bolt.App"), \
     patch("slack_bolt.adapter.socket_mode.SocketModeHandler"):
    from tools import drive
    import agents.during as during
    import agents.before as before
    from agents.during import (
        _pending_minutes,
        _find_draft_for_user,
    )

_TEST_USER = "UTEST"


# ── CM-07: [[링크]] 상호 참조 ────────────────────────────────────


class TestWikiCrossReferences:
    """CM-07: 기업·인물 파일에 [[링크]] 상호 참조 삽입"""

    def test_add_minutes_backlinks_all_fields(self):
        """회의록 하단에 모든 관련 자료 역링크 삽입"""
        content = "# 카카오 미팅 (내부용)\n\n## 회의 요약\n내용"
        result = drive.add_minutes_backlinks(
            content,
            company_names=["카카오"],
            attendee_names=["홍길동", "김영희"],
            transcript_source="Sources/Transcripts/2026-04-10_카카오_transcript.md",
        )
        assert "## 관련 자료" in result
        assert "[[카카오]]" in result
        assert "[[홍길동]]" in result
        assert "[[김영희]]" in result
        assert "원본 트랜스크립트" in result

    def test_add_minutes_backlinks_empty(self):
        """빈 입력이면 원본 그대로 반환"""
        content = "# 미팅\n\n내용"
        result = drive.add_minutes_backlinks(content)
        assert result == content

    def test_add_minutes_backlinks_company_only(self):
        """업체명만 있는 경우"""
        content = "# 미팅"
        result = drive.add_minutes_backlinks(content, company_names=["KISA"])
        assert "[[KISA]]" in result
        assert "참석자" not in result

    def test_cross_references_company_to_person(self):
        """기업 파일에 인물 [[링크]] 삽입"""
        mock_creds = MagicMock()
        company_content = "# 카카오\n\n## 기본 정보\n- 업종: IT\n\n## 최근 동향\n내용"

        with patch.object(drive, "get_company_info",
                          return_value=(company_content, "file_123", False)), \
             patch.object(drive, "save_company_info") as mock_save, \
             patch.object(drive, "get_person_info", return_value=(None, None)):
            drive.add_wiki_cross_references(
                mock_creds, "contacts_folder", "카카오", ["홍길동"]
            )

        assert mock_save.called
        saved_content = mock_save.call_args[0][3]
        assert "[[홍길동]]" in saved_content
        assert "주요 담당자" in saved_content

    def test_cross_references_person_to_company(self):
        """인물 파일에 기업 [[링크]] 삽입"""
        mock_creds = MagicMock()
        person_content = "# 홍길동\n\n## 기본 정보\n- 이메일: hong@kakao.com\n"

        with patch.object(drive, "get_company_info",
                          return_value=(None, None, False)), \
             patch.object(drive, "get_person_info",
                          return_value=(person_content, "person_123")), \
             patch.object(drive, "save_person_info") as mock_save:
            drive.add_wiki_cross_references(
                mock_creds, "contacts_folder", "카카오", ["홍길동"]
            )

        assert mock_save.called
        saved_content = mock_save.call_args[0][3]
        assert "[[카카오]]" in saved_content


# ── CM-08: 미팅 히스토리 자동 갱신 ──────────────────────────────


class TestMeetingHistory:
    """CM-08: 기업·인물 파일 미팅 히스토리 테이블 자동 갱신"""

    def test_append_company_history_new_section(self):
        """미팅 히스토리 섹션이 없으면 새로 생성"""
        mock_creds = MagicMock()
        content = "# 카카오\n\n## 최근 동향\n내용"

        with patch.object(drive, "get_company_info",
                          return_value=(content, "file_123", False)), \
             patch.object(drive, "save_company_info") as mock_save:
            drive.append_meeting_history_company(
                mock_creds, "contacts_folder", "카카오",
                "2026-04-10", "AI 협업 논의",
                "2026-04-10_카카오_AI협업논의_내부용",
                ["홍길동"],
            )

        saved_content = mock_save.call_args[0][3]
        assert "## 미팅 히스토리" in saved_content
        assert "| 2026-04-10 | AI 협업 논의 |" in saved_content
        assert "[[2026-04-10_카카오_AI협업논의_내부용]]" in saved_content
        assert "[[홍길동]]" in saved_content

    def test_append_company_history_existing_table(self):
        """기존 테이블에 행 추가"""
        mock_creds = MagicMock()
        content = (
            "# 카카오\n\n## 미팅 히스토리\n"
            "| 날짜 | 주제 | 회의록 | 참석자 |\n"
            "|------|------|--------|--------|\n"
            "| 2026-03-15 | 초기 제안 | [[old_minutes]] | [[홍길동]] |\n"
        )

        with patch.object(drive, "get_company_info",
                          return_value=(content, "file_123", False)), \
             patch.object(drive, "save_company_info") as mock_save:
            drive.append_meeting_history_company(
                mock_creds, "contacts_folder", "카카오",
                "2026-04-10", "후속 미팅",
                "2026-04-10_후속_내부용",
            )

        saved_content = mock_save.call_args[0][3]
        # 기존 행 유지
        assert "| 2026-03-15 | 초기 제안 |" in saved_content
        # 새 행 추가
        assert "| 2026-04-10 | 후속 미팅 |" in saved_content

    def test_no_duplicate_history_entry(self):
        """같은 날짜+제목 중복 방지"""
        mock_creds = MagicMock()
        content = (
            "# 카카오\n\n## 미팅 히스토리\n"
            "| 날짜 | 주제 | 회의록 | 참석자 |\n"
            "|------|------|--------|--------|\n"
            "| 2026-04-10 | AI 협업 | [[minutes]] | [[홍길동]] |\n"
        )

        with patch.object(drive, "get_company_info",
                          return_value=(content, "file_123", False)), \
             patch.object(drive, "save_company_info") as mock_save:
            drive.append_meeting_history_company(
                mock_creds, "contacts_folder", "카카오",
                "2026-04-10", "AI 협업",
                "minutes",
            )

        # 중복이므로 save 호출 안 함
        assert not mock_save.called

    def test_append_person_history(self):
        """인물 파일에 미팅 히스토리 추가"""
        mock_creds = MagicMock()
        content = "# 홍길동\n\n## 기본 정보\n- 소속: 카카오\n"

        with patch.object(drive, "get_person_info",
                          return_value=(content, "person_123")), \
             patch.object(drive, "save_person_info") as mock_save:
            drive.append_meeting_history_person(
                mock_creds, "contacts_folder", "홍길동",
                "2026-04-10", "AI 협업",
                "2026-04-10_카카오_AI협업_내부용",
            )

        saved_content = mock_save.call_args[0][3]
        assert "## 미팅 히스토리" in saved_content
        assert "| 2026-04-10 | AI 협업 |" in saved_content


# ── CM-09: 출처 태그 부착 ────────────────────────────────────────


class TestSourceTags:
    """CM-09: 정보 수집 시 출처 태그 [출처: {type}] 부착"""

    def test_company_research_web_search_source_tag(self):
        """업체 리서치: 웹 검색 결과에 [출처: 웹 검색] 태그"""
        with patch.object(before, "_get_creds_and_config",
                          return_value=(MagicMock(), "contacts_id", "knowledge_id")), \
             patch.object(before, "drive") as mock_drive, \
             patch.object(before, "_search", return_value="- AI 투자 확대"), \
             patch.object(before, "_generate", return_value="- DID 연동 가능"), \
             patch.object(before, "gmail") as mock_gmail:
            mock_drive.get_company_info.return_value = (None, None, False)
            mock_drive.get_company_knowledge.return_value = "서비스 정보"
            mock_drive.save_company_info.return_value = "file_123"
            mock_drive.save_source_file.return_value = "source_123"
            mock_gmail.search_recent_emails.return_value = []

            content, _ = before.research_company("UTEST", "카카오", force=True)

        assert "[출처: 웹 검색" in content

    def test_company_research_email_source_tag(self):
        """업체 리서치: Gmail 섹션에 [출처: Gmail] 태그"""
        with patch.object(before, "_get_creds_and_config",
                          return_value=(MagicMock(), "contacts_id", "knowledge_id")), \
             patch.object(before, "drive") as mock_drive, \
             patch.object(before, "_search", return_value="- 뉴스 없음"), \
             patch.object(before, "_generate", return_value="- 연결점 없음"), \
             patch.object(before, "gmail") as mock_gmail:
            mock_drive.get_company_info.return_value = (None, None, False)
            mock_drive.get_company_knowledge.return_value = "서비스 정보"
            mock_drive.save_company_info.return_value = "file_123"
            mock_drive.save_source_file.return_value = "source_123"
            mock_gmail.search_recent_emails.return_value = [
                {"date": "2026-04-05", "subject": "PoC 논의", "snippet": "내용"}
            ]

            content, _ = before.research_company("UTEST", "카카오", force=True)

        assert "[출처: Gmail" in content

    def test_person_research_wiki_link(self):
        """인물 리서치: 소속 기업에 [[링크]] 삽입"""
        with patch.object(before, "_get_creds_and_config",
                          return_value=(MagicMock(), "contacts_id", None)), \
             patch.object(before, "drive") as mock_drive, \
             patch.object(before, "_search", return_value="프로필 정보"), \
             patch.object(before, "gmail") as mock_gmail, \
             patch.object(before, "research_company"):
            mock_drive.get_person_info.return_value = (None, None)
            mock_drive.save_person_info.return_value = "person_123"
            mock_gmail.search_recent_emails.return_value = []

            content, _ = before.research_person("UTEST", "홍길동", "카카오", force=True)

        assert "[[카카오]]" in content


# ── CM-10: Sources/ 원본 보관 ────────────────────────────────────


class TestSourcesStorage:
    """CM-10: Sources/ 폴더에 원본 자료 저장"""

    def test_save_source_file_creates_folder(self):
        """save_source_file이 Sources/{subfolder} 폴더를 생성하고 파일 저장"""
        mock_creds = MagicMock()

        with patch.object(drive, "_service") as mock_svc, \
             patch.object(drive, "create_folder", return_value="folder_123") as mock_cf, \
             patch.object(drive, "_write_file", return_value="file_123") as mock_wf:
            # _ensure_sources_folder에서 parent 조회
            mock_svc.return_value.files.return_value.get.return_value.execute.return_value = {
                "parents": ["root_id"]
            }

            result = drive.save_source_file(
                mock_creds, "contacts_folder",
                "Transcripts", "2026-04-10_카카오_transcript.md",
                "트랜스크립트 내용",
            )

        assert result == "file_123"
        # create_folder 호출: Sources + Transcripts
        assert mock_cf.call_count == 2

    def test_research_company_saves_to_sources(self):
        """업체 리서치 시 웹 검색 결과가 Sources/Research/에 저장"""
        with patch.object(before, "_get_creds_and_config",
                          return_value=(MagicMock(), "contacts_id", "knowledge_id")), \
             patch.object(before, "drive") as mock_drive, \
             patch.object(before, "_search", return_value="- AI 투자 확대 뉴스"), \
             patch.object(before, "_generate", return_value="- 연결점"), \
             patch.object(before, "gmail") as mock_gmail:
            mock_drive.get_company_info.return_value = (None, None, False)
            mock_drive.get_company_knowledge.return_value = "서비스 정보"
            mock_drive.save_company_info.return_value = "file_123"
            mock_drive.save_source_file.return_value = "source_123"
            mock_gmail.search_recent_emails.return_value = []

            before.research_company("UTEST", "카카오", force=True)

        # Sources/Research 저장 확인
        mock_drive.save_source_file.assert_called_once()
        call_args = mock_drive.save_source_file.call_args
        assert call_args[0][2] == "Research"  # subfolder
        assert "카카오" in call_args[0][3]  # filename

    def test_company_research_saves_trello_context_to_wiki(self):
        """업체 Wiki에 Trello 카드 설명/미완료/코멘트를 저장"""
        trello_context = {
            "card_name": "다날핀테크 - PoC/Pilot 제안",
            "url": "https://trello.com/c/CXQzHjRn",
            "description": "스테이블코인 파일럿 제안 진행 중",
            "incomplete_items": ["파일럿 제안 방향 내부 정리"],
            "recent_comments": [{"author": "김민환", "text": "다음 미팅 전 제안서 정리"}],
        }
        with patch.object(before, "_get_creds_and_config",
                          return_value=(MagicMock(), "contacts_id", "knowledge_id")), \
             patch.object(before, "drive") as mock_drive, \
             patch.object(before, "_search", return_value="- 뉴스"), \
             patch.object(before, "_generate", return_value="- 연결점"), \
             patch.object(before, "gmail") as mock_gmail, \
             patch.object(before, "trello") as mock_trello:
            mock_drive.get_company_info.return_value = (None, None, False)
            mock_drive.get_company_knowledge.return_value = "서비스 정보"
            mock_drive.save_company_info.return_value = "file_123"
            mock_drive.save_source_file.return_value = "source_123"
            mock_gmail.search_recent_emails.return_value = []
            mock_trello.get_card_context.return_value = trello_context

            content, _ = before.research_company("UTEST", "다날핀테크", force=True)

        assert "## Trello 맥락" in content
        assert "[출처: Trello" in content
        assert "[다날핀테크 - PoC/Pilot 제안](https://trello.com/c/CXQzHjRn)" in content
        assert "스테이블코인 파일럿 제안 진행 중" in content
        assert "파일럿 제안 방향 내부 정리" in content
        assert "김민환: 다음 미팅 전 제안서 정리" in content

    def test_company_research_uses_trello_for_service_connection_fallback(self):
        trello_context = {
            "card_name": "다날핀테크 - PoC/Pilot 제안",
            "url": "https://trello.com/c/CXQzHjRn",
            "description": "스테이블코인 파일럿 제안 진행 중",
            "incomplete_items": ["파일럿 제안 방향 내부 정리"],
            "recent_comments": [],
        }
        with patch.object(before, "_get_creds_and_config",
                          return_value=(MagicMock(), "contacts_id", "knowledge_id")), \
             patch.object(before, "drive") as mock_drive, \
             patch.object(before, "_search", return_value="- 일반 뉴스"), \
             patch.object(before, "_generate", return_value="- 명확한 접점 없음"), \
             patch.object(before, "gmail") as mock_gmail, \
             patch.object(before, "trello") as mock_trello:
            mock_drive.get_company_info.return_value = (None, None, False)
            mock_drive.get_company_knowledge.return_value = "loopchain, MyID"
            mock_drive.save_company_info.return_value = "file_123"
            mock_drive.save_source_file.return_value = "source_123"
            mock_gmail.search_recent_emails.return_value = []
            mock_trello.get_card_context.return_value = trello_context

            content, _ = before.research_company("UTEST", "다날핀테크", force=True)

        assert "명확한 접점 없음" not in content
        assert "loopchain" in content
        assert "스테이블코인 결제·정산 인프라" in content
        assert "PoC/Pilot 제안" in content


# ── FR-D13: 자연어 회의록 검색 ───────────────────────────────────


class TestNaturalLanguageSearch:
    """FR-D13: 자연어 회의록 검색 (인텐트 분류 → _search_minutes)"""

    def test_search_minutes_intent_in_prompt(self):
        """_INTENT_PROMPT에 search_minutes 인텐트 포함"""
        from main import _INTENT_PROMPT
        assert "search_minutes" in _INTENT_PROMPT

    def test_search_minutes_with_company(self):
        """업체명 기반 검색 — Drive 파일명에서 필터링"""
        from main import _search_minutes
        from store import user_store

        slack = MagicMock()
        files = [
            {"id": "f1", "name": "2026-04-10_카카오 미팅_내부용.md"},
            {"id": "f2", "name": "2026-04-11_네이버 미팅_내부용.md"},
        ]
        with patch.object(user_store, "get_credentials", return_value=MagicMock()), \
             patch.object(user_store, "get_user",
                          return_value={"minutes_folder_id": "F"}), \
             patch("tools.drive.list_minutes", return_value=files):
            _search_minutes(slack, user_id=_TEST_USER, query="카카오")

        slack.chat_postMessage.assert_called_once()
        text = slack.chat_postMessage.call_args[1]["text"]
        assert "카카오 미팅" in text
        assert "네이버" not in text

    def test_search_minutes_with_month(self):
        """YYYY-MM 기간 기반 검색 — 해당 월 파일만 통과"""
        from main import _search_minutes
        from store import user_store

        slack = MagicMock()
        files = [
            {"id": "f1", "name": "2026-03-10_A_내부용.md"},      # 3월 (통과)
            {"id": "f2", "name": "2026-03-31_B_내부용.md"},      # 3월 (통과)
            {"id": "f3", "name": "2026-04-01_C_내부용.md"},      # 4월 (제외)
        ]
        with patch.object(user_store, "get_credentials", return_value=MagicMock()), \
             patch.object(user_store, "get_user",
                          return_value={"minutes_folder_id": "F"}), \
             patch("tools.drive.list_minutes", return_value=files):
            _search_minutes(slack, user_id=_TEST_USER, query="2026-03")

        text = slack.chat_postMessage.call_args[1]["text"]
        assert "A" in text and "B" in text
        assert "2026-04-01" not in text and "_C" not in text

    def test_search_minutes_no_results(self):
        """검색 결과 없음"""
        from main import _search_minutes
        from store import user_store

        slack = MagicMock()
        with patch.object(user_store, "get_credentials", return_value=MagicMock()), \
             patch.object(user_store, "get_user",
                          return_value={"minutes_folder_id": "F"}), \
             patch("tools.drive.list_minutes", return_value=[]):
            _search_minutes(slack, user_id=_TEST_USER, query="없는회사")

        text = slack.chat_postMessage.call_args[1]["text"]
        assert "결과가 없" in text or "회의록이 없" in text or "검색 결과" in text


# ── 인텐트 분류 개선: question 인텐트 ──────────────────────────────


class TestQuestionIntent:
    """질문 형태 메시지를 feedback이 아니라 question으로 분류하고 LLM 답변 반환.
    이슈 재현: '구글 밋에서 회의를 할 건데, 회의록은 자동 생성하고 싶으면 어떻게 하면 됨?'
    등이 feedback으로 잘못 분류되던 버그 대응."""

    def test_question_intent_defined_in_prompt(self):
        """_INTENT_PROMPT에 question 인텐트 + 질문 형태 feedback 제외 규칙 포함"""
        from main import _INTENT_PROMPT
        assert "question" in _INTENT_PROMPT
        # feedback 설명에 질문 형태 제외 문구가 있어야 함
        assert "질문 형태" in _INTENT_PROMPT

    def test_handle_question_invokes_llm_and_posts_answer(self):
        """_handle_question: generate_text를 호출해 답변을 Slack에 게시"""
        from main import _handle_question

        slack = MagicMock()
        with patch("agents.before.generate_text",
                   return_value="회의록은 `/미팅종료` 후 자동 생성됩니다.") as gen:
            _handle_question(slack, text="회의록 자동 생성은 어떻게 해?",
                             user_id="U1", channel=None, thread_ts=None)

        # LLM 호출 확인
        gen.assert_called_once()
        prompt_arg = gen.call_args[0][0]
        assert "회의록 자동 생성은 어떻게 해?" in prompt_arg  # 질문 전달
        assert "ParaMee" in prompt_arg                      # 시스템 컨텍스트 포함

        # Slack 게시 확인
        slack.chat_postMessage.assert_called_once()
        kwargs = slack.chat_postMessage.call_args[1]
        assert kwargs["text"] == "회의록은 `/미팅종료` 후 자동 생성됩니다."
        assert kwargs["channel"] == "U1"  # channel 없으면 DM

    def test_handle_question_falls_back_on_llm_error(self):
        """LLM 호출 실패 시 친절한 폴백 메시지 출력"""
        from main import _handle_question

        slack = MagicMock()
        with patch("agents.before.generate_text", side_effect=RuntimeError("LLM down")):
            _handle_question(slack, text="뭐라도 답해줘",
                             user_id="U1", channel="C1", thread_ts="T1")

        kwargs = slack.chat_postMessage.call_args[1]
        assert "생성하지 못" in kwargs["text"]
        assert "/도움말" in kwargs["text"]
        assert kwargs["channel"] == "C1"
        assert kwargs["thread_ts"] == "T1"


# ── FR-D15: 복수 미팅 대기열 ────────────────────────────────────


class TestMultipleMeetingQueue:
    """FR-D15: 동일 사용자 복수 미팅 대기열 + 알림"""

    def setup_method(self):
        _pending_minutes.clear()

    def test_multiple_drafts_coexist(self):
        """event_id 키 덕분에 동일 사용자의 복수 초안이 공존"""
        _pending_minutes["evt_001"] = {
            "user_id": _TEST_USER,
            "title": "미팅 A",
            "internal_body": "내용 A",
        }
        _pending_minutes["evt_002"] = {
            "user_id": _TEST_USER,
            "title": "미팅 B",
            "internal_body": "내용 B",
        }

        assert len(_pending_minutes) == 2
        assert _pending_minutes["evt_001"]["title"] == "미팅 A"
        assert _pending_minutes["evt_002"]["title"] == "미팅 B"

    def test_find_draft_returns_first_match(self):
        """_find_draft_for_user는 첫 번째 매칭 반환"""
        _pending_minutes["evt_001"] = {
            "user_id": _TEST_USER,
            "title": "미팅 A",
        }
        _pending_minutes["evt_002"] = {
            "user_id": "OTHER_USER",
            "title": "미팅 B",
        }

        found = _find_draft_for_user(_TEST_USER)
        assert found is not None
        assert found[0] == "evt_001"

        found_other = _find_draft_for_user("OTHER_USER")
        assert found_other is not None
        assert found_other[0] == "evt_002"

    def test_no_draft_returns_none(self):
        """초안 없는 사용자에 대해 None 반환"""
        assert _find_draft_for_user("NONEXISTENT") is None


# ── 통합 테스트: _build_minutes_content with backlinks ──────────


class TestMinutesContentWithBacklinks:
    """_build_minutes_content에 Wiki 역링크 포함"""

    def test_internal_minutes_has_backlinks(self):
        """내부용 회의록에 관련 자료 섹션 포함"""
        with patch.object(drive, "add_minutes_backlinks",
                          wraps=drive.add_minutes_backlinks):
            content = during._build_minutes_content(
                "카카오 미팅", "2026-04-10", "14:00-15:00",
                "홍길동, 김영희", "트랜스크립트",
                "## 회의 요약\n내용", "트랜스크립트 원문", "",
                kind="내부용",
                company_names=["카카오"],
                attendee_names=["홍길동"],
                transcript_source_name="Sources/Transcripts/2026-04-10_카카오_transcript.md",
            )

        assert "## 관련 자료" in content
        assert "[[카카오]]" in content
        assert "[[홍길동]]" in content
        assert "원본 트랜스크립트" in content

    def test_external_minutes_has_backlinks(self):
        """외부용 회의록에도 업체/참석자 역링크"""
        content = during._build_minutes_content(
            "카카오 미팅", "2026-04-10", "14:00-15:00",
            "홍길동", "트랜스크립트",
            "## 회의 개요\n내용", "", "",
            kind="외부용",
            company_names=["카카오"],
            attendee_names=["홍길동"],
        )

        assert "## 관련 자료" in content
        assert "[[카카오]]" in content

    def test_no_backlinks_when_no_context(self):
        """업체/참석자 정보 없으면 역링크 섹션 없음"""
        content = during._build_minutes_content(
            "미팅", "2026-04-10", "14:00-15:00",
            "정보 없음", "노트",
            "## 회의 요약\n내용", "", "노트 내용",
            kind="내부용",
        )

        assert "## 관련 자료" not in content


class TestCompanyResearchSectionExtraction:
    def test_recent_trends_subsection_excludes_company_overview(self):
        content = """# 다날

## 최근 동향
- **산업 위치**: 모바일 결제 플랫폼
- **시장 포지션**: 국내 결제사

### 최근 동향 (`2026-04-28` 기준)
- **[다날, 원화 스테이블코인 결제 실증 추진]**: 결제 인프라 연계 (https://example.com/stablecoin)
- **[다날 DID 인증 적용]**: 외국인 결제 인증 관련 (https://example.com/did)

## 파라메타 서비스 연결점
- **MyID**: DID 인증 연계
"""

        news_lines, _parascope, connection_lines, _emails, _updates = (
            before._extract_company_content_sections(content)
        )

        assert news_lines == [
            "다날, 원화 스테이블코인 결제 실증 추진 (https://example.com/stablecoin)",
            "다날 DID 인증 적용 (https://example.com/did)",
        ]
        assert not any("산업 위치" in line for line in news_lines)
        assert connection_lines == ["**MyID**: DID 인증 연계"]
