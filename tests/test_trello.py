"""tools/trello.py 및 After Agent Trello 연동 단위 테스트"""
import os
# import 전에 환경변수 설정
os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
os.environ.setdefault("TRELLO_API_KEY", "test-trello-key")
os.environ.setdefault("TRELLO_BOARD_ID", "test-board-id")

import json
import pytest
from unittest.mock import patch, MagicMock

# TrelloClient 및 외부 서비스 차단
with patch("trello.TrelloClient"), \
     patch("google.genai.Client"), \
     patch("anthropic.Anthropic"), \
     patch("tools.calendar._service"), \
     patch("tools.drive._service"), \
     patch("tools.gmail._service"):
    import tools.trello as trello_mod
    from tools.trello import (
        find_card_by_name,
        get_card_context,
        create_card,
        add_checklist_items,
        add_comment,
        _format_checklist_item,
        _DummyCard,
        _DummyChecklist,
        clear_user_cache,
    )
    import agents.after as after


_TEST_USER_ID = "UTEST"


def _slack():
    """mock Slack client"""
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "111.222"}
    return client


def _mock_card(name="카카오", card_id="card123"):
    """py-trello Card mock"""
    card = MagicMock()
    card.name = name
    card.id = card_id
    card.url = f"https://trello.com/c/{card_id}"

    mock_list = MagicMock()
    mock_list.name = "Contact/Meeting"
    card.get_list.return_value = mock_list

    checklist = MagicMock()
    checklist.name = "Action Items"
    checklist.items = [
        {"name": "[김민환] 기술 검토 (기한: 2026-04-15)", "state": "incomplete"},
        {"name": "[이수연] 레퍼런스 공유 (기한: 미정)", "state": "complete"},
        {"name": "[홍길동] 계약서 초안 (기한: 2026-04-20)", "state": "incomplete"},
    ]
    card.checklists = [checklist]

    card.comments = [
        {
            "memberCreator": {"fullName": "홍길동"},
            "data": {"text": "레퍼런스 케이스 공유 부탁"},
        },
        {
            "memberCreator": {"fullName": "김민환"},
            "data": {"text": "다음 미팅 전까지 검토 완료 예정"},
        },
    ]
    return card


# ── _format_checklist_item ──────────────────────────────────

class TestFormatChecklistItem:
    def test_full_item(self):
        result = _format_checklist_item({
            "assignee": "김민환",
            "content": "기술 검토 문서 작성",
            "due_date": "2026-04-15",
        })
        assert result == "[김민환] 기술 검토 문서 작성 (기한: 2026-04-15)"

    def test_no_due_date(self):
        result = _format_checklist_item({
            "assignee": "이수연",
            "content": "레퍼런스 공유",
            "due_date": None,
        })
        assert result == "[이수연] 레퍼런스 공유 (기한: 미정)"

    def test_empty_assignee(self):
        result = _format_checklist_item({
            "assignee": "",
            "content": "문서 작성",
            "due_date": "2026-04-15",
        })
        assert result == "[] 문서 작성 (기한: 2026-04-15)"


# ── DummyCard / DummyChecklist ──────────────────────────────

class TestDummyObjects:
    def test_dummy_card(self):
        card = _DummyCard("테스트업체")
        assert card.name == "테스트업체"
        assert "dry-run" in card.id
        assert card.url
        assert len(card.checklists) == 1

    def test_dummy_checklist_add_item(self):
        cl = _DummyChecklist()
        cl.add_checklist_item("테스트 항목")
        assert len(cl.items) == 1
        assert cl.items[0]["name"] == "테스트 항목"

    def test_dummy_card_comment(self):
        card = _DummyCard("테스트")
        card.comment("테스트 코멘트")
        assert len(card.comments) == 1

    def test_dummy_card_add_checklist(self):
        card = _DummyCard("테스트")
        cl = card.add_checklist("New Checklist")
        assert cl.name == "New Checklist"
        assert len(card.checklists) == 2


# ── clear_user_cache ────────────────────────────────────────

class TestClearUserCache:
    def test_clears_cache(self):
        trello_mod._client_cache["UTEST"] = MagicMock()
        trello_mod._board_cache["UTEST"] = MagicMock()
        clear_user_cache("UTEST")
        assert "UTEST" not in trello_mod._client_cache
        assert "UTEST" not in trello_mod._board_cache

    def test_noop_for_unknown_user(self):
        clear_user_cache("UNKNOWN")  # 에러 없이 통과


# ── find_card_by_name ───────────────────────────────────────

class TestFindCardByName:
    def test_card_found(self):
        card = _mock_card("카카오")
        with patch.object(trello_mod, "_find_card", return_value=card), \
             patch.object(trello_mod, "_is_dry_run", return_value=False):
            result = find_card_by_name(_TEST_USER_ID, "카카오")
            assert result is not None
            assert result["card_name"] == "카카오"
            assert result["list_name"] == "Contact/Meeting"

    def test_card_not_found(self):
        with patch.object(trello_mod, "_find_card", return_value=None), \
             patch.object(trello_mod, "_is_dry_run", return_value=False):
            result = find_card_by_name(_TEST_USER_ID, "없는업체")
            assert result is None

    def test_dry_run_returns_none(self):
        with patch.object(trello_mod, "_is_dry_run", return_value=True):
            result = find_card_by_name(_TEST_USER_ID, "카카오")
            assert result is None


# ── get_card_context ────────────────────────────────────────

class TestGetCardContext:
    def test_card_with_context(self):
        card = _mock_card()
        with patch.object(trello_mod, "_find_card", return_value=card), \
             patch.object(trello_mod, "_is_dry_run", return_value=False):
            result = get_card_context(_TEST_USER_ID, "카카오")
            assert result["card_name"] == "카카오"
            assert len(result["incomplete_items"]) == 2
            assert "기술 검토" in result["incomplete_items"][0]
            assert len(result["recent_comments"]) == 2

    def test_card_not_found(self):
        with patch.object(trello_mod, "_find_card", return_value=None), \
             patch.object(trello_mod, "_is_dry_run", return_value=False):
            result = get_card_context(_TEST_USER_ID, "없는업체")
            assert result == {}

    def test_dry_run_returns_empty(self):
        with patch.object(trello_mod, "_is_dry_run", return_value=True):
            result = get_card_context(_TEST_USER_ID, "카카오")
            assert result == {}

    def test_empty_checklists(self):
        card = _mock_card()
        card.checklists = []
        with patch.object(trello_mod, "_find_card", return_value=card), \
             patch.object(trello_mod, "_is_dry_run", return_value=False):
            result = get_card_context(_TEST_USER_ID, "카카오")
            assert result["incomplete_items"] == []


# ── create_card ─────────────────────────────────────────────

class TestCreateCard:
    def test_create_success(self):
        mock_list = MagicMock()
        mock_list.name = "Contact/Meeting"
        new_card = MagicMock()
        new_card.id = "new123"
        new_card.name = "새업체"
        new_card.url = "https://trello.com/c/new123"
        mock_list.add_card.return_value = new_card

        mock_board = MagicMock()
        mock_board.list_lists.return_value = [mock_list]

        with patch.object(trello_mod, "_board_for_user", return_value=mock_board), \
             patch.object(trello_mod, "_is_dry_run", return_value=False):
            result = create_card(_TEST_USER_ID, "새업체")
            assert result["card_name"] == "새업체"
            mock_list.add_card.assert_called_once()

    def test_list_not_found(self):
        mock_list = MagicMock()
        mock_list.name = "다른리스트"
        mock_board = MagicMock()
        mock_board.list_lists.return_value = [mock_list]

        with patch.object(trello_mod, "_board_for_user", return_value=mock_board), \
             patch.object(trello_mod, "_is_dry_run", return_value=False):
            result = create_card(_TEST_USER_ID, "새업체")
            assert result is None

    def test_dry_run_returns_dummy(self):
        with patch.object(trello_mod, "_is_dry_run", return_value=True):
            result = create_card(_TEST_USER_ID, "새업체")
            assert result is not None
            assert "dry-run" in result["card_id"]

    def test_board_none(self):
        with patch.object(trello_mod, "_board_for_user", return_value=None), \
             patch.object(trello_mod, "_is_dry_run", return_value=False):
            result = create_card(_TEST_USER_ID, "새업체")
            assert result is None


# ── add_checklist_items ─────────────────────────────────────

class TestAddChecklistItems:
    def test_add_to_existing_checklist(self):
        card = _mock_card()
        items = [
            {"assignee": "김민환", "content": "기술 검토", "due_date": "2026-04-15"},
            {"assignee": "이수연", "content": "자료 공유", "due_date": None},
        ]
        with patch.object(trello_mod, "_find_card", return_value=card), \
             patch.object(trello_mod, "_is_dry_run", return_value=False):
            count = add_checklist_items(_TEST_USER_ID, "카카오", items)
            assert count == 2
            checklist = card.checklists[0]
            assert checklist.add_checklist_item.call_count == 2

    def test_empty_items(self):
        count = add_checklist_items(_TEST_USER_ID, "카카오", [])
        assert count == 0

    def test_card_not_found_creates_new(self):
        new_card = _mock_card("새업체", "new123")
        with patch.object(trello_mod, "_find_card", side_effect=[None, new_card]), \
             patch.object(trello_mod, "create_card", return_value={"card_id": "new123", "card_name": "새업체", "url": ""}), \
             patch.object(trello_mod, "_is_dry_run", return_value=False):
            items = [{"assignee": "김", "content": "작업", "due_date": None}]
            count = add_checklist_items(_TEST_USER_ID, "새업체", items)
            assert count == 1
            trello_mod.create_card.assert_called_once_with(_TEST_USER_ID, "새업체")

    def test_dry_run(self):
        with patch.object(trello_mod, "_is_dry_run", return_value=True):
            items = [{"assignee": "김", "content": "작업", "due_date": None}]
            count = add_checklist_items(_TEST_USER_ID, "카카오", items)
            assert count == 1


# ── add_comment ─────────────────────────────────────────────

class TestAddComment:
    def test_success(self):
        card = _mock_card()
        with patch.object(trello_mod, "_find_card", return_value=card), \
             patch.object(trello_mod, "_is_dry_run", return_value=False):
            result = add_comment(_TEST_USER_ID, "카카오", "테스트 코멘트")
            assert result is True
            card.comment.assert_called_once_with("테스트 코멘트")

    def test_card_not_found(self):
        with patch.object(trello_mod, "_find_card", return_value=None), \
             patch.object(trello_mod, "_is_dry_run", return_value=False):
            result = add_comment(_TEST_USER_ID, "없는업체", "코멘트")
            assert result is False

    def test_dry_run(self):
        with patch.object(trello_mod, "_is_dry_run", return_value=True):
            result = add_comment(_TEST_USER_ID, "카카오", "코멘트")
            assert result is True


# ── After Agent: _propose_trello_registration ─────────────────

class TestProposeTrelloRegistration:
    def test_sends_buttons_when_items_exist(self):
        slack = _slack()
        items = [
            {"id": 1, "assignee": "김민환", "content": "기술 검토", "due_date": "2026-04-15", "status": "open"},
        ]
        with patch("agents.after.user_store") as mock_store, \
             patch("agents.after.trello") as mock_trello:
            mock_store.get_action_items.return_value = items
            mock_trello.find_card_by_name.return_value = {
                "card_id": "c1", "card_name": "카카오",
                "list_name": "Contact/Meeting", "url": "https://trello.com/c/c1",
            }
            after._propose_trello_registration(
                slack, user_id=_TEST_USER_ID, event_id="evt1",
                company_names=["카카오"],
            )
            slack.chat_postMessage.assert_called_once()
            call_kwargs = slack.chat_postMessage.call_args[1]
            assert "Trello" in call_kwargs["text"]
            blocks = call_kwargs["blocks"]
            action_block = [b for b in blocks if b["type"] == "actions"]
            assert len(action_block) == 1

    def test_sends_buttons_per_company(self):
        """업체가 여러 개면 각각 메시지 발송"""
        slack = _slack()
        items = [{"id": 1, "assignee": "김민환", "content": "검토", "due_date": "2026-04-15", "status": "open"}]
        with patch("agents.after.user_store") as mock_store, \
             patch("agents.after.trello") as mock_trello:
            mock_store.get_action_items.return_value = items
            mock_trello.find_card_by_name.return_value = None
            after._propose_trello_registration(
                slack, user_id=_TEST_USER_ID, event_id="evt1",
                company_names=["카카오", "네이버"],
            )
            assert slack.chat_postMessage.call_count == 2

    def test_skips_when_no_items(self):
        slack = _slack()
        with patch("agents.after.user_store") as mock_store:
            mock_store.get_action_items.return_value = []
            after._propose_trello_registration(
                slack, user_id=_TEST_USER_ID, event_id="evt1",
                company_names=["카카오"],
            )
            slack.chat_postMessage.assert_not_called()


# ── After Agent: handle_trello_register ──────────────────────

class TestHandleTrelloRegister:
    def _body(self, event_id="evt1", company="카카오"):
        return {
            "user": {"id": _TEST_USER_ID},
            "actions": [{"value": json.dumps({"event_id": event_id, "company": company})}],
        }

    def test_registers_items(self):
        slack = _slack()
        items = [
            {"assignee": "김민환", "content": "기술 검토", "due_date": "2026-04-15"},
        ]
        with patch("agents.after.user_store") as mock_store, \
             patch("agents.after.trello") as mock_trello:
            mock_store.get_action_items.return_value = items
            mock_trello.add_checklist_items.return_value = 1
            mock_trello.find_card_by_name.return_value = {
                "card_id": "c1", "card_name": "카카오",
                "list_name": "Contact/Meeting", "url": "https://trello.com/c/c1",
            }
            after.handle_trello_register(slack, self._body())
            mock_trello.add_checklist_items.assert_called_once()
            call_text = slack.chat_postMessage.call_args[1]["text"]
            assert "등록 완료" in call_text

    def test_no_items_skips(self):
        slack = _slack()
        with patch("agents.after.user_store") as mock_store:
            mock_store.get_action_items.return_value = []
            after.handle_trello_register(slack, self._body())
            call_text = slack.chat_postMessage.call_args[1]["text"]
            assert "건너뜁니다" in call_text

    def test_add_failure(self):
        slack = _slack()
        items = [{"assignee": "김", "content": "작업", "due_date": None}]
        with patch("agents.after.user_store") as mock_store, \
             patch("agents.after.trello") as mock_trello:
            mock_store.get_action_items.return_value = items
            mock_trello.add_checklist_items.return_value = 0
            after.handle_trello_register(slack, self._body())
            call_text = slack.chat_postMessage.call_args[1]["text"]
            assert "실패" in call_text


# ── After Agent: handle_trello_skip ──────────────────────────

class TestHandleTrelloSkip:
    def test_sends_skip_message(self):
        slack = _slack()
        body = {
            "user": {"id": _TEST_USER_ID},
            "actions": [{"value": "{}"}],
        }
        after.handle_trello_skip(slack, body)
        slack.chat_postMessage.assert_called_once()
        call_text = slack.chat_postMessage.call_args[1]["text"]
        assert "건너뛰었습니다" in call_text
