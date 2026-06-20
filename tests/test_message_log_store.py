"""store/user_store.py — message_log 테이블/함수 단위 테스트"""
import base64
import os

os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

import pytest
import store.user_store as user_store


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_msglog.db")
    monkeypatch.setattr(user_store, "_DB_PATH", db_path)
    user_store.init_db()


def _log(**kw):
    base = dict(method="post", channel="U1", recipient_user_id="U1",
                recipient_kind="dm", text="안녕", category="other", ok=True)
    base.update(kw)
    return user_store.log_message(**base)


class TestLogMessage:
    def test_returns_id_and_persists(self):
        mid = _log(text="첫 메시지")
        assert isinstance(mid, int)
        row = user_store.get_message(mid)
        assert row["text"] == "첫 메시지"
        assert row["ok"] == 1
        assert row["recipient_user_id"] == "U1"

    def test_get_message_missing_returns_none(self):
        assert user_store.get_message(99999) is None


class TestListMessages:
    def test_filter_by_user(self):
        _log(recipient_user_id="U1", text="a")
        _log(recipient_user_id="U2", text="b")
        rows = user_store.list_messages(user_id="U2")
        assert len(rows) == 1 and rows[0]["text"] == "b"

    def test_filter_by_category_and_ok(self):
        _log(category="briefing", ok=True, text="브리핑")
        _log(category="briefing", ok=False, text="실패", error="channel_not_found")
        assert len(user_store.list_messages(category="briefing")) == 2
        fails = user_store.list_messages(ok=0)
        assert len(fails) == 1 and fails[0]["error"] == "channel_not_found"

    def test_search_text(self):
        _log(text="아침 브리핑 본문")
        _log(text="회의록 초안")
        rows = user_store.list_messages(q="브리핑")
        assert len(rows) == 1 and "브리핑" in rows[0]["text"]

    def test_search_matches_blocks_json(self):
        """브리핑·회의록 본문은 주로 blocks에 담긴다 — text뿐 아니라 blocks_json도 검색돼야 함."""
        _log(text="안내문", blocks_json='[{"type":"section","text":"카카오페이 미팅 브리핑"}]')
        _log(text="다른 메시지", blocks_json=None)
        rows = user_store.list_messages(q="카카오페이")
        assert len(rows) == 1 and "카카오페이" in (rows[0]["blocks_json"] or "")

    def test_newest_first_and_pagination(self):
        for i in range(5):
            _log(text=f"m{i}")
        page1 = user_store.list_messages(limit=2, offset=0)
        page2 = user_store.list_messages(limit=2, offset=2)
        assert [r["text"] for r in page1] == ["m4", "m3"]
        assert [r["text"] for r in page2] == ["m2", "m1"]


class TestPruneAndStats:
    def test_prune_removes_before_cutoff(self):
        with user_store._conn() as conn:
            conn.execute(
                "INSERT INTO message_log (ts, method, ok) VALUES (?, 'post', 1)",
                ("2020-01-01T00:00:00",),
            )
        _log(text="최신")
        deleted = user_store.prune_messages("2021-01-01T00:00:00")
        assert deleted == 1
        assert len(user_store.list_messages()) == 1

    def test_message_stats(self):
        _log(category="briefing", ok=True, recipient_user_id="U1")
        _log(category="briefing", ok=True, recipient_user_id="U2")
        _log(category="minutes", ok=False, recipient_user_id="U1", error="x")
        stats = user_store.message_stats()
        assert stats["total"] == 3
        assert stats["failures"] == 1
        assert stats["active_recipients"] == 2
        assert stats["by_category"]["briefing"] == 2


class TestDirection:
    def test_default_is_outbound(self):
        mid = _log(text="발송 기본값")
        assert user_store.get_message(mid)["direction"] == "outbound"

    def test_inbound_persists(self):
        mid = _log(text="질문", direction="inbound", method="message")
        row = user_store.get_message(mid)
        assert row["direction"] == "inbound" and row["text"] == "질문"


def test_migration_adds_direction_to_old_db(tmp_path, monkeypatch):
    """구버전(직접 만든 message_log, direction 없음) DB도 init_db가 컬럼 추가."""
    db_path = str(tmp_path / "old.db")
    monkeypatch.setattr(user_store, "_DB_PATH", db_path)
    with user_store._conn() as conn:
        conn.execute("CREATE TABLE message_log "
                     "(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, "
                     " method TEXT NOT NULL, ok INTEGER NOT NULL DEFAULT 1)")
        conn.execute("INSERT INTO message_log (ts, method, ok) "
                     "VALUES ('2020-01-01T00:00:00','post',1)")
    user_store.init_db()  # ALTER로 direction 추가
    rows = user_store.list_messages()
    assert rows[0]["direction"] == "outbound"


class TestDirectionFilterAndOrder:
    def test_filter_by_direction(self):
        _log(text="나간거", direction="outbound")
        _log(text="들어온거", direction="inbound", method="message")
        rows = user_store.list_messages(direction="inbound")
        assert len(rows) == 1 and rows[0]["text"] == "들어온거"

    def test_order_asc_is_chronological(self):
        _log(text="처음")
        _log(text="나중")
        rows = user_store.list_messages(order="asc")
        assert [r["text"] for r in rows] == ["처음", "나중"]

    def test_default_order_still_desc(self):
        _log(text="처음")
        _log(text="나중")
        rows = user_store.list_messages()
        assert [r["text"] for r in rows] == ["나중", "처음"]
