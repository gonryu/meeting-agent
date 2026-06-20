# Q&A 관측 — 인바운드 캡처 + 대화 타임라인 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 관리자가 "누가 무엇을 묻고 봇이 무엇을 답했는지"를 사용자별 대화 타임라인으로 본다 (인바운드 포착 추가).

**Architecture:** 기존 `message_log`에 `direction` 컬럼 1개를 추가하고, 인바운드 행의 `recipient_user_id`에 *발신자*를 담아 한 컬럼으로 양방향을 표현한다. 인바운드는 Bolt `@app.middleware` 단일 지점에서 `slack_logger.record_inbound(body)`로 best-effort 적재(아웃바운드 래핑과 동일 패턴). 관리자 user 상세를 시간 오름차순 대화 타임라인으로 렌더링한다.

**Tech Stack:** Python, SQLite(`store/user_store.py`), Slack Bolt(`main.py`), FastAPI(`server/admin.py`), 정적 JS 프론트(`frontend/`), pytest.

**선행 스펙:** `docs/superpowers/specs/2026-06-21-qa-observability-inbound-design.md`

---

### Task 1: `direction` 컬럼 + `log_message(direction=...)`

**Files:**
- Modify: `store/user_store.py` (CREATE TABLE `message_log` ~224-242, `log_message` ~930-945)
- Test: `tests/test_message_log_store.py`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_message_log_store.py` 끝에 추가

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_message_log_store.py::TestDirection tests/test_message_log_store.py::test_migration_adds_direction_to_old_db -v`
Expected: FAIL — `KeyError: 'direction'` / `log_message() got an unexpected keyword argument 'direction'`

- [ ] **Step 3: 구현** — `store/user_store.py`

(a) CREATE TABLE `message_log`의 `error TEXT` 다음 줄에 컬럼 추가:

```python
                ok                INTEGER NOT NULL DEFAULT 1,
                error             TEXT,
                direction         TEXT NOT NULL DEFAULT 'outbound'
            )
```

(b) `message_log` 인덱스 4줄(`idx_msglog_ts`…`idx_msglog_ok`) 바로 뒤에 추가:

```python
        # 기존 DB에 direction 컬럼이 없으면 추가 (기존 행은 전부 발송 → outbound)
        try:
            conn.execute("ALTER TABLE message_log ADD COLUMN direction TEXT NOT NULL DEFAULT 'outbound'")
        except Exception:
            pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_msglog_direction ON message_log(direction)")
```

(c) `log_message` — `direction` 인자 추가 + INSERT 반영:

```python
def log_message(*, method: str, channel: str = None, recipient_user_id: str = None,
                recipient_kind: str = None, thread_ts: str = None, text: str = None,
                blocks_json: str = None, category: str = None, ok: bool = True,
                error: str = None, direction: str = "outbound") -> int:
    """메시지 1건 기록(발송=outbound 기본, 사용자 입력은 direction='inbound').

    인바운드 행은 recipient_user_id에 '발신자' id를 담는다(= 대화의 사람 쪽).
    Returns: message_log id"""
    now = datetime.now().isoformat()
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO message_log
               (ts, method, channel, recipient_user_id, recipient_kind, thread_ts,
                text, blocks_json, category, ok, error, direction)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, method, channel, recipient_user_id, recipient_kind, thread_ts,
             text, blocks_json, category, 1 if ok else 0, error, direction),
        )
        return cur.lastrowid
```

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_message_log_store.py -v`
Expected: PASS (기존 테스트 포함 전부)

- [ ] **Step 5: 커밋**

```bash
git add store/user_store.py tests/test_message_log_store.py
git commit -m "feat(msglog): message_log에 direction 컬럼 추가 (기본 outbound)"
```

---

### Task 2: `list_messages` — direction 필터 + 정렬 옵션

**Files:**
- Modify: `store/user_store.py` (`list_messages` ~948-976)
- Test: `tests/test_message_log_store.py`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_message_log_store.py`에 추가

```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_message_log_store.py::TestDirectionFilterAndOrder -v`
Expected: FAIL — `list_messages() got an unexpected keyword argument 'direction'`

- [ ] **Step 3: 구현** — `list_messages` 시그니처·본문 수정

시그니처에 `direction`·`order` 추가:

```python
def list_messages(*, user_id: str = None, category: str = None, ok: int = None,
                  direction: str = None, date_from: str = None, date_to: str = None,
                  q: str = None, order: str = "desc",
                  limit: int = 100, offset: int = 0) -> list[dict]:
    """메시지 로그 조회. order='asc'면 시간 오름차순(대화 타임라인용). 인자 미지정 시 전체."""
```

`if ok is not None:` 블록 바로 다음에 direction 조건 추가:

```python
    if direction:
        conditions.append("direction = ?"); params.append(direction)
```

`ORDER BY` 줄을 정렬 옵션 반영으로 교체:

```python
    order_sql = "ASC" if str(order).lower() == "asc" else "DESC"
    query += f" ORDER BY id {order_sql} LIMIT ? OFFSET ?"
```

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_message_log_store.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add store/user_store.py tests/test_message_log_store.py
git commit -m "feat(msglog): list_messages에 direction 필터·정렬(order) 옵션 추가"
```

---

### Task 3: `message_stats` — 발송(outbound) 한정 + inbound 카운트

**Files:**
- Modify: `store/user_store.py` (`message_stats` ~994-1017)
- Test: `tests/test_message_log_store.py`

- [ ] **Step 1: 실패하는 테스트 작성** — `TestPruneAndStats` 클래스에 추가

```python
    def test_stats_count_outbound_only(self):
        _log(category="briefing", ok=True)               # outbound
        _log(text="질문", direction="inbound", method="message")  # inbound
        stats = user_store.message_stats()
        assert stats["total"] == 1          # 발송만 카운트
        assert stats["inbound"] == 1
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_message_log_store.py::TestPruneAndStats::test_stats_count_outbound_only -v`
Expected: FAIL — `assert 2 == 1` (현재 total은 인바운드까지 셈) 또는 `KeyError: 'inbound'`

- [ ] **Step 3: 구현** — `message_stats` 전체 교체

```python
def message_stats(*, date_from: str = None) -> dict:
    """date_from 이후(없으면 전체) 발송(outbound) 집계 + inbound 건수."""
    conds = ["direction = 'outbound'"]
    params: list = []
    if date_from:
        conds.append("ts >= ?"); params.append(date_from)
    where = " WHERE " + " AND ".join(conds)
    fail_where = where + " AND ok = 0"
    active_where = where + " AND recipient_user_id IS NOT NULL"
    in_where = " WHERE direction = 'inbound'" + (" AND ts >= ?" if date_from else "")
    in_params = [date_from] if date_from else []
    with _conn() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM message_log{where}", params).fetchone()[0]
        failures = conn.execute(f"SELECT COUNT(*) FROM message_log{fail_where}", params).fetchone()[0]
        active = conn.execute(
            f"SELECT COUNT(DISTINCT recipient_user_id) FROM message_log{active_where}", params
        ).fetchone()[0]
        by_cat = conn.execute(
            f"SELECT category, COUNT(*) AS c FROM message_log{where} GROUP BY category", params
        ).fetchall()
        inbound = conn.execute(
            f"SELECT COUNT(*) FROM message_log{in_where}", in_params
        ).fetchone()[0]
    return {
        "total": total,
        "failures": failures,
        "active_recipients": active,
        "by_category": {(r["category"] or "other"): r["c"] for r in by_cat},
        "inbound": inbound,
    }
```

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_message_log_store.py tests/test_admin_messages.py -v`
Expected: PASS (대시보드 stats 테스트 포함 — 시드가 전부 outbound라 total 불변)

- [ ] **Step 5: 커밋**

```bash
git add store/user_store.py tests/test_message_log_store.py
git commit -m "feat(msglog): message_stats를 발송(outbound) 한정으로 보정 + inbound 카운트"
```

---

### Task 4: `record_inbound(body)` — 인바운드 포착 로직

**Files:**
- Modify: `tools/slack_logger.py` (헬퍼·`record_inbound` 추가)
- Test: `tests/test_slack_logger.py`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_slack_logger.py` 끝에 추가

```python
class TestRecordInbound:
    def test_dm_message(self):
        with patch.object(slack_logger.user_store, "log_message") as m:
            slack_logger.record_inbound({"event": {
                "type": "message", "user": "U7", "channel": "U7", "text": "브리핑 해줘"}})
        kw = m.call_args.kwargs
        assert kw["direction"] == "inbound" and kw["method"] == "message"
        assert kw["recipient_user_id"] == "U7" and kw["text"] == "브리핑 해줘"
        assert kw["category"] == "briefing"

    def test_app_mention_strips_token(self):
        with patch.object(slack_logger.user_store, "log_message") as m:
            slack_logger.record_inbound({"event": {
                "type": "app_mention", "user": "U7", "channel": "C1",
                "text": "<@U0BOT> 회의록 찾아줘"}})
        kw = m.call_args.kwargs
        assert kw["text"] == "회의록 찾아줘" and kw["method"] == "app_mention"

    def test_slash_command(self):
        with patch.object(slack_logger.user_store, "log_message") as m:
            slack_logger.record_inbound({
                "command": "/브리핑", "text": "다음주", "user_id": "U7", "channel_id": "D1"})
        kw = m.call_args.kwargs
        assert kw["method"] == "command" and kw["text"] == "/브리핑 다음주"
        assert kw["recipient_user_id"] == "U7" and kw["direction"] == "inbound"

    def test_file_share(self):
        with patch.object(slack_logger.user_store, "log_message") as m:
            slack_logger.record_inbound({"event": {
                "type": "message", "subtype": "file_share", "user": "U7", "channel": "U7",
                "files": [{"name": "녹음.m4a", "mimetype": "audio/m4a"}]}})
        assert m.call_args.kwargs["text"] == "[파일 업로드: 녹음.m4a (audio/m4a)]"

    def test_skip_bot_message(self):
        with patch.object(slack_logger.user_store, "log_message") as m:
            slack_logger.record_inbound({"event": {"type": "message", "bot_id": "B1", "text": "x"}})
        m.assert_not_called()

    def test_skip_message_changed(self):
        with patch.object(slack_logger.user_store, "log_message") as m:
            slack_logger.record_inbound({"event": {"type": "message", "subtype": "message_changed"}})
        m.assert_not_called()

    def test_skip_button_action(self):
        with patch.object(slack_logger.user_store, "log_message") as m:
            slack_logger.record_inbound({"actions": [{"action_id": "trello_register"}]})
        m.assert_not_called()

    def test_redacts_secrets(self):
        with patch.object(slack_logger.user_store, "log_message") as m:
            slack_logger.record_inbound({"event": {
                "type": "message", "user": "U7", "channel": "U7",
                "text": "내 토큰 token=SECRETXYZ 임"}})
        assert "SECRETXYZ" not in m.call_args.kwargs["text"]

    def test_best_effort_never_raises(self):
        with patch.object(slack_logger.user_store, "log_message",
                          side_effect=RuntimeError("db down")):
            slack_logger.record_inbound({"event": {
                "type": "message", "user": "U7", "channel": "U7", "text": "x"}})
        # 예외 없이 반환하면 통과 (best-effort)
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_slack_logger.py::TestRecordInbound -v`
Expected: FAIL — `AttributeError: module 'tools.slack_logger' has no attribute 'record_inbound'`

- [ ] **Step 3: 구현** — `tools/slack_logger.py`의 `install_logging` 위(또는 파일 끝)에 추가

```python
def _inbound_text_from_event(event):
    """message/app_mention 이벤트에서 기록할 텍스트 추출."""
    if event.get("subtype") == "file_share":
        files = event.get("files") or []
        if not files:
            return "[파일 업로드]"
        f0 = files[0]
        more = f" 외 {len(files) - 1}건" if len(files) > 1 else ""
        return f"[파일 업로드: {f0.get('name', '')} ({f0.get('mimetype', '')})]{more}"
    text = event.get("text", "") or ""
    # @멘션 토큰(<@U…>) 제거 — handle_mention과 동일 정리
    return " ".join(w for w in text.split() if not w.startswith("<@")).strip()


def record_inbound(body):
    """사용자 인바운드(DM·@멘션·슬래시) 1건을 message_log에 적재 — best-effort.

    버튼 action·봇 메시지·메시지 수정/삭제는 기록하지 않는다.
    인바운드 행은 recipient_user_id에 '발신자'를 담아 per-user 타임라인에 잡히게 한다.
    예외는 삼킨다(이벤트 처리를 절대 막지 않음)."""
    try:
        if not isinstance(body, dict):
            return
        event = body.get("event")
        if isinstance(event, dict):
            etype = event.get("type")
            if etype not in ("message", "app_mention"):
                return
            if event.get("bot_id"):
                return
            if event.get("subtype") not in (None, "file_share"):
                return  # message_changed/deleted 등 skip
            channel = event.get("channel")
            kind, _ = _recipient_kind(channel)
            text = _inbound_text_from_event(event)
            user_store.log_message(
                method=etype, channel=channel,
                recipient_user_id=event.get("user"),
                recipient_kind=kind or "dm", thread_ts=event.get("thread_ts"),
                text=_redact_secrets(text), blocks_json=None,
                category=_infer_category(text, None), ok=True,
                error=None, direction="inbound",
            )
            return
        if body.get("command"):  # 슬래시 커맨드
            channel = body.get("channel_id")
            kind, _ = _recipient_kind(channel)
            text = f"{body.get('command', '')} {body.get('text', '') or ''}".strip()
            user_store.log_message(
                method="command", channel=channel,
                recipient_user_id=body.get("user_id"),
                recipient_kind=kind or "dm", thread_ts=None,
                text=_redact_secrets(text), blocks_json=None,
                category=_infer_category(text, None), ok=True,
                error=None, direction="inbound",
            )
            return
        # actions(버튼) 및 그 외 → skip
    except Exception as e:
        log.warning(f"인바운드 로깅 실패(이벤트 처리에는 영향 없음): {e}")
```

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_slack_logger.py -v`
Expected: PASS (기존 아웃바운드 테스트 포함)

- [ ] **Step 5: 커밋**

```bash
git add tools/slack_logger.py tests/test_slack_logger.py
git commit -m "feat(slack_logger): 인바운드 캡처 record_inbound 추가 (DM·멘션·슬래시)"
```

---

### Task 5: 인바운드 미들웨어 배선 (main.py)

**Files:**
- Modify: `main.py` (`_install_message_logging` 미들웨어 ~103-111 바로 뒤)

> 이 미들웨어는 Bolt 앱 전체를 띄워야 해서 단위 테스트가 어렵다(로직은 Task 4에서 전수 검증됨). 따라서 추가 후 **전체 스위트 회귀 + 수동 스모크**로 검증한다.

- [ ] **Step 1: 구현** — `main.py`의 `_install_message_logging` 함수 정의 끝(`next()` 줄) 다음에 추가

```python
@app.middleware
def _log_inbound(body, next):
    """사용자 인바운드(메시지·멘션·슬래시)를 message_log에 기록 — best-effort."""
    try:
        slack_logger.record_inbound(body)
    except Exception:
        pass
    next()
```

(참고: `slack_logger`는 `main.py:100`에서 이미 import·사용 중 — 추가 import 불필요.)

- [ ] **Step 2: 전체 스위트 회귀 확인**

Run: `pytest tests/ -q`
Expected: PASS (신규 미들웨어가 기존 테스트를 깨지 않음)

- [ ] **Step 3: 구문/임포트 스모크**

Run: `python -c "import ast; ast.parse(open('main.py').read()); print('main.py OK')"`
Expected: `main.py OK`

- [ ] **Step 4: 수동 스모크 (로컬, 선택)**

`bash start.sh`로 로컬 기동 → 봇에 DM `브리핑 해줘` 전송 → `sqlite3 store/users.db "SELECT direction,method,recipient_user_id,text FROM message_log ORDER BY id DESC LIMIT 3"`에 `inbound|message|<내 uid>|브리핑 해줘` 행이 보이는지 확인. (라이브 영향 없음 — additive)

- [ ] **Step 5: 커밋**

```bash
git add main.py
git commit -m "feat(main): 인바운드 로깅 미들웨어 배선 (best-effort)"
```

---

### Task 6: 관리자 user 타임라인 — 시간 오름차순

**Files:**
- Modify: `server/admin.py` (`api_user_messages` ~207-210)
- Test: `tests/test_admin_messages.py`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_admin_messages.py`에 추가

```python
class TestUserTimeline:
    def test_timeline_asc_and_has_direction(self, client):
        # 기존 fixture가 U1에 "아침 브리핑"(outbound) 1건 시드 → 인바운드·아웃바운드 추가
        user_store.log_message(method="message", channel="U1", recipient_user_id="U1",
                               recipient_kind="dm", text="질문1", category="other",
                               ok=True, direction="inbound")
        user_store.log_message(method="post", channel="U1", recipient_user_id="U1",
                               recipient_kind="dm", text="답변1", category="other", ok=True)
        items = client.get("/admin/api/users/U1/messages", headers=_AUTH).json()["items"]
        texts = [it["text"] for it in items]
        assert texts.index("질문1") < texts.index("답변1")   # 시간 오름차순
        assert all("direction" in it for it in items)        # 방향 정보 포함
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_admin_messages.py::TestUserTimeline -v`
Expected: FAIL — `질문1`이 `답변1`보다 뒤(현재 DESC) → `assert ... < ...` 실패

- [ ] **Step 3: 구현** — `api_user_messages` 교체

```python
@router.get("/users/{uid}/messages")
def api_user_messages(uid: str, _: str = Depends(_require_admin)):
    # 대화 타임라인: 시간 오름차순(인바운드+아웃바운드 인터리브)
    items = user_store.list_messages(user_id=uid, order="asc", limit=200)
    return {"user_id": uid, "items": _enrich_messages(items)}
```

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_admin_messages.py -v`
Expected: PASS (기존 `TestUserMessages`는 시드 1건만 보므로 불변)

- [ ] **Step 5: 커밋**

```bash
git add server/admin.py tests/test_admin_messages.py
git commit -m "feat(admin): user 메시지 조회를 대화 타임라인(시간 오름차순)으로"
```

---

### Task 7: 프론트 — 사용자 대화 타임라인 렌더링

**Files:**
- Modify: `frontend/app.js` (`renderUserDetail` ~218-248)
- Modify: `frontend/style.css` (말풍선 스타일 추가)

> 프론트는 자동 테스트 하네스가 없다 → **수동 검증**(관리자 페이지 로드). 코드는 전량 제시한다.

- [ ] **Step 1: 구현 — `frontend/app.js`의 `renderUserDetail` 함수 전체 교체**

```javascript
  async function renderUserDetail(uid) {
    setLoading("사용자 상세 불러오는 중...");
    const [users, msgData] = await Promise.all([
      api("/users"),
      api("/users/" + encodeURIComponent(uid) + "/messages"),
    ]);
    const u = (users || []).find((x) => x.slack_user_id === uid) || { slack_user_id: uid };
    const items = msgData.items || [];
    const uname = escapeHtml(u.name || uid);
    const bubbles = items.map((m) => {
      const inbound = m.direction === "inbound";
      const who = inbound ? `👤 ${uname}` : "🤖 봇";
      const cat = inbound ? "" : `<span class="tag">${escapeHtml(MSG_CATEGORY_LABEL[m.category] || m.category || "기타")}</span>`;
      const fail = (!inbound && !m.ok) ? ' <span class="tag warn">실패</span>' : "";
      const raw = m.text || "";
      const txt = escapeHtml(raw.slice(0, 500)) + (raw.length > 500 ? "…" : "");
      return `
        <div class="chat-row ${inbound ? "chat-in" : "chat-out"}">
          <div class="chat-meta">${who} · ${escapeHtml(fmtDt(m.ts))} ${cat}${fail}</div>
          <div class="chat-bubble">${txt || "(본문 없음)"}</div>
        </div>`;
    }).join("");
    main.innerHTML = `
      <div class="card">
        <h2><a href="#/users" class="small">← 사용자 목록</a>&nbsp;${uname}</h2>
        <p class="muted small">
          <code>${escapeHtml(uid)}</code> · ${escapeHtml(u.email || "")} ·
          Drive ${u.has_drive ? "🟢" : "—"} / Trello ${u.has_trello ? "🟢" : "—"} / Dreamplus ${u.has_dreamplus ? "🟢" : "—"}
        </p>
      </div>
      <div class="card">
        <h2>대화 타임라인 <span class="muted">(${items.length}건)</span></h2>
        ${items.length === 0
          ? '<div class="empty">기록된 대화가 없습니다.</div>'
          : `<div class="chat">${bubbles}</div>`}
      </div>
    `;
  }
```

- [ ] **Step 2: 구현 — `frontend/style.css` 끝에 추가**

```css
/* 사용자 대화 타임라인 */
.chat { display: flex; flex-direction: column; gap: 10px; }
.chat-row { max-width: 80%; }
.chat-in { align-self: flex-start; }
.chat-out { align-self: flex-end; text-align: right; }
.chat-meta { font-size: 12px; color: #888; margin-bottom: 3px; }
.chat-bubble {
  display: inline-block; padding: 8px 12px; border-radius: 10px;
  white-space: pre-wrap; word-break: break-word; text-align: left;
}
.chat-in .chat-bubble { background: #f1f3f5; }
.chat-out .chat-bubble { background: #d8ecff; }
```

- [ ] **Step 3: 수동 검증**

`bash start.sh` → 브라우저 `http://localhost:8000/admin/` (Basic Auth) → `사용자` 탭 → 사용자 클릭 → "대화 타임라인"에 인바운드(왼쪽 `👤 이름`)·아웃바운드(오른쪽 `🤖 봇`)가 시간순 말풍선으로 보이는지 확인. 빈 사용자는 "기록된 대화가 없습니다." 표시.

- [ ] **Step 4: 커밋**

```bash
git add frontend/app.js frontend/style.css
git commit -m "feat(admin-ui): 사용자 상세를 대화 타임라인(말풍선·방향 구분)으로"
```

---

### Task 8: 문서 갱신 + 최종 회귀

**Files:**
- Modify: `CLAUDE.md` ("메시지 관측" 절)

- [ ] **Step 1: 구현 — `CLAUDE.md`의 "### 메시지 관측(발송 로그)" 절 보강**

"**포착:**" 문단 끝에 다음 문장을 추가:

```markdown
사용자 인바운드(DM·@멘션·슬래시 커맨드)는 `tools/slack_logger.install_logging`과 별개로 `main.py`의 `_log_inbound` Bolt 미들웨어가 `slack_logger.record_inbound(body)`로 포착한다(버튼 action·봇 메시지·메시지 수정/삭제는 제외). 인바운드 행은 `direction='inbound'`이며 `recipient_user_id`에 **발신자**를 담아(컬럼 의미를 "이 로그가 관계된 사용자"로 확장) 관리자 "사용자" 탭의 대화 타임라인이 인바운드·아웃바운드를 한 사람 기준으로 시간순 인터리브한다. 인바운드 본문도 `_redact_secrets`로 비밀 파라미터를 마스킹하지만 자유 텍스트의 임의 비밀까지 전부 거르지는 못한다.
```

"**저장:**" 문단의 컬럼 나열에 `direction`을 포함하도록 수정:

```markdown
**저장:** `message_log` 테이블(`store/user_store.py`) — `ts/method/channel/recipient_user_id/recipient_kind/thread_ts/text/blocks_json/category/ok/error/direction`.
```

- [ ] **Step 2: 전체 회귀**

Run: `pytest tests/ -q`
Expected: PASS (전체)

- [ ] **Step 3: 커밋**

```bash
git add CLAUDE.md
git commit -m "docs: 메시지 관측에 인바운드 캡처·direction·대화 타임라인 반영"
```

---

## 완료 기준 (Definition of Done)

- `message_log.direction` 컬럼 존재, 기존 행은 `outbound`.
- DM·@멘션·슬래시가 `direction='inbound'`로 적재되고 버튼/봇/수정 이벤트는 제외.
- 인바운드 본문 비밀 파라미터 마스킹.
- `message_stats`의 발송 지표는 outbound만 카운트.
- 관리자 `사용자` 탭에서 한 사용자의 인바운드+아웃바운드가 시간순 말풍선 타임라인으로 표시.
- `pytest tests/ -q` 전부 통과.
- 게이팅 없이 일반 배포 가능(사용자 체감 동작 변경 0, best-effort).
