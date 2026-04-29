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
        text = blocks[0]["text"]["text"]
        # 각 색상 이모지 모두 포함
        assert "🔴" in text
        assert "🟠" in text
        assert "🟡" in text
        assert "⚪" in text

    def test_overflow_collapses(self, temp_db):
        today = datetime(2026, 4, 28, tzinfo=todo_agent.KST)
        for i in range(20):
            user_store.add_todo(_TEST_USER, f"task-{i}", "work", "2026-05-15")
        blocks = todo_agent.build_todo_block(_TEST_USER, today_date=today)
        text = blocks[0]["text"]["text"]
        assert "외" in text  # "…외 N건" 폴드


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
