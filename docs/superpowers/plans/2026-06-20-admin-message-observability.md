# 관리자 메시지 관측(Observability) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 봇이 보내는 모든 Slack 메시지를 중앙에서 기록하고, 관리자 페이지에서 글로벌 피드·사용자별·대시보드로 조회할 수 있게 한다.

**Architecture:** 시작 시 `app.client`(WebClient)의 send 메서드 3종을 in-place로 감싸고 Bolt 미들웨어로 리스너 주입 client도 감싸 모든 발송을 `message_log` 테이블에 적재한다(로깅 실패는 발송에 영향 없음). FastAPI 관리자 API에 조회 엔드포인트를 더하고, 기존 프론트엔드 SPA에 뷰를 추가한다.

**Tech Stack:** Python 3.12, SQLite(`store/user_store.py`), slack_sdk/slack_bolt, FastAPI(`server/admin.py`), APScheduler, 바닐라 JS SPA(`frontend/`), pytest.

> **테스트 실행은 반드시 `.venv/bin/python -m pytest`** — 시스템 python3(3.9)는 `X | None` 문법으로 수집이 깨진다.
> **테스트의 `ENCRYPTION_KEY`는 유효한 32바이트 키**(`base64.urlsafe_b64encode(b"0"*32)`)를 쓴다 — 기존 `test_user_store.py`의 30바이트 키 버그를 답습하지 않는다.

**참조 스펙:** `docs/superpowers/specs/2026-06-20-admin-message-observability-design.md`

---

## File Structure

- **Create** `tools/slack_logger.py` — send 메서드 로깅 래퍼 + 순수 헬퍼(`_recipient_kind`, `_infer_category`, `_truncate_blocks`). 책임: "발송을 가로채 기록".
- **Modify** `store/user_store.py` — `init_db`에 `message_log` 테이블 추가 + 함수(`log_message`/`list_messages`/`get_message`/`prune_messages`/`message_stats`) + `admin_counts` 유지. 책임: 영속화.
- **Modify** `main.py` — 시작 시 로깅 설치 + Bolt 미들웨어 + 일일 prune 잡. 책임: 배선.
- **Modify** `server/admin.py` — messages 엔드포인트 3종 + dashboard 확장. 책임: 조회 API.
- **Modify** `frontend/index.html`, `frontend/app.js`, `frontend/style.css` — 메시지 피드·사용자 상세·대시보드 stats. 책임: UI.
- **Create** `tests/test_message_log_store.py`, `tests/test_slack_logger.py`, `tests/test_admin_messages.py`.
- **Modify** `CLAUDE.md` — 메시지 관측 섹션 추가.

---

## Task 1: message_log 테이블 + 스토어 함수

**Files:**
- Modify: `store/user_store.py` (`init_db` 내부 + 파일 끝 함수 추가)
- Test: `tests/test_message_log_store.py`

- [ ] **Step 1: 실패하는 테스트 작성**

Create `tests/test_message_log_store.py`:

```python
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
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_message_log_store.py -q`
Expected: FAIL — `AttributeError: module 'store.user_store' has no attribute 'log_message'`

- [ ] **Step 3: `init_db`에 테이블 추가**

`store/user_store.py`의 `init_db()` 안, `todo_history` 인덱스 생성(라인 ~220) **직후**, `init_db` 함수 본문 끝에 추가:

```python
        # 발송 메시지 로그 (관리자 관측)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS message_log (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                TEXT NOT NULL,
                method            TEXT NOT NULL,
                channel           TEXT,
                recipient_user_id TEXT,
                recipient_kind    TEXT,
                thread_ts         TEXT,
                text              TEXT,
                blocks_json       TEXT,
                category          TEXT,
                ok                INTEGER NOT NULL DEFAULT 1,
                error             TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msglog_ts ON message_log(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msglog_recipient ON message_log(recipient_user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msglog_category ON message_log(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msglog_ok ON message_log(ok)")
```

- [ ] **Step 4: 스토어 함수 추가**

`store/user_store.py` 파일 **맨 끝**에 추가:

```python
# ── 메시지 로그 (관리자 관측) ────────────────────────────────

def log_message(*, method: str, channel: str = None, recipient_user_id: str = None,
                recipient_kind: str = None, thread_ts: str = None, text: str = None,
                blocks_json: str = None, category: str = None, ok: bool = True,
                error: str = None) -> int:
    """발송 메시지 1건 기록. Returns: message_log id"""
    now = datetime.now().isoformat()
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO message_log
               (ts, method, channel, recipient_user_id, recipient_kind, thread_ts,
                text, blocks_json, category, ok, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, method, channel, recipient_user_id, recipient_kind, thread_ts,
             text, blocks_json, category, 1 if ok else 0, error),
        )
        return cur.lastrowid


def list_messages(*, user_id: str = None, category: str = None, ok: int = None,
                  date_from: str = None, date_to: str = None, q: str = None,
                  limit: int = 100, offset: int = 0) -> list[dict]:
    """메시지 로그 조회 (최신순). 인자 미지정 시 전체."""
    query = "SELECT * FROM message_log"
    conditions: list[str] = []
    params: list = []
    if user_id:
        conditions.append("recipient_user_id = ?"); params.append(user_id)
    if category:
        conditions.append("category = ?"); params.append(category)
    if ok is not None:
        conditions.append("ok = ?"); params.append(ok)
    if date_from:
        conditions.append("ts >= ?"); params.append(date_from)
    if date_to:
        conditions.append("ts <= ?"); params.append(date_to)
    if q:
        conditions.append("text LIKE ?"); params.append(f"%{q}%")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with _conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_message(message_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM message_log WHERE id = ?", (message_id,)
        ).fetchone()
    return dict(row) if row else None


def prune_messages(before_iso: str) -> int:
    """before_iso(ISO8601) 이전 메시지 삭제. Returns: 삭제 건수."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM message_log WHERE ts < ?", (before_iso,))
        return cur.rowcount


def message_stats(*, date_from: str = None) -> dict:
    """date_from 이후(없으면 전체) 발송 집계."""
    cond = ""
    params: list = []
    if date_from:
        cond = " WHERE ts >= ?"
        params = [date_from]
    fail_cond = (cond + " AND ok = 0") if cond else " WHERE ok = 0"
    active_cond = (cond + " AND recipient_user_id IS NOT NULL") if cond else " WHERE recipient_user_id IS NOT NULL"
    with _conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM message_log{cond}", params).fetchone()[0]
        failures = conn.execute(f"SELECT COUNT(*) FROM message_log{fail_cond}", params).fetchone()[0]
        active = conn.execute(
            f"SELECT COUNT(DISTINCT recipient_user_id) FROM message_log{active_cond}", params
        ).fetchone()[0]
        by_cat = conn.execute(
            f"SELECT category, COUNT(*) AS c FROM message_log{cond} GROUP BY category", params
        ).fetchall()
    return {
        "total": total,
        "failures": failures,
        "active_recipients": active,
        "by_category": {(r["category"] or "other"): r["c"] for r in by_cat},
    }
```

- [ ] **Step 5: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_message_log_store.py -q`
Expected: PASS (9 passed)

- [ ] **Step 6: 커밋**

```bash
git add store/user_store.py tests/test_message_log_store.py
git commit -m "feat(store): message_log 테이블 + 조회/집계/prune 함수"
```

---

## Task 2: slack_logger 모듈 (포착 래퍼)

**Files:**
- Create: `tools/slack_logger.py`
- Test: `tests/test_slack_logger.py`

- [ ] **Step 1: 실패하는 테스트 작성**

Create `tests/test_slack_logger.py`:

```python
"""tools/slack_logger.py — 발송 로깅 래퍼 단위 테스트"""
import base64
import os

os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

import pytest
from unittest.mock import MagicMock, patch
from slack_sdk.errors import SlackApiError

import tools.slack_logger as slack_logger


class FakeClient:
    """WebClient 대역 — install_logging이 인스턴스 속성으로 메서드를 교체할 수 있다."""
    pass


def _fake():
    c = FakeClient()
    c.chat_postMessage = MagicMock(return_value={"ok": True, "ts": "1.2"})
    c.chat_update = MagicMock(return_value={"ok": True})
    c.chat_postEphemeral = MagicMock(return_value={"ok": True})
    c.conversations_open = MagicMock(return_value={"ok": True})
    return c


class TestPureHelpers:
    def test_recipient_kind(self):
        assert slack_logger._recipient_kind("U123") == ("dm", "U123")
        assert slack_logger._recipient_kind("D123") == ("dm", None)
        assert slack_logger._recipient_kind("C123") == ("channel", None)
        assert slack_logger._recipient_kind(None) == (None, None)

    def test_infer_category(self):
        assert slack_logger._infer_category("오늘의 미팅 브리핑입니다", None) == "briefing"
        assert slack_logger._infer_category("회의록 초안이 준비됐어요", None) == "minutes"
        assert slack_logger._infer_category("그냥 평범한 메시지", None) == "other"

    def test_truncate_blocks_caps_size(self):
        big = [{"type": "section", "text": "x" * 30000}]
        out = slack_logger._truncate_blocks(big)
        assert out.endswith("…(truncated)")
        assert slack_logger._truncate_blocks(None) is None


class TestInstallLogging:
    def test_success_logs_ok_and_returns_response(self):
        c = _fake()
        slack_logger.install_logging(c)
        with patch.object(slack_logger.user_store, "log_message") as mock_log:
            resp = c.chat_postMessage(channel="U1", text="안녕")
        assert resp == {"ok": True, "ts": "1.2"}
        kw = mock_log.call_args.kwargs
        assert kw["ok"] is True
        assert kw["method"] == "post"
        assert kw["recipient_user_id"] == "U1"
        assert kw["text"] == "안녕"

    def test_failure_logs_and_reraises(self):
        c = _fake()
        c.chat_postMessage.side_effect = SlackApiError("boom", {"error": "channel_not_found"})
        slack_logger.install_logging(c)
        with patch.object(slack_logger.user_store, "log_message") as mock_log:
            with pytest.raises(SlackApiError):
                c.chat_postMessage(channel="U1", text="x")
        kw = mock_log.call_args.kwargs
        assert kw["ok"] is False
        assert kw["error"] == "channel_not_found"

    def test_logging_failure_never_breaks_send(self):
        c = _fake()
        slack_logger.install_logging(c)
        with patch.object(slack_logger.user_store, "log_message",
                          side_effect=RuntimeError("db down")):
            resp = c.chat_postMessage(channel="U1", text="x")
        assert resp == {"ok": True, "ts": "1.2"}  # 발송은 정상

    def test_non_logged_method_passthrough(self):
        c = _fake()
        slack_logger.install_logging(c)
        with patch.object(slack_logger.user_store, "log_message") as mock_log:
            c.conversations_open(users="U1")
        mock_log.assert_not_called()

    def test_idempotent(self):
        c = _fake()
        slack_logger.install_logging(c)
        first = c.chat_postMessage
        slack_logger.install_logging(c)  # 두 번째 호출은 무시돼야 함
        assert c.chat_postMessage is first
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_slack_logger.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.slack_logger'` (또는 AttributeError)

- [ ] **Step 3: 모듈 구현**

Create `tools/slack_logger.py`:

```python
"""Slack 발송 메시지 로깅 — WebClient send 메서드 in-place 래핑.

app.client(및 Bolt 리스너 주입 client)의 chat_postMessage/chat_update/
chat_postEphemeral를 감싸 store.user_store.message_log에 적재한 뒤 원본을 그대로
위임한다. 로깅 실패는 절대 실제 발송을 막지 않는다(best-effort).
"""
import json
import logging

from slack_sdk.errors import SlackApiError

from store import user_store

log = logging.getLogger(__name__)

# send 메서드명 → message_log.method 값
_LOGGED_METHODS = {
    "chat_postMessage": "post",
    "chat_update": "update",
    "chat_postEphemeral": "ephemeral",
}
_MAX_BLOCKS_BYTES = 20_000
_INSTALLED_FLAG = "_meeting_agent_logged"


def _recipient_kind(channel):
    """channel ID 접두사로 수신 유형 판정. Returns: (kind, user_id)"""
    if not channel:
        return None, None
    if channel.startswith("U"):
        return "dm", channel            # DM: channel == user_id
    if channel.startswith("D"):
        return "dm", None               # DM 채널 ID만 — user_id 미상
    return "channel", None              # C…(채널) 등


def _infer_category(text, blocks):
    """text/blocks 마커로 메시지 유형 추정 (best-effort)."""
    hay = text or ""
    try:
        if blocks:
            hay += " " + json.dumps(blocks, ensure_ascii=False)
    except Exception:
        pass
    if "브리핑" in hay or "오늘의 미팅" in hay:
        return "briefing"
    if "회의록" in hay:
        return "minutes"
    if "액션" in hay or "할 일" in hay or "리마인더" in hay:
        return "action_item"
    if "미팅 시작" in hay or "분 후" in hay:
        return "meeting_alarm"
    if "회의실" in hay or "예약" in hay:
        return "room"
    if "제안서" in hay:
        return "proposal"
    if "피드백" in hay:
        return "feedback"
    return "other"


def _truncate_blocks(blocks):
    if not blocks:
        return None
    try:
        s = json.dumps(blocks, ensure_ascii=False)
    except Exception:
        return None
    if len(s.encode("utf-8")) > _MAX_BLOCKS_BYTES:
        return s[:_MAX_BLOCKS_BYTES] + "…(truncated)"
    return s


def _record(method_label, kwargs, ok, error):
    """발송 1건 기록 — best-effort. 예외는 삼킨다(발송에 영향 금지)."""
    try:
        channel = kwargs.get("channel")
        kind, uid = _recipient_kind(channel)
        text = kwargs.get("text")
        blocks = kwargs.get("blocks")
        user_store.log_message(
            method=method_label,
            channel=channel,
            recipient_user_id=uid,
            recipient_kind=kind,
            thread_ts=kwargs.get("thread_ts"),
            text=text,
            blocks_json=_truncate_blocks(blocks),
            category=_infer_category(text, blocks),
            ok=ok,
            error=error,
        )
    except Exception as e:
        log.warning(f"메시지 로깅 실패(발송에는 영향 없음): {e}")


def _make_wrapper(original, method_label):
    def wrapped(*args, **kwargs):
        try:
            resp = original(*args, **kwargs)
        except SlackApiError as e:
            try:
                err = e.response["error"]
            except Exception:
                err = str(e)
            _record(method_label, kwargs, ok=False, error=err)
            raise
        _record(method_label, kwargs, ok=True, error=None)
        return resp
    return wrapped


def install_logging(client):
    """WebClient 인스턴스의 send 3종을 in-place로 감싼다 (idempotent).

    인스턴스 속성으로 메서드를 덮어써 클래스 메서드를 가린다. app.client 및 Bolt
    리스너 주입 client에 동일 호출해 양쪽 발송을 모두 포착한다.
    """
    if getattr(client, _INSTALLED_FLAG, False):
        return client
    for name, label in _LOGGED_METHODS.items():
        original = getattr(client, name)
        setattr(client, name, _make_wrapper(original, label))
    setattr(client, _INSTALLED_FLAG, True)
    return client
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_slack_logger.py -q`
Expected: PASS (9 passed)

- [ ] **Step 5: 커밋**

```bash
git add tools/slack_logger.py tests/test_slack_logger.py
git commit -m "feat(tools): slack_logger — 발송 로깅 래퍼(in-place, best-effort)"
```

---

## Task 3: main.py 배선 — 로깅 설치 + 미들웨어 + prune 잡

**Files:**
- Modify: `main.py` (import 블록 ~85, `app = App(...)` ~96, 스케줄러 등록 ~352-363, 잡 함수 영역)

> 이 태스크는 통합 배선이라 자동 단위테스트 대신 import 스모크 + 라이브 검증을 한다.

- [ ] **Step 1: import 추가**

`main.py`의 도구 import 블록(`from tools import stt` 부근, 라인 ~87)에 추가:

```python
from tools import slack_logger
```

- [ ] **Step 2: 시작 시 로깅 설치 + Bolt 미들웨어**

`main.py`의 `app = App(token=os.getenv("SLACK_BOT_TOKEN"))` (라인 ~96) **바로 다음 줄**에 추가:

```python

# 발송 메시지 로깅 설치 — 스케줄러가 쓰는 app.client(직접) + 리스너 주입 client(미들웨어)
slack_logger.install_logging(app.client)


@app.middleware
def _install_message_logging(context, next):
    """리스너 주입 client도 감싼다 (app.client와 동일 인스턴스면 idempotent로 무시)."""
    try:
        if getattr(context, "client", None) is not None:
            slack_logger.install_logging(context.client)
    except Exception:
        pass
    next()
```

- [ ] **Step 3: prune 잡 함수 추가**

`main.py`의 다른 `scheduled_*` 잡 함수들 근처(예: `scheduled_feedback_digest` 정의 부근, 라인 ~166)에 추가:

```python
def scheduled_message_log_prune():
    """메시지 로그 보존기간(기본 90일) 초과분 정리 — 매일 03:00 KST."""
    try:
        from datetime import timedelta
        days = int(os.getenv("MESSAGE_LOG_RETENTION_DAYS", "90"))
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        deleted = user_store.prune_messages(cutoff)
        log.info(f"메시지 로그 prune: {deleted}건 삭제 (cutoff={cutoff})")
    except Exception:
        log.exception("메시지 로그 prune 실패")
```

> `datetime`은 main.py 상단에 이미 import돼 있다(스케줄러 잡들이 사용). `os`/`log`도 동일.

- [ ] **Step 4: 스케줄러 잡 등록**

`main.py`의 스케줄러 등록 블록(라인 ~363, `scheduled_fast_transcript_check` 등록 다음)에 추가:

```python
scheduler.add_job(scheduled_message_log_prune, "cron", hour=3, minute=0)
```

- [ ] **Step 5: import 스모크 확인**

Run: `.venv/bin/python -c "import tools.slack_logger; print('slack_logger ok')"`
Expected: `slack_logger ok`

Run (구문/배선 점검 — main 전체 import는 부수효과가 크므로 컴파일만 확인):
`.venv/bin/python -m py_compile main.py && echo "main compiles"`
Expected: `main compiles`

- [ ] **Step 6: 라이브 검증 (수동)**

서버 기동 후 발송이 실제로 기록되는지 확인한다:

```bash
bash start.sh
sleep 5
# 본인에게 DM으로 아무 명령(예: "설정")을 보내거나 브리핑 트리거 후:
.venv/bin/python -c "import store.user_store as u; rows=u.list_messages(limit=5); print(len(rows), [r['category'] for r in rows])"
```
Expected: 최근 발송이 행으로 잡힘(예: `3 ['other', 'briefing', ...]`). 0건이면 미들웨어/설치 지점을 재점검.

- [ ] **Step 7: 커밋**

```bash
git add main.py
git commit -m "feat(main): 발송 로깅 설치 + Bolt 미들웨어 + 일일 prune 잡"
```

---

## Task 4: 대시보드 stats 확장

**Files:**
- Modify: `server/admin.py` (`api_dashboard` ~87, import ~10)
- Test: `tests/test_admin_messages.py` (대시보드 부분 — Task 5에서 함께 검증되나 여기서 stats만 우선)

- [ ] **Step 1: dashboard 엔드포인트 확장**

`server/admin.py`의 `api_dashboard`(라인 ~87)를 다음으로 교체:

```python
@router.get("/dashboard")
def api_dashboard(_: str = Depends(_require_admin)):
    today_start = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()
    return {
        "counts": user_store.admin_counts(),
        "recent_feedback": _enrich_feedback(user_store.list_all_feedback(limit=5)),
        "message_stats": user_store.message_stats(date_from=today_start),
    }
```

> `datetime`은 `server/admin.py:10`에서 이미 import됨 (`from datetime import datetime`).

- [ ] **Step 2: 컴파일 확인**

Run: `.venv/bin/python -m py_compile server/admin.py && echo ok`
Expected: `ok`

> 동작 검증은 Task 5의 API 테스트에서 함께 수행한다.

- [ ] **Step 3: 커밋**

```bash
git add server/admin.py
git commit -m "feat(admin): 대시보드에 오늘자 메시지 stats 추가"
```

---

## Task 5: messages API 엔드포인트

**Files:**
- Modify: `server/admin.py` (엔드포인트 추가)
- Test: `tests/test_admin_messages.py`

- [ ] **Step 1: 실패하는 테스트 작성**

Create `tests/test_admin_messages.py`:

```python
"""server/admin.py — 메시지 로그 조회 API 단위 테스트"""
import base64
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test")
os.environ["ADMIN_PASSWORD"] = "test-admin-pw"

import pytest
from unittest.mock import patch

with patch("anthropic.Anthropic"):
    from fastapi.testclient import TestClient
    from server.oauth import app
    import store.user_store as user_store

_AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:test-admin-pw").decode()}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin-pw")
    db_path = str(tmp_path / "test_admin_msg.db")
    monkeypatch.setattr(user_store, "_DB_PATH", db_path)
    user_store.init_db()
    user_store.log_message(method="post", channel="U1", recipient_user_id="U1",
                           recipient_kind="dm", text="아침 브리핑", category="briefing", ok=True)
    user_store.log_message(method="post", channel="U2", recipient_user_id="U2",
                           recipient_kind="dm", text="회의록 초안", category="minutes", ok=True)
    user_store.log_message(method="post", channel="C9", recipient_kind="channel",
                           text="발송 실패건", category="other", ok=False, error="channel_not_found")
    return TestClient(app)


class TestAuth:
    def test_requires_auth(self, client):
        assert client.get("/admin/api/messages").status_code == 401


class TestMessagesFeed:
    def test_list_all_newest_first(self, client):
        r = client.get("/admin/api/messages", headers=_AUTH)
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 3
        assert items[0]["text"] == "발송 실패건"

    def test_filter_by_user(self, client):
        r = client.get("/admin/api/messages?user=U2", headers=_AUTH)
        items = r.json()["items"]
        assert len(items) == 1 and items[0]["category"] == "minutes"

    def test_filter_by_category(self, client):
        r = client.get("/admin/api/messages?category=briefing", headers=_AUTH)
        assert len(r.json()["items"]) == 1

    def test_filter_failures(self, client):
        r = client.get("/admin/api/messages?ok=0", headers=_AUTH)
        items = r.json()["items"]
        assert len(items) == 1 and items[0]["error"] == "channel_not_found"

    def test_search_text(self, client):
        r = client.get("/admin/api/messages?q=브리핑", headers=_AUTH)
        assert len(r.json()["items"]) == 1


class TestMessageDetail:
    def test_detail_ok(self, client):
        feed = client.get("/admin/api/messages", headers=_AUTH).json()["items"]
        mid = feed[0]["id"]
        r = client.get(f"/admin/api/messages/{mid}", headers=_AUTH)
        assert r.status_code == 200 and r.json()["text"] == "발송 실패건"

    def test_detail_404(self, client):
        assert client.get("/admin/api/messages/99999", headers=_AUTH).status_code == 404


class TestUserMessages:
    def test_user_messages(self, client):
        r = client.get("/admin/api/users/U1/messages", headers=_AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["user_id"] == "U1"
        assert len(body["items"]) == 1


class TestDashboardStats:
    def test_dashboard_includes_message_stats(self, client):
        r = client.get("/admin/api/dashboard", headers=_AUTH)
        assert r.status_code == 200
        stats = r.json()["message_stats"]
        assert stats["total"] == 3
        assert stats["failures"] == 1
        assert stats["by_category"]["briefing"] == 1
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_admin_messages.py -q`
Expected: FAIL — messages 엔드포인트 404 (TestMessagesFeed 실패). `TestDashboardStats`는 Task 4 덕에 통과할 수 있음.

- [ ] **Step 3: 엔드포인트 구현**

`server/admin.py`의 `_enrich_feedback` 함수(라인 ~78) **다음**에 추가:

```python
def _enrich_messages(items: list[dict]) -> list[dict]:
    """메시지 항목에 수신자 Slack 프로필 이름 주입 (DM 한정)."""
    out = []
    for it in items:
        uid = it.get("recipient_user_id")
        name = _lookup_profile(uid)["name"] if uid else ""
        out.append({**it, "recipient_name": name})
    return out
```

그리고 `server/admin.py`의 feedback 엔드포인트들 다음(프롬프트 섹션 시작 전, 라인 ~155)에 추가:

```python
# ── 메시지 로그 (관리자 관측) ────────────────────────────────

def _int_param(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@router.get("/messages")
def api_messages(request: Request, _: str = Depends(_require_admin)):
    qp = request.query_params
    ok_val = None
    if qp.get("ok") in ("0", "1"):
        ok_val = int(qp.get("ok"))
    items = user_store.list_messages(
        user_id=qp.get("user") or None,
        category=qp.get("category") or None,
        ok=ok_val,
        date_from=qp.get("date_from") or None,
        date_to=qp.get("date_to") or None,
        q=qp.get("q") or None,
        limit=min(_int_param(qp.get("limit"), 100), 500),
        offset=_int_param(qp.get("offset"), 0),
    )
    return {"items": _enrich_messages(items)}


@router.get("/messages/{message_id}")
def api_message_detail(message_id: int, _: str = Depends(_require_admin)):
    msg = user_store.get_message(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="메시지를 찾을 수 없습니다")
    return _enrich_messages([msg])[0]


@router.get("/users/{uid}/messages")
def api_user_messages(uid: str, _: str = Depends(_require_admin)):
    items = user_store.list_messages(user_id=uid, limit=200)
    return {"user_id": uid, "items": _enrich_messages(items)}
```

> `Request`, `HTTPException`, `Depends`는 `server/admin.py:13`에서 이미 import됨.

- [ ] **Step 4: 테스트 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_admin_messages.py -q`
Expected: PASS (10 passed)

- [ ] **Step 5: 회귀 확인 (관리자 기존 테스트)**

Run: `.venv/bin/python -m pytest tests/test_admin_prompts.py tests/test_admin_messages.py -q`
Expected: PASS (전부 통과)

- [ ] **Step 6: 커밋**

```bash
git add server/admin.py tests/test_admin_messages.py
git commit -m "feat(admin): 메시지 로그 조회 API(피드/상세/사용자별)"
```

---

## Task 6: 프론트엔드 — 메시지 피드 + 상세

**Files:**
- Modify: `frontend/index.html` (nav), `frontend/app.js` (라우트/렌더), `frontend/style.css` (검색창)

> 프론트엔드는 빌드/테스트 도구가 없다. 검증은 브라우저 수동 확인.

- [ ] **Step 1: nav 링크 추가**

`frontend/index.html`의 `<nav>` 안, 피드백 링크 다음에 추가:

```html
    <a href="#/messages">메시지</a>
```

- [ ] **Step 2: 카테고리 라벨 맵 추가**

`frontend/app.js`의 `CATEGORY_LABEL` 상수(라인 ~10) **다음**에 추가:

```javascript
  const MSG_CATEGORY_LABEL = {
    briefing: "📢 브리핑",
    minutes: "📝 회의록",
    action_item: "✅ 액션",
    meeting_alarm: "⏰ 미팅알람",
    room: "🏢 회의실",
    proposal: "📄 제안서",
    feedback: "💬 피드백",
    other: "··· 기타",
  };
```

- [ ] **Step 3: 메시지 피드 + 상세 렌더 함수 추가**

`frontend/app.js`의 라우터 정의(`const routes = {` 라인 ~377) **직전**에 추가:

```javascript
  // ── 메시지 로그 ─────────────────────────────────────────
  function msgFilterParams() {
    const qs = location.hash.split("?")[1] || "";
    return new URLSearchParams(qs);
  }

  async function renderMessages() {
    const params = msgFilterParams();
    if (params.get("id")) return renderMessageDetail(params.get("id"));

    setLoading("메시지 불러오는 중...");
    const data = await api("/messages?" + params.toString());
    const items = data.items || [];

    const cat = params.get("category") || "";
    const okv = params.get("ok");
    const userv = params.get("user") || "";
    const qv = params.get("q") || "";

    const catLink = (name, label) => {
      const p = new URLSearchParams(params);
      if (name) p.set("category", name); else p.delete("category");
      p.delete("id");
      return `<a href="#/messages?${p.toString()}" class="${cat === name ? "active" : ""}">${label}</a>`;
    };
    const okLink = (val, label) => {
      const p = new URLSearchParams(params);
      if (val === null) p.delete("ok"); else p.set("ok", val);
      p.delete("id");
      const cur = okv == null ? "" : okv;
      return `<a href="#/messages?${p.toString()}" class="${String(cur) === String(val ?? "") ? "active" : ""}">${label}</a>`;
    };

    const filters = `
      <div class="filters">
        <div class="filter-group">
          <span class="filter-label">유형</span>
          ${catLink("", "전체")}${catLink("briefing", "브리핑")}${catLink("minutes", "회의록")}${catLink("action_item", "액션")}${catLink("meeting_alarm", "미팅알람")}${catLink("other", "기타")}
        </div>
        <div class="filter-group">
          <span class="filter-label">발송</span>
          ${okLink(null, "전체")}${okLink("1", "성공")}${okLink("0", "실패")}
        </div>
        <div class="filter-group">
          <input id="msg-search" class="msg-search" placeholder="본문 검색…" value="${escapeHtml(qv)}">
          <button id="msg-search-btn" class="act-btn">검색</button>
          ${userv ? `<span class="muted small">수신자 필터: <code>${escapeHtml(userv)}</code></span>` : ""}
        </div>
      </div>
    `;

    let body;
    if (items.length === 0) {
      body = '<div class="card"><div class="empty">메시지가 없습니다.</div></div>';
    } else {
      const rows = items.map((m) => `
        <tr class="msg-row" data-id="${m.id}" style="cursor:pointer">
          <td class="nowrap">${escapeHtml(fmtDt(m.ts))}</td>
          <td>
            <div>${escapeHtml(m.recipient_name || "—")}</div>
            <div class="muted small"><code>${escapeHtml(m.recipient_user_id || m.channel || "")}</code></div>
          </td>
          <td><span class="tag">${escapeHtml(MSG_CATEGORY_LABEL[m.category] || m.category || "기타")}</span></td>
          <td>${m.ok ? '<span class="tag ok">성공</span>' : '<span class="tag warn">실패</span>'}</td>
          <td>${escapeHtml((m.text || "").slice(0, 80))}${(m.text || "").length > 80 ? "…" : ""}</td>
        </tr>`).join("");
      body = `
        <div class="card">
          <h2>메시지 로그 <span class="muted">(${items.length}건)</span></h2>
          <table>
            <thead><tr><th>시각</th><th>수신자</th><th>유형</th><th>발송</th><th>본문</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      `;
    }
    main.innerHTML = filters + body;

    const searchBtn = document.getElementById("msg-search-btn");
    const searchInput = document.getElementById("msg-search");
    const doSearch = () => {
      const p = new URLSearchParams(params);
      const v = searchInput.value.trim();
      if (v) p.set("q", v); else p.delete("q");
      p.delete("id");
      location.hash = "#/messages?" + p.toString();
    };
    searchBtn.addEventListener("click", doSearch);
    searchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });
  }

  async function renderMessageDetail(id) {
    setLoading("메시지 불러오는 중...");
    const m = await api("/messages/" + encodeURIComponent(id));
    const back = new URLSearchParams(msgFilterParams());
    back.delete("id");
    let blocks = "";
    if (m.blocks_json) {
      let pretty = m.blocks_json;
      try { pretty = JSON.stringify(JSON.parse(m.blocks_json), null, 2); } catch (_) {}
      blocks = `<h3>blocks</h3><pre class="content">${escapeHtml(pretty)}</pre>`;
    }
    main.innerHTML = `
      <div class="card">
        <h2><a href="#/messages?${back.toString()}" class="small">← 목록</a>&nbsp;메시지 #${escapeHtml(String(m.id))}</h2>
        <p class="muted small">
          ${escapeHtml(fmtDt(m.ts))} · ${escapeHtml(m.method)} ·
          수신자 ${escapeHtml(m.recipient_name || m.recipient_user_id || m.channel || "—")} ·
          ${m.ok ? "성공" : "실패: " + escapeHtml(m.error || "")}
        </p>
        <h3>text</h3>
        <pre class="content">${escapeHtml(m.text || "(없음)")}</pre>
        ${blocks}
      </div>
    `;
  }
```

- [ ] **Step 4: 라우트 등록 + 행 클릭 위임**

`frontend/app.js`의 `routes` 객체(라인 ~377)에 추가:

```javascript
    "#/messages": renderMessages,
```

그리고 기존 `main.addEventListener("click", ...)` (피드백 버튼 위임, 라인 ~257) 블록 **다음**에 행 클릭 위임 추가:

```javascript
  // 메시지 행 클릭 → 상세
  main.addEventListener("click", (e) => {
    const row = e.target.closest(".msg-row");
    if (!row) return;
    const p = new URLSearchParams(msgFilterParams());
    p.set("id", row.getAttribute("data-id"));
    location.hash = "#/messages?" + p.toString();
  });
```

- [ ] **Step 5: 검색창 CSS 추가**

`frontend/style.css` **맨 끝**에 추가:

```css
.msg-search {
  padding: 6px 10px;
  border: 1px solid #ccc;
  border-radius: 6px;
  font-size: 14px;
  min-width: 200px;
}
```

- [ ] **Step 6: 수동 검증**

```bash
bash start.sh   # 또는 이미 실행 중이면 생략
```
브라우저에서 `http://localhost:8000/admin/` → 비밀번호 입력 → "메시지" 탭:
- 발송 기록이 최신순 표로 보이는가
- 유형/발송 필터 링크가 동작하는가
- 본문 검색이 동작하는가
- 행 클릭 시 상세(text + blocks)가 보이는가

Expected: 위 4가지 모두 정상.

- [ ] **Step 7: 커밋**

```bash
git add frontend/index.html frontend/app.js frontend/style.css
git commit -m "feat(frontend): 메시지 로그 피드 + 필터 + 상세 뷰"
```

---

## Task 7: 프론트엔드 — 사용자 상세 + 대시보드 stats

**Files:**
- Modify: `frontend/app.js` (사용자 행 링크 + 상세 렌더 + 대시보드 stats)

- [ ] **Step 1: 대시보드에 메시지 stats 카드 추가**

`frontend/app.js`의 `renderDashboard()`에서 `const fb = data.recent_feedback || [];` 다음에 추가:

```javascript
    const ms = data.message_stats || { total: 0, failures: 0, active_recipients: 0 };
```

그리고 같은 함수의 `stats` 템플릿 리터럴 안, 마지막 `</div>`(stats div 닫힘) **직전**에 stat 3개 추가:

```javascript
        <div class="stat"><div class="label">오늘 발송</div>
          <div class="value">${ms.total}</div></div>
        <div class="stat"><div class="label">오늘 발송 실패</div>
          <div class="value">${ms.failures}</div>
          <div class="sub">${ms.failures > 0 ? '<a href="#/messages?ok=0">실패 보기 →</a>' : "정상"}</div></div>
        <div class="stat"><div class="label">오늘 수신 사용자</div>
          <div class="value">${ms.active_recipients}</div></div>
```

- [ ] **Step 2: 사용자 행을 상세로 연결**

`frontend/app.js`의 `renderUsers()`에서 사용자 이름 셀(`<div>${escapeHtml(u.name || "—")}</div>`)을 링크로 교체:

```javascript
          <div><a href="#/users?uid=${encodeURIComponent(u.slack_user_id)}">${escapeHtml(u.name || "—")}</a></div>
```

- [ ] **Step 3: renderUsers에 상세 분기 추가**

`frontend/app.js`의 `renderUsers()` 함수 **맨 첫 줄**(`setLoading(...)` 전)에 추가:

```javascript
    const uidParam = new URLSearchParams(location.hash.split("?")[1] || "").get("uid");
    if (uidParam) return renderUserDetail(uidParam);
```

- [ ] **Step 4: renderUserDetail 함수 추가**

`frontend/app.js`의 `renderUsers` 함수 정의 **다음**에 추가:

```javascript
  async function renderUserDetail(uid) {
    setLoading("사용자 상세 불러오는 중...");
    const [users, msgData] = await Promise.all([
      api("/users"),
      api("/users/" + encodeURIComponent(uid) + "/messages"),
    ]);
    const u = (users || []).find((x) => x.slack_user_id === uid) || { slack_user_id: uid };
    const items = msgData.items || [];
    const rows = items.map((m) => `
      <tr class="msg-row" data-id="${m.id}" style="cursor:pointer">
        <td class="nowrap">${escapeHtml(fmtDt(m.ts))}</td>
        <td><span class="tag">${escapeHtml(MSG_CATEGORY_LABEL[m.category] || m.category || "기타")}</span></td>
        <td>${m.ok ? '<span class="tag ok">성공</span>' : '<span class="tag warn">실패</span>'}</td>
        <td>${escapeHtml((m.text || "").slice(0, 80))}${(m.text || "").length > 80 ? "…" : ""}</td>
      </tr>`).join("");
    main.innerHTML = `
      <div class="card">
        <h2><a href="#/users" class="small">← 사용자 목록</a>&nbsp;${escapeHtml(u.name || uid)}</h2>
        <p class="muted small">
          <code>${escapeHtml(uid)}</code> · ${escapeHtml(u.email || "")} ·
          Drive ${u.has_drive ? "🟢" : "—"} / Trello ${u.has_trello ? "🟢" : "—"} / Dreamplus ${u.has_dreamplus ? "🟢" : "—"}
        </p>
      </div>
      <div class="card">
        <h2>받은 메시지 <span class="muted">(${items.length}건)</span></h2>
        ${items.length === 0
          ? '<div class="empty">기록된 메시지가 없습니다.</div>'
          : `<table><thead><tr><th>시각</th><th>유형</th><th>발송</th><th>본문</th></tr></thead><tbody>${rows}</tbody></table>`}
      </div>
    `;
  }
```

> 사용자 상세의 행 클릭도 Task 6에서 추가한 `.msg-row` 위임으로 메시지 상세(`#/messages?id=N`)로 이동한다.

- [ ] **Step 5: 수동 검증**

브라우저에서:
- "대시보드": 오늘 발송/실패/수신 사용자 stat 3개가 보이는가, 실패>0이면 "실패 보기" 링크가 메시지 피드로 가는가
- "사용자" → 이름 클릭 → 그 사용자가 받은 메시지 목록 + 프로필이 보이는가
- 사용자 상세에서 메시지 행 클릭 → 메시지 상세로 이동하는가

Expected: 모두 정상.

- [ ] **Step 6: 커밋**

```bash
git add frontend/app.js
git commit -m "feat(frontend): 사용자 상세 메시지 + 대시보드 메시지 stats"
```

---

## Task 8: 문서 갱신 + 전체 회귀

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: CLAUDE.md에 섹션 추가**

`CLAUDE.md`의 "관리자 페이지" 섹션 **다음**에 추가:

```markdown
### 메시지 관측(발송 로그)

봇이 보내는 모든 Slack 메시지를 중앙에서 기록해 관리자가 사후 점검할 수 있습니다.

**포착:** `tools/slack_logger.install_logging()`이 `app.client`의 `chat_postMessage`/`chat_update`/`chat_postEphemeral`를 in-place로 감싸고, Bolt 미들웨어가 리스너 주입 client도 감쌉니다(idempotent). 로깅 실패는 발송에 영향을 주지 않습니다(best-effort).

**저장:** `message_log` 테이블(`store/user_store.py`) — `ts/method/channel/recipient_user_id/recipient_kind/thread_ts/text/blocks_json/category/ok/error`. 수신자 이름은 저장하지 않고 조회 시점에 `_lookup_profile`로 해석. `category`는 text/blocks 마커 기반 best-effort 추정.

**조회:** 관리자 페이지 `메시지` 탭(글로벌 피드 + 필터 + 본문검색 + 상세), `사용자` 탭의 사용자 클릭 시 상세, 대시보드 오늘자 stats.

**보존:** 기본 90일(`MESSAGE_LOG_RETENTION_DAYS`), 매일 03:00 KST `scheduled_message_log_prune` 잡이 정리.
```

- [ ] **Step 2: 전체 테스트 회귀**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: 신규 28개(9+9+10) 통과 + 기존 통과 유지. (기존에 깨져 있던 `test_user_store` 7건·`test_drive_minutes` 1건·`test_v2_phase2_1` 1건은 이 작업과 무관한 사전 결함 — 신규 실패가 없어야 함)

- [ ] **Step 3: 커밋**

```bash
git add CLAUDE.md
git commit -m "docs: 메시지 관측 섹션 추가"
```

---

## Self-Review 결과 (작성자 확인)

- **스펙 커버리지:** §5 데이터모델→T1, §6 포착→T2/T3, §7 API→T4/T5, §8 프론트→T6/T7, §9 보존→T3(prune), §10 테스트→각 태스크 TDD. 누락 없음.
- **타입 일관성:** `log_message`/`list_messages`/`get_message`/`prune_messages`/`message_stats` 시그니처가 T1 정의와 T2(`_record`)·T5(엔드포인트) 호출에서 일치. `install_logging`은 T2 정의·T3 호출 일치. `_recipient_kind` 반환 `(kind, uid)` 순서 일치.
- **플레이스홀더:** 없음(모든 코드 단계에 실제 코드 포함).
- **알려진 한계(스펙 §12):** Bolt 리스너 주입 client가 app.client와 다른 인스턴스여도 미들웨어가 포착하나, 발송이 kwargs 대신 positional이면 `channel` 미기록(코드베이스는 전부 kwargs). T3 Step 6에서 라이브 검증.
