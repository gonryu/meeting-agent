"""v2 Phase 2.4 테스트 — 제안서 워크플로우

대상:
- FR-A11: 제안서 트리거 키워드 감지 + 제안
- FR-A12: intake 자동 추출 + 개요 제시
- FR-A13: 개요 확인 → 생성 → 수정 루프
- FR-A14: Google Docs 공유 + 편집
- FR-B16: 참석자 기반 업체 역추론
"""
import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

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
    from agents import proposal
    from agents import before
    from tools import drive

_TEST_USER = "UTEST"


def _slack():
    s = MagicMock()
    s.chat_postMessage.return_value = {"ok": True, "ts": "1234567890.123456"}
    return s


# ── FR-A11: 트리거 키워드 감지 ───────────────────────────────────


class TestProposalTrigger:
    """FR-A11: 회의록 키워드 감지 → 제안서 작성 제안"""

    def setup_method(self):
        proposal._pending_proposals.clear()

    def test_trigger_detected_with_keyword(self):
        """트리거 키워드가 있으면 제안 메시지 발송"""
        slack = _slack()
        proposal.detect_and_suggest_proposal(
            slack,
            user_id=_TEST_USER,
            event_id="evt_001",
            title="카카오 AI 협업 미팅",
            date_str="2026-04-10",
            internal_body="## 주요 결정 사항\n- AI 공동개발 PoC 진행 합의\n- 제안서 작성 필요",
            company_names=["카카오"],
            attendees_raw=[{"name": "홍길동"}],
            creds=MagicMock(),
        )

        # Slack 메시지 발송됨
        slack.chat_postMessage.assert_called_once()
        call_kwargs = slack.chat_postMessage.call_args[1]
        assert "제안서" in call_kwargs["text"]
        assert "blocks" in call_kwargs

        # 상태 저장됨
        assert _TEST_USER in proposal._pending_proposals

    def test_no_trigger_without_keywords(self):
        """트리거 키워드 없으면 건너뜀"""
        slack = _slack()
        proposal.detect_and_suggest_proposal(
            slack,
            user_id=_TEST_USER,
            event_id="evt_002",
            title="내부 주간 회의",
            date_str="2026-04-10",
            internal_body="## 주요 논의\n- 주간 진행 상황 공유",
            company_names=[],
            attendees_raw=[],
            creds=MagicMock(),
        )

        slack.chat_postMessage.assert_not_called()
        assert _TEST_USER not in proposal._pending_proposals

    def test_trigger_keywords_list(self):
        """모든 트리거 키워드 확인"""
        expected = ["협업", "제안", "MOU", "PoC", "파일럿", "공동개발",
                    "제휴", "투자", "계약", "도입", "검토", "다음 단계"]
        assert proposal._PROPOSAL_TRIGGERS == expected

    def test_multiple_keywords_detected(self):
        """복수 키워드 감지 시 모두 표시"""
        slack = _slack()
        proposal.detect_and_suggest_proposal(
            slack,
            user_id=_TEST_USER,
            event_id="evt_003",
            title="미팅",
            date_str="2026-04-10",
            internal_body="MOU 체결 논의, PoC 진행 합의, 투자 검토 필요",
            company_names=["KISA"],
            attendees_raw=[],
            creds=MagicMock(),
        )

        call_kwargs = slack.chat_postMessage.call_args[1]
        blocks_text = json.dumps(call_kwargs["blocks"])
        assert "MOU" in blocks_text
        assert "PoC" in blocks_text


# ── FR-A12: Intake 추출 + 개요 제시 ──────────────────────────────


class TestProposalIntake:
    """FR-A12: 제안서 intake 자동 추출 + 개요 제시"""

    def setup_method(self):
        proposal._pending_proposals.clear()

    def test_handle_proposal_start_extracts_intake(self):
        """제안서 작성 시작 → intake JSON 추출 + 개요 Slack 발송"""
        # 상태 준비
        proposal._pending_proposals[_TEST_USER] = {
            "event_id": "evt_001",
            "title": "카카오 미팅",
            "date_str": "2026-04-10",
            "internal_body": "## 주요 결정\n- AI 공동개발 PoC 합의",
            "company_names": ["카카오"],
            "attendees_raw": [],
            "creds": MagicMock(),
            "contacts_folder_id": "contacts_123",
            "knowledge_file_id": "knowledge_123",
            "intake": None,
            "outline_ts": None,
            "draft_doc_id": None,
            "proposal_body": None,
        }

        intake_json = json.dumps({
            "title": "AI 데이터 분석 공동 개발 제안",
            "purpose": "AI 데이터 분석 기술 공동 개발",
            "target": "카카오 AI사업부",
            "scope": "PoC 3개월",
            "key_points": ["데이터 연동", "성과 지표", "역할 분담"],
            "background": "양사 AI 기술 시너지",
        })

        slack = _slack()
        body = {"user": {"id": _TEST_USER}, "actions": [{"value": "{}"}]}

        with patch.object(proposal, "_generate", return_value=intake_json), \
             patch.object(proposal, "drive") as mock_drive:
            mock_drive.get_company_info.return_value = ("카카오 정보", "file_id", True)
            mock_drive.get_company_knowledge.return_value = "서비스 지식"
            proposal.handle_proposal_start(slack, body)

        # intake가 저장됨
        state = proposal._pending_proposals[_TEST_USER]
        assert state["intake"] is not None
        assert state["intake"]["title"] == "AI 데이터 분석 공동 개발 제안"

        # 개요가 Slack에 발송됨 (2번 호출: 추출 중 + 개요)
        assert slack.chat_postMessage.call_count >= 2

    def test_intake_extraction_failure(self):
        """intake 추출 실패 시 에러 메시지"""
        proposal._pending_proposals[_TEST_USER] = {
            "event_id": "evt_001",
            "title": "미팅",
            "date_str": "2026-04-10",
            "internal_body": "내용",
            "company_names": [],
            "attendees_raw": [],
            "creds": MagicMock(),
            "contacts_folder_id": None,
            "knowledge_file_id": None,
            "intake": None,
            "outline_ts": None,
            "draft_doc_id": None,
            "proposal_body": None,
        }

        slack = _slack()
        body = {"user": {"id": _TEST_USER}, "actions": [{"value": "{}"}]}

        with patch.object(proposal, "_generate", side_effect=Exception("LLM 오류")):
            proposal.handle_proposal_start(slack, body)

        # 에러 메시지 발송
        last_call = slack.chat_postMessage.call_args_list[-1]
        assert "실패" in last_call[1]["text"]


# ── FR-A13: 생성 → 수정 루프 ─────────────────────────────────────


class TestProposalGeneration:
    """FR-A13: 개요 확인 → 생성 → 수정 요청"""

    def setup_method(self):
        proposal._pending_proposals.clear()

    def _setup_state_with_intake(self):
        proposal._pending_proposals[_TEST_USER] = {
            "event_id": "evt_001",
            "title": "카카오 미팅",
            "date_str": "2026-04-10",
            "internal_body": "## 주요 결정\n- PoC 합의",
            "company_names": ["카카오"],
            "attendees_raw": [],
            "creds": MagicMock(),
            "contacts_folder_id": "contacts_123",
            "knowledge_file_id": "knowledge_123",
            "company_info": "카카오 정보",
            "knowledge": "서비스 지식",
            "intake": {
                "title": "AI 공동 개발 제안",
                "purpose": "AI 기술 시너지",
                "target": "카카오 AI사업부",
                "scope": "PoC 3개월",
                "key_points": ["데이터 연동", "성과 지표"],
                "background": "배경 정보",
            },
            "outline_ts": "ts_outline",
            "draft_doc_id": None,
            "proposal_body": None,
        }

    def test_confirm_outline_generates_proposal(self):
        """개요 확인 → 제안서 생성"""
        self._setup_state_with_intake()
        slack = _slack()
        body = {"user": {"id": _TEST_USER}}

        with patch.object(proposal, "_generate_proposal",
                          return_value="# AI 공동 개발 제안\n\n## 1. 배경\n내용"), \
             patch.object(proposal, "drive") as mock_drive, \
             patch.object(proposal, "user_store"):
            mock_drive._service.return_value.files.return_value.get.return_value.execute.return_value = {
                "parents": ["root_id"]
            }
            mock_drive.create_folder.return_value = "proposals_folder"
            mock_drive.create_draft_doc.return_value = "doc_123"

            proposal.handle_proposal_confirm_outline(slack, body)

        state = proposal._pending_proposals[_TEST_USER]
        assert state["proposal_body"] is not None
        assert "AI 공동 개발 제안" in state["proposal_body"]

    def test_proposal_has_backlinks(self):
        """생성된 제안서에 역링크 포함"""
        self._setup_state_with_intake()
        slack = _slack()
        body = {"user": {"id": _TEST_USER}}

        with patch.object(proposal, "_generate_proposal",
                          return_value="# 제안서\n내용"), \
             patch.object(proposal, "drive") as mock_drive, \
             patch.object(proposal, "user_store"):
            mock_drive._service.return_value.files.return_value.get.return_value.execute.return_value = {
                "parents": ["root_id"]
            }
            mock_drive.create_folder.return_value = "proposals_folder"
            mock_drive.create_draft_doc.return_value = "doc_123"

            proposal.handle_proposal_confirm_outline(slack, body)

        state = proposal._pending_proposals[_TEST_USER]
        assert "## 관련 자료" in state["proposal_body"]
        assert "[[카카오]]" in state["proposal_body"]

    def test_outline_edit_reply(self):
        """개요 수정 텍스트 반영"""
        self._setup_state_with_intake()
        slack = _slack()

        new_intake = json.dumps({
            "title": "AI 공동 개발 제안 (수정)",
            "purpose": "수정된 목적",
            "target": "카카오",
            "scope": "6개월",
            "key_points": ["항목1"],
            "background": "배경",
        })

        with patch.object(proposal, "_generate", return_value=new_intake):
            proposal.handle_proposal_outline_edit_reply(
                slack, _TEST_USER, "범위를 6개월로 변경해줘")

        state = proposal._pending_proposals[_TEST_USER]
        assert state["intake"]["scope"] == "6개월"


# ── FR-A14: Google Docs 공유 ─────────────────────────────────────


class TestProposalDocs:
    """FR-A14: 제안서 Google Docs 공유"""

    def setup_method(self):
        proposal._pending_proposals.clear()

    def test_done_sends_doc_link(self):
        """완료 시 Google Docs 링크 발송"""
        proposal._pending_proposals[_TEST_USER] = {
            "intake": {"title": "제안서"},
            "title": "미팅",
            "draft_doc_id": "doc_abc",
        }
        slack = _slack()
        body = {"user": {"id": _TEST_USER}}

        proposal.handle_proposal_done(slack, body)

        text = slack.chat_postMessage.call_args[1]["text"]
        assert "docs.google.com" in text
        # 상태 정리됨
        assert _TEST_USER not in proposal._pending_proposals

    def test_cancel_clears_state(self):
        """취소 시 상태 정리"""
        proposal._pending_proposals[_TEST_USER] = {
            "intake": {"title": "제안서"},
        }
        slack = _slack()
        body = {"user": {"id": _TEST_USER}}

        proposal.handle_proposal_cancel(slack, body)

        assert _TEST_USER not in proposal._pending_proposals
        text = slack.chat_postMessage.call_args[1]["text"]
        assert "취소" in text


# ── FR-B16: 참석자 기반 업체 역추론 ──────────────────────────────


class TestAttendeeCompanyInference:
    """FR-B16: 참석자 이메일 도메인/인물 파일에서 소속 회사 역추론"""

    def test_infer_from_person_file(self):
        """인물 파일의 소속 정보에서 업체명 추출"""
        mock_creds = MagicMock()
        person_content = "# 홍길동\n\n## 기본 정보\n- 소속: [[카카오]]\n"

        with patch.object(before, "drive") as mock_drive:
            mock_drive.get_person_info.return_value = (person_content, "person_123")
            result = before._infer_company_from_attendees(
                [{"email": "hong@kakao.com", "displayName": "홍길동"}],
                creds=mock_creds,
                contacts_folder_id="contacts_123",
            )

        assert result == "카카오"

    def test_infer_from_known_domain(self):
        """알려진 도메인 매핑에서 업체명 추출"""
        result = before._infer_company_from_attendees(
            [{"email": "user@samsung.com"}],
        )
        assert result == "삼성전자"

    def test_infer_from_unknown_domain(self):
        """알려지지 않은 도메인에서 2차 도메인 추출"""
        result = before._infer_company_from_attendees(
            [{"email": "user@shinhan.co.kr"}],
        )
        assert result == "Shinhan"

    def test_skip_internal_domains(self):
        """내부 도메인은 무시"""
        result = before._infer_company_from_attendees(
            [{"email": "user@parametacorp.com"}],
        )
        assert result == ""

    def test_skip_gmail_domain(self):
        """일반 이메일 도메인은 무시"""
        result = before._infer_company_from_attendees(
            [{"email": "user@gmail.com"}],
        )
        assert result == ""

    def test_no_attendees(self):
        """참석자 없으면 빈 문자열"""
        result = before._infer_company_from_attendees([])
        assert result == ""


# ── 프롬프트 템플릿 ──────────────────────────────────────────────


class TestProposalPrompts:
    """제안서 프롬프트 템플릿 로딩"""

    def test_intake_prompt_loads(self):
        """proposal_intake_prompt 로딩 + 변수 치환"""
        from prompts.briefing import proposal_intake_prompt
        result = proposal_intake_prompt(
            minutes_body="회의 내용",
            company_info="기업 정보",
            knowledge="서비스 지식",
        )
        assert "회의 내용" in result
        assert "기업 정보" in result

    def test_generate_prompt_loads(self):
        """proposal_generate_prompt 로딩 + 변수 치환"""
        from prompts.briefing import proposal_generate_prompt
        result = proposal_generate_prompt(
            title="AI 제안",
            purpose="목적",
            target="대상",
            scope="범위",
            key_points="항목",
            background="배경",
            minutes_body="회의록",
        )
        assert "AI 제안" in result
        assert "목적" in result


# ── user_store.update_meeting_proposal_flag ───────────────────────


class TestProposalFlag:
    """meeting_index has_proposal 플래그"""

    def test_update_proposal_flag_exists(self):
        """update_meeting_proposal_flag 함수 존재"""
        from store import user_store
        assert hasattr(user_store, "update_meeting_proposal_flag")
