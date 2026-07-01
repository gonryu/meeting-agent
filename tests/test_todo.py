"""agents/todo.py + store.user_store Todo CRUD 단위 테스트"""
import os
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# 환경변수
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
os.environ.setdefault("INTERNAL_DOMAINS", "parametacorp.com,iconloop.com")

# Anthropic / Google 차단 후 import
with patch("anthropic.Anthropic"), \
     patch("tools.calendar._service"), \
     patch("tools.drive._service"), \
     patch("tools.gmail._service"):
    from store import user_store
    from agents import todo as todo_agent


_TEST_USER = "UTEST"


def _slack():
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "111.222"}
    return client


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """각 테스트마다 임시 SQLite DB. user_store._DB_PATH 만 교체."""
    db_path = str(tmp_path / "test_todo.db")
    monkeypatch.setattr(user_store, "_DB_PATH", db_path)
    user_store.init_db()
    yield db_path


@pytest.fixture
def patch_drive(monkeypatch):
    """Drive 호출을 모두 모킹 — 네트워크/credential 차단."""
    monkeypatch.setattr(todo_agent, "_safe_drive_upsert", lambda *a, **k: None)
    monkeypatch.setattr(todo_agent, "_safe_drive_history", lambda *a, **k: None)


# ── _format_due (FR-T6) ──────────────────────────────────────

class TestFormatDue:
    def test_no_due(self):
        emoji, label = todo_agent._format_due(None)
        assert emoji == "⚪"

    def test_overdue(self):
        today = datetime(2026, 4, 28, tzinfo=todo_agent.KST)
        emoji, label = todo_agent._format_due("2026-04-26", today=today)
        assert emoji == "🔴"
        assert "지남" in label

    def test_today(self):
        today = datetime(2026, 4, 28, tzinfo=todo_agent.KST)
        emoji, label = todo_agent._format_due("2026-04-28", today=today)
        assert emoji == "🔴"

    def test_imminent(self):
        today = datetime(2026, 4, 28, tzinfo=todo_agent.KST)
        emoji, _ = todo_agent._format_due("2026-04-30", today=today)
        assert emoji == "🟠"

    def test_caution(self):
        today = datetime(2026, 4, 28, tzinfo=todo_agent.KST)
        emoji, _ = todo_agent._format_due("2026-05-04", today=today)
        assert emoji == "🟡"

    def test_far_future(self):
        today = datetime(2026, 4, 28, tzinfo=todo_agent.KST)
        emoji, _ = todo_agent._format_due("2026-06-01", today=today)
        assert emoji == "⚪"


# ── _parse_todo_text ─────────────────────────────────────────

class TestParseTodoText:
    def test_explicit_date_tomorrow(self):
        """'내일' → today+1"""
        today = datetime.now(todo_agent.KST).date()
        tomorrow = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        llm_response = json.dumps({
            "task": "AIA 제안서 이슈 작성",
            "category": "work",
            "due_date": tomorrow,
            "original_text": "내일까지 AIA 제안서 이슈 작성",
            "_is_past_completion": False,
        })
        with patch("agents.todo.generate_text", return_value=llm_response):
            result = todo_agent._parse_todo_text("내일까지 AIA 제안서 이슈 작성")
        assert result["due_date"] == tomorrow
        assert result["category"] == "work"
        assert "AIA" in result["task"]

    def test_personal_hashtag(self):
        """#개인 → category personal"""
        llm_response = json.dumps({
            "task": "병원 예약",
            "category": "personal",
            "due_date": None,
            "original_text": "병원 예약 #개인",
            "_is_past_completion": False,
        })
        with patch("agents.todo.generate_text", return_value=llm_response):
            result = todo_agent._parse_todo_text("병원 예약 #개인")
        assert result["category"] == "personal"

    def test_invalid_category_falls_back_to_work(self):
        """LLM이 잘못된 카테고리 반환 시 work 폴백"""
        llm_response = json.dumps({
            "task": "회의 준비",
            "category": "invalid",
            "due_date": None,
            "original_text": "회의 준비",
            "_is_past_completion": False,
        })
        with patch("agents.todo.generate_text", return_value=llm_response):
            result = todo_agent._parse_todo_text("회의 준비")
        assert result["category"] == "work"

    def test_invalid_due_date_format_dropped(self):
        """잘못된 날짜 형식은 None으로"""
        llm_response = json.dumps({
            "task": "테스트",
            "category": "work",
            "due_date": "not-a-date",
            "original_text": "테스트",
            "_is_past_completion": False,
        })
        with patch("agents.todo.generate_text", return_value=llm_response):
            result = todo_agent._parse_todo_text("테스트")
        assert result["due_date"] is None

    def test_llm_failure_fallback(self):
        """LLM 실패 시 task=원문, category=work, due=None"""
        with patch("agents.todo.generate_text", side_effect=Exception("LLM 다운")):
            result = todo_agent._parse_todo_text("뭐든 추가")
        assert result["task"] == "뭐든 추가"
        assert result["category"] == "work"
        assert result["due_date"] is None


# ── handle_add ───────────────────────────────────────────────

class TestHandleAdd:
    def test_add_with_tomorrow(self, temp_db, patch_drive):
        slack = _slack()
        today = datetime.now(todo_agent.KST).date()
        tomorrow = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        llm_response = json.dumps({
            "task": "AIA 제안서 이슈 작성",
            "category": "work",
            "due_date": tomorrow,
            "original_text": "내일까지 AIA 제안서 이슈 작성",
            "_is_past_completion": False,
        })
        with patch("agents.todo.generate_text", return_value=llm_response):
            todo_id = todo_agent.handle_add(
                slack, _TEST_USER, "내일까지 AIA 제안서 이슈 작성",
                channel="C123", thread_ts="111.222",
            )
        assert todo_id is not None
        # DB row
        row = user_store.get_todo(_TEST_USER, todo_id)
        assert row["task"] == "AIA 제안서 이슈 작성"
        assert row["category"] == "work"
        assert row["due_date"] == tomorrow
        assert row["status"] == "open"
        assert row["source"] == "slack:C123:111.222"
        # 히스토리
        history = user_store.get_todo_history(_TEST_USER)
        assert any(h["event"] == "created" and h["todo_id"] == todo_id for h in history)
        # Slack 메시지 — [🗑️ 삭제] 버튼 포함
        msg = slack.chat_postMessage.call_args
        blocks = msg.kwargs.get("blocks", [])
        action_btns = [b for b in blocks if b.get("type") == "actions"]
        assert action_btns, "삭제 버튼 actions 블록 누락"
        btn_labels = [el["text"]["text"] for el in action_btns[0]["elements"]]
        assert any("삭제" in t for t in btn_labels)

    def test_add_personal_hashtag(self, temp_db, patch_drive):
        slack = _slack()
        llm_response = json.dumps({
            "task": "병원 예약",
            "category": "personal",
            "due_date": None,
            "original_text": "병원 예약 #개인",
            "_is_past_completion": False,
        })
        with patch("agents.todo.generate_text", return_value=llm_response):
            todo_id = todo_agent.handle_add(slack, _TEST_USER, "병원 예약 #개인")
        row = user_store.get_todo(_TEST_USER, todo_id)
        assert row["category"] == "personal"

    def test_add_blank_task_rejected(self, temp_db, patch_drive):
        slack = _slack()
        llm_response = json.dumps({
            "task": "",
            "category": "work",
            "due_date": None,
            "original_text": "할 일 추가",
            "_is_past_completion": False,
        })
        with patch("agents.todo.generate_text", return_value=llm_response):
            result = todo_agent.handle_add(slack, _TEST_USER, "할 일 추가")
        assert result is None


# ── handle_list ──────────────────────────────────────────────

class TestHandleList:
    def test_list_sorted_by_due_then_open(self, temp_db, patch_drive):
        # due 없는 것 / 가까운 due / 먼 due 섞어서 추가
        user_store.add_todo(_TEST_USER, "B (먼 미래)", "work", "2026-12-31")
        user_store.add_todo(_TEST_USER, "A (가까운 미래)", "work", "2026-04-30")
        user_store.add_todo(_TEST_USER, "C (due 없음)", "work", None)
        active = user_store.list_active_todos(_TEST_USER)
        assert active[0]["task"] == "A (가까운 미래)"
        assert active[1]["task"] == "B (먼 미래)"
        assert active[2]["task"] == "C (due 없음)"

    def test_handle_list_posts_blocks(self, temp_db, patch_drive):
        slack = _slack()
        user_store.add_todo(_TEST_USER, "테스트 작업", "work", "2026-04-30")
        todo_agent.handle_list(slack, _TEST_USER)
        msg = slack.chat_postMessage.call_args
        blocks = msg.kwargs.get("blocks", [])
        # 헤더 + context + 카테고리 섹션 등 다수 블록
        assert len(blocks) >= 3
        # 텍스트 어딘가에 task가 들어 있어야 함
        texts = json.dumps(blocks, ensure_ascii=False)
        assert "테스트 작업" in texts


# ── 완료 / 취소 / 삭제 ───────────────────────────────────────

class TestCloseFlows:
    def test_complete_creates_history(self, temp_db, patch_drive):
        slack = _slack()
        tid = user_store.add_todo(_TEST_USER, "완료할 일", "work")
        user_store.log_todo_history(tid, _TEST_USER, "created",
                                    payload={"task": "완료할 일"})

        ok = todo_agent.handle_complete(slack, _TEST_USER, tid)
        assert ok
        row = user_store.get_todo(_TEST_USER, tid)
        assert row["status"] == "done"
        events = [h["event"] for h in user_store.get_todo_history(_TEST_USER)]
        assert "created" in events
        assert "completed" in events

    def test_cancel_with_reason(self, temp_db, patch_drive):
        slack = _slack()
        tid = user_store.add_todo(_TEST_USER, "취소할 일", "work")
        ok = todo_agent.handle_cancel(slack, _TEST_USER, tid, reason="다른 도구 사용")
        assert ok
        row = user_store.get_todo(_TEST_USER, tid)
        assert row["status"] == "cancelled"
        # 히스토리 payload에 reason 포함
        history = user_store.get_todo_history(_TEST_USER)
        cancel_events = [h for h in history if h["event"] == "cancelled"]
        assert cancel_events
        payload = json.loads(cancel_events[0]["payload"])
        assert payload.get("reason") == "다른 도구 사용"

    def test_delete(self, temp_db, patch_drive):
        slack = _slack()
        tid = user_store.add_todo(_TEST_USER, "삭제 대상", "personal")
        ok = todo_agent.handle_delete(slack, _TEST_USER, tid)
        assert ok
        row = user_store.get_todo(_TEST_USER, tid)
        assert row["status"] == "deleted"

    def test_close_other_user_todo_fails(self, temp_db, patch_drive):
        slack = _slack()
        tid = user_store.add_todo("UOTHER", "남의 일", "work")
        ok = todo_agent.handle_complete(slack, _TEST_USER, tid)
        assert not ok

    def test_text_match_ambiguous(self, temp_db, patch_drive):
        slack = _slack()
        user_store.add_todo(_TEST_USER, "AIA 제안서 이슈", "work")
        user_store.add_todo(_TEST_USER, "AIA 검토 일정", "work")
        ok = todo_agent.handle_complete(slack, _TEST_USER, "AIA")
        assert not ok
        # Slack 메시지에 "여러 개" 문구
        text = slack.chat_postMessage.call_args.kwargs.get("text", "")
        assert "여러 개" in text

    def test_number_is_display_index_not_db_id(self, temp_db, patch_drive):
        # 자연어 "N번" = 표시 순번(work→personal→ai), DB id 아님 (#2 버그 수정)
        slack = _slack()
        t1 = user_store.add_todo(_TEST_USER, "work-A", "work")          # 표시 1
        t2 = user_store.add_todo(_TEST_USER, "personal-B", "personal")  # 표시 3 (personal 뒤로)
        t3 = user_store.add_todo(_TEST_USER, "work-C", "work")          # 표시 2
        # _active_ordered = [work-A, work-C, personal-B] → "2번" = work-C(t3), DB id 2(t2) 아님
        ok = todo_agent.handle_delete(slack, _TEST_USER, "2")
        assert ok
        assert user_store.get_todo(_TEST_USER, t3)["status"] == "deleted"  # 표시 2번 = t3
        assert user_store.get_todo(_TEST_USER, t2)["status"] == "open"     # DB id 2는 안 지워짐

    def test_button_int_target_uses_db_id(self, temp_db, patch_drive):
        # 버튼은 DB id(int)를 넘김 → 순번 아닌 그 id 삭제
        slack = _slack()
        t1 = user_store.add_todo(_TEST_USER, "일1", "work")
        t2 = user_store.add_todo(_TEST_USER, "일2", "work")
        ok = todo_agent.handle_delete(slack, _TEST_USER, t2)   # int DB id
        assert ok
        assert user_store.get_todo(_TEST_USER, t2)["status"] == "deleted"
        assert user_store.get_todo(_TEST_USER, t1)["status"] == "open"


# ── handle_update ────────────────────────────────────────────

class TestHandleUpdate:
    def test_update_due_date(self, temp_db, patch_drive):
        slack = _slack()
        tid = user_store.add_todo(_TEST_USER, "수정 테스트", "work", "2026-05-01")
        ok = todo_agent.handle_update(slack, _TEST_USER, tid, "due_date", "2026-05-10")
        assert ok
        row = user_store.get_todo(_TEST_USER, tid)
        assert row["due_date"] == "2026-05-10"
        # updated 히스토리
        history = user_store.get_todo_history(_TEST_USER)
        upd = [h for h in history if h["event"] == "updated"]
        assert upd
        payload = json.loads(upd[0]["payload"])
        assert payload["field"] == "due_date"
        assert payload["new"] == "2026-05-10"

    def test_update_invalid_field(self, temp_db, patch_drive):
        slack = _slack()
        tid = user_store.add_todo(_TEST_USER, "테스트", "work")
        ok = todo_agent.handle_update(slack, _TEST_USER, tid, "status", "done")
        assert not ok

    def test_update_invalid_due_format(self, temp_db, patch_drive):
        slack = _slack()
        tid = user_store.add_todo(_TEST_USER, "테스트", "work")
        ok = todo_agent.handle_update(slack, _TEST_USER, tid, "due_date", "내일")
        assert not ok


# ── build_todo_block (FR-T5/T6) ─────────────────────────────

class TestBuildTodoBlock:
    def test_empty_returns_empty_list(self, temp_db):
        assert todo_agent.build_todo_block(_TEST_USER) == []

    def test_emoji_markers_correct(self, temp_db):
        today = datetime(2026, 4, 28, tzinfo=todo_agent.KST)
        # 긴급 / 임박 / 주의 / 일반 섞기
        user_store.add_todo(_TEST_USER, "긴급일", "work", "2026-04-26")
        user_store.add_todo(_TEST_USER, "임박일", "work", "2026-04-29")
        user_store.add_todo(_TEST_USER, "주의일", "work", "2026-05-04")
        user_store.add_todo(_TEST_USER, "일반일", "personal", None)

        blocks = todo_agent.build_todo_block(_TEST_USER, today_date=today)
        assert blocks, "블록이 생성되어야 함"
        text = "".join(b["text"]["text"] for b in blocks if b.get("type") == "section")
        # 각 색상 이모지 모두 포함
        assert "🔴" in text
        assert "🟠" in text
        assert "🟡" in text
        assert "⚪" in text

    def test_has_delete_and_complete_buttons(self, temp_db):
        # 브리핑 Todo에서 바로 완료/삭제할 수 있어야 함(#1)
        tid = user_store.add_todo(_TEST_USER, "지울 일", "work")
        blocks = todo_agent.build_todo_block(_TEST_USER)
        actions = [b for b in blocks if b.get("type") == "actions"]
        assert actions, "완료/삭제 버튼 actions 블록이 있어야 함"
        aids = {e["action_id"] for b in actions for e in b["elements"]}
        assert "todo_delete_btn" in aids and "todo_complete_btn" in aids
        # 버튼 value는 DB id
        vals = {e["value"] for b in actions for e in b["elements"]}
        assert str(tid) in vals

    def test_overflow_collapses(self, temp_db):
        today = datetime(2026, 4, 28, tzinfo=todo_agent.KST)
        for i in range(20):
            user_store.add_todo(_TEST_USER, f"task-{i}", "work", "2026-05-15")
        blocks = todo_agent.build_todo_block(_TEST_USER, today_date=today)
        text = "".join(b["text"]["text"] for b in blocks if b.get("type") == "section")
        assert "외" in text  # "…외 N건" 폴드


# ── _disable_clicked_action_block (버튼 클릭 후 메시지 갱신) ──

class TestDisableClickedActionBlock:
    """클릭된 actions 블록만 '✓ {status}'로 교체하고 chat_update로 원본 메시지 갱신."""

    @staticmethod
    def _msg_blocks(*todo_ids):
        """build_todo_block 유사 구조 — 각 todo 마다 section + actions(고유 block_id)."""
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "📋 오늘의 Todo"}}]
        for tid in todo_ids:
            blocks.append({"type": "section", "block_id": f"sec_{tid}",
                           "text": {"type": "mrkdwn", "text": f"item {tid}"}})
            blocks.append({"type": "actions", "block_id": f"act_{tid}", "elements": [
                {"type": "button", "action_id": "todo_delete_btn", "value": str(tid)},
            ]})
        return blocks

    def _body(self, *, block_id, value, blocks):
        return {
            "user": {"id": _TEST_USER},
            "channel": {"id": "D123"},
            "message": {"ts": "999.888", "blocks": blocks, "text": "📋 오늘의 Todo"},
            "actions": [{"action_id": "todo_delete_btn", "block_id": block_id, "value": value}],
        }

    def test_matches_by_block_id_even_when_value_empty(self):
        # Slack이 message.blocks의 버튼 value를 비워 에코하는 상황 재현:
        # value 매칭은 실패해도 block_id로 교체되어야 함(관측된 무응답 버그의 근본 원인).
        echoed = self._msg_blocks(3)
        for b in echoed:
            if b.get("type") == "actions":
                for el in b["elements"]:
                    el["value"] = ""          # Slack이 value를 비워 보냄
        client = _slack()
        body = self._body(block_id="act_3", value="3", blocks=echoed)
        todo_agent._disable_clicked_action_block(client, body, "삭제됨")

        client.chat_update.assert_called_once()
        new_blocks = client.chat_update.call_args.kwargs["blocks"]
        # act_3 actions 블록이 context '✓ 삭제됨'으로 교체됐는지
        assert not any(b.get("block_id") == "act_3" and b.get("type") == "actions" for b in new_blocks)
        ctx_text = "".join(
            e["text"] for b in new_blocks if b.get("type") == "context" for e in b["elements"])
        assert "삭제됨" in ctx_text

    def test_falls_back_to_value_when_no_block_id(self):
        # 구버전/일부 payload는 block_id가 없을 수 있음 → value 폴백으로 교체.
        client = _slack()
        body = self._body(block_id="", value="7", blocks=self._msg_blocks(7))
        todo_agent._disable_clicked_action_block(client, body, "완료됨")
        new_blocks = client.chat_update.call_args.kwargs["blocks"]
        assert any(b.get("type") == "context" for b in new_blocks)

    def test_other_todos_actions_preserved(self):
        # 같은 메시지 내 다른 todo의 버튼은 유지되어야 함.
        client = _slack()
        body = self._body(block_id="act_3", value="3", blocks=self._msg_blocks(3, 5))
        todo_agent._disable_clicked_action_block(client, body, "삭제됨")
        new_blocks = client.chat_update.call_args.kwargs["blocks"]
        # act_5는 그대로, act_3만 사라짐
        act_ids = {b.get("block_id") for b in new_blocks if b.get("type") == "actions"}
        assert "act_5" in act_ids and "act_3" not in act_ids

    def test_skips_when_no_channel_or_ts(self):
        client = _slack()
        body = {"user": {"id": _TEST_USER}, "actions": [{"block_id": "act_3", "value": "3"}]}
        todo_agent._disable_clicked_action_block(client, body, "삭제됨")
        client.chat_update.assert_not_called()


# ── parse_close_command (자연어) ────────────────────────────

class TestParseCloseCommand:
    def test_complete(self):
        action, target = todo_agent.parse_close_command("AIA 제안서 이슈 작성 완료")
        assert action == "complete"
        assert "AIA" in target

    def test_cancel(self):
        action, target = todo_agent.parse_close_command("워드프레스 블로그 이전 취소")
        assert action == "cancel"

    def test_delete(self):
        action, target = todo_agent.parse_close_command("그 항목 삭제")
        assert action == "delete"

    def test_no_match(self):
        action, target = todo_agent.parse_close_command("그냥 일상 메시지")
        assert action is None


# ── DB 마이그레이션 idempotency ─────────────────────────────

class TestUserStoreTodoMigration:
    def test_init_db_creates_tables(self, temp_db):
        with sqlite3.connect(temp_db) as conn:
            cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            names = {row[0] for row in cursor.fetchall()}
        assert "todos" in names
        assert "todo_history" in names

    def test_init_db_idempotent(self, temp_db):
        user_store.init_db()
        user_store.init_db()  # 중복 호출 OK
