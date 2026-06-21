# 온톨로지(lib-mesh) read 통합 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 브리핑의 ④이전맥락에 사내 온톨로지(lib-mesh) 검색 결과를 채운다 — 사용자별 토큰 등록 + MCP 클라이언트 + 게이팅된 브리핑 주입.

**Architecture:** 신규 `tools/ontology.py`가 검증된 Streamable-HTTP MCP 클라이언트(`/mcp/` 직타·`entity_find`→`entity_cluster`)를 제공. 사용자별 토큰은 `users.ontology_token_enc`(Fernet)에 저장하며 `/온톨로지` → 랜딩 폼 붙여넣기로 등록(`/trello` 패턴). 브리핑은 `ONTOLOGY_BETA_USERS` allowlist로 게이팅되어 미등록/실패/만료 시 기존 동작으로 폴백.

**Tech Stack:** Python, httpx(이미 설치됨), SQLite(`store/user_store.py`), FastAPI(`server/oauth.py`), Slack Bolt(`main.py`), pytest(httpx.MockTransport).

**선행 스펙:** `docs/superpowers/specs/2026-06-21-ontology-read-integration-design.md`

**이 계획의 범위:** 데이터 파이프라인 + ④이전맥락에 **구조화 표시**(관계·문서 목록)까지. **LLM 합성(Sonnet 프로즈화)은 다음 증분으로 의도적 보류** — 게이팅 베타로 *데이터/파이프라인을 먼저 검증*하는 게 스펙 롤아웃("④이전맥락부터")의 취지. `company_context()`는 합성에 바로 넣을 수 있는 구조화 dict를 반환한다.

---

### Task 1: `ontology_token_enc` 컬럼 + save/get/clear

**Files:**
- Modify: `store/user_store.py` (users ALTER 튜플 ~85-92, trello 토큰 함수 옆 ~524-551)
- Test: `tests/test_ontology_store.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_store.py`

```python
"""store/user_store.py — ontology_token_enc 토큰 저장 테스트"""
import base64, os
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())
import pytest
import store.user_store as user_store


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(user_store, "_DB_PATH", str(tmp_path / "t.db"))
    user_store.init_db()
    user_store.register("U1", '{"token":"t","refresh_token":"r","token_uri":"u","client_id":"c","client_secret":"s","scopes":[]}')


def test_default_none():
    assert user_store.get_ontology_token("U1") is None


def test_save_get_clear_roundtrip():
    user_store.save_ontology_token("U1", "eyJabc.def.ghi")
    assert user_store.get_ontology_token("U1") == "eyJabc.def.ghi"
    user_store.clear_ontology_token("U1")
    assert user_store.get_ontology_token("U1") is None
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_store.py -v`
Expected: FAIL — `AttributeError: module 'store.user_store' has no attribute 'get_ontology_token'`

- [ ] **Step 3: 구현** — `store/user_store.py`

(a) users 마이그레이션 튜플(`"trello_token_enc TEXT",` 줄 옆)에 추가:

```python
                    "trello_token_enc TEXT",
                    "ontology_token_enc TEXT",
```

(b) `clear_trello_token` 함수 정의 끝 다음에 추가:

```python
def save_ontology_token(slack_user_id: str, token: str) -> None:
    """온톨로지(lib-mesh) 사용자 토큰을 Fernet 암호화하여 저장"""
    enc = _fernet().encrypt(token.encode()).decode()
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET ontology_token_enc = ? WHERE slack_user_id = ?",
            (enc, slack_user_id),
        )


def get_ontology_token(slack_user_id: str) -> str | None:
    """온톨로지 토큰 복호화 반환. 미설정 시 None."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT ontology_token_enc FROM users WHERE slack_user_id = ?",
            (slack_user_id,),
        ).fetchone()
    if not row or not row["ontology_token_enc"]:
        return None
    return _fernet().decrypt(row["ontology_token_enc"].encode()).decode()


def clear_ontology_token(slack_user_id: str) -> None:
    """온톨로지 연결 해제 (토큰 삭제)"""
    with _conn() as conn:
        conn.execute(
            "UPDATE users SET ontology_token_enc = NULL WHERE slack_user_id = ?",
            (slack_user_id,),
        )
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_store.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add store/user_store.py tests/test_ontology_store.py
git commit -m "feat(ontology): users.ontology_token_enc 컬럼 + save/get/clear"
```

---

### Task 2: `tools/ontology.py` — 순수 헬퍼 (토큰 추출·파싱 유틸)

**Files:**
- Create: `tools/ontology.py`
- Test: `tests/test_ontology_client.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_client.py`

```python
"""tools/ontology.py — 순수 헬퍼 + MCP 클라이언트 테스트"""
import os
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import tools.ontology as ont

_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig123"


class TestPureHelpers:
    def test_extract_from_json_config(self):
        cfg = '{"mcpServers":{"lib-mesh":{"url":"https://ont/mcp","headers":{"Authorization":"Bearer %s"}}}}' % _JWT
        assert ont.extract_bearer_token(cfg) == _JWT

    def test_extract_from_raw_text(self):
        assert ont.extract_bearer_token("아무 텍스트 " + _JWT + " 끝") == _JWT

    def test_extract_none(self):
        assert ont.extract_bearer_token("토큰 없음") is None
        assert ont.extract_bearer_token("") is None

    def test_endpoint_adds_trailing_slash(self):
        assert ont._endpoint("https://ont.x/mcp") == "https://ont.x/mcp/"
        assert ont._endpoint("https://ont.x/mcp/") == "https://ont.x/mcp/"

    def test_best_slug_prefers_exact(self):
        find = {"matches": [
            {"slug": "entity/sub", "match_kind": "substring", "confidence": 0.9, "importance": 0.9},
            {"slug": "entity/komsa", "match_kind": "exact", "confidence": 0.95, "importance": 0.9},
        ]}
        assert ont._best_slug(find) == "entity/komsa"

    def test_best_slug_empty(self):
        assert ont._best_slug({"matches": []}) is None
        assert ont._best_slug({}) is None

    def test_normalize_cluster(self):
        cluster = {"seed": "entity/komsa", "entities": [
            {"slug": "entity/komsa", "hop": 0, "title": "KOMSA", "via": None},
            {"slug": "entity/kca", "hop": 1, "title": "KCA", "via": "related-to"},
        ], "documents": [{"id": "doc/1", "title": "KOMSA 마케팅 계획"}]}
        out = ont._normalize_cluster(cluster, "entity/komsa")
        assert out["entity_count"] == 2
        assert {"relation": "related-to", "title": "KCA"} in out["relations"]
        assert out["documents"][0]["title"] == "KOMSA 마케팅 계획"
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_client.py::TestPureHelpers -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'tools.ontology'`

- [ ] **Step 3: 구현** — `tools/ontology.py` 생성 (순수 헬퍼 부분)

```python
"""사내 온톨로지(lib-mesh) MCP read 클라이언트 — Streamable-HTTP JSON-RPC.

/mcp/ 직타(트레일링 슬래시 필수 — /mcp는 307이고 리다이렉트 추종 시 Authorization 드롭).
읽기 전용: entity_find / entity_cluster / document_* → 우리 하네스가 합성.
"""
import json
import logging
import os
import re
from datetime import datetime

import httpx

from store import user_store

log = logging.getLogger(__name__)

DEFAULT_URL = os.getenv("ONTOLOGY_MCP_URL", "https://ont.parametacorp.com/mcp/")
_PROTOCOL = "2025-06-18"
_TIMEOUT = float(os.getenv("ONTOLOGY_TIMEOUT", "40"))
_JWT_RE = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")


class OntologyAuthError(Exception):
    """토큰 만료/무효(HTTP 401)."""


def extract_bearer_token(config_text: str) -> str | None:
    """ont 'MCP 설정' 붙여넣기에서 Bearer JWT 추출. JSON이면 Authorization 헤더,
    아니면 원시 텍스트에서 eyJ... 패턴."""
    if not config_text or not config_text.strip():
        return None
    txt = config_text.strip()
    try:
        data = json.loads(txt)
        found = {}

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if str(k).lower() == "authorization" and isinstance(v, str):
                        found["auth"] = v
                    walk(v)
            elif isinstance(o, list):
                for x in o:
                    walk(x)

        walk(data)
        if found.get("auth"):
            m = _JWT_RE.search(found["auth"])
            if m:
                return m.group(0)
    except Exception:
        pass
    m = _JWT_RE.search(txt)
    return m.group(0) if m else None


def _endpoint(url: str = None) -> str:
    u = url or DEFAULT_URL
    return u if u.endswith("/") else u + "/"


def _recent_range(months: int = 6) -> list[str]:
    """['YYYY-MM','YYYY-MM'] — 현재월 기준 과거 N개월(변동층 time_range용)."""
    now = datetime.now()
    y, m = now.year, now.month
    fm, fy = m - months, y
    while fm <= 0:
        fm += 12
        fy -= 1
    return [f"{fy:04d}-{fm:02d}", f"{y:04d}-{m:02d}"]


def _best_slug(find_result) -> str | None:
    """entity_find data → 최선 slug (exact > confidence > importance)."""
    matches = (find_result or {}).get("matches", []) if isinstance(find_result, dict) else []
    if not matches:
        return None
    matches = sorted(
        matches,
        key=lambda mm: (mm.get("match_kind") == "exact", mm.get("confidence", 0), mm.get("importance", 0)),
        reverse=True,
    )
    return matches[0].get("slug")


def _normalize_cluster(cluster, slug) -> dict:
    """entity_cluster data → {seed, relations[], documents[], entity_count, document_count}."""
    data = cluster if isinstance(cluster, dict) else {}
    ents = data.get("entities", []) or []
    docs = data.get("documents", []) or []
    relations = []
    for e in ents:
        via = e.get("via")
        if via and e.get("slug") != slug:
            relations.append({"relation": via, "title": e.get("title") or e.get("slug")})
    doclist = [
        {"title": d.get("title") or d.get("name") or d.get("id"),
         "id": d.get("id") or d.get("document_id")}
        for d in docs
    ]
    return {
        "seed": slug,
        "relations": relations[:20],
        "documents": doclist[:10],
        "entity_count": len(ents),
        "document_count": len(docs),
    }
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_client.py::TestPureHelpers -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add tools/ontology.py tests/test_ontology_client.py
git commit -m "feat(ontology): tools/ontology 순수 헬퍼 (토큰추출·slug선택·cluster정규화)"
```

---

### Task 3: `tools/ontology.py` — `OntologyClient` (MCP 핸드셰이크 + tools/call)

**Files:**
- Modify: `tools/ontology.py`
- Test: `tests/test_ontology_client.py`

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_client.py`에 추가

```python
import httpx, json
import pytest


def _mock_transport():
    """initialize → 200(serverInfo), tools/call → content[].text의 data 봉투, 그 외 405."""
    def handler(request: httpx.Request):
        body = json.loads(request.content.decode())
        method = body.get("method")
        # 트레일링 슬래시로 와야 함
        assert str(request.url).endswith("/mcp/"), f"슬래시 직타 아님: {request.url}"
        assert request.headers.get("authorization", "").startswith("Bearer ")
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                "result": {"serverInfo": {"name": "lib-mesh"}, "protocolVersion": "2025-06-18", "capabilities": {}}})
        if method == "notifications/initialized":
            return httpx.Response(202, json={})
        if method == "tools/call":
            name = body["params"]["name"]
            data = {"matches": [{"slug": "entity/komsa", "match_kind": "exact", "confidence": 0.95}]} \
                if name == "entity_find" else {"seed": "entity/komsa", "entities": [], "documents": []}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2,
                "result": {"content": [{"type": "text", "text": json.dumps({"data": data})}]}})
        return httpx.Response(405)
    return httpx.MockTransport(handler)


def _client(token="eyJa.b.c"):
    oc = ont.OntologyClient(token, url="https://ont.x/mcp")
    oc._http = httpx.Client(transport=_mock_transport())
    return oc


class TestOntologyClient:
    def test_call_tool_parses_data_envelope(self):
        with _client() as oc:
            res = oc.call_tool("entity_find", {"name": "KOMSA"})
        assert res["matches"][0]["slug"] == "entity/komsa"

    def test_endpoint_has_trailing_slash(self):
        oc = ont.OntologyClient("eyJa.b.c", url="https://ont.x/mcp")
        assert oc.url == "https://ont.x/mcp/"

    def test_401_raises_auth_error(self):
        def h(request): return httpx.Response(401, json={"error": "unauthorized"})
        oc = ont.OntologyClient("eyJa.b.c", url="https://ont.x/mcp")
        oc._http = httpx.Client(transport=httpx.MockTransport(h))
        with pytest.raises(ont.OntologyAuthError):
            oc.call_tool("entity_find", {"name": "X"})

    def test_validate_ok(self):
        with _client() as oc:
            assert oc.validate() is True
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_client.py::TestOntologyClient -v`
Expected: FAIL — `AttributeError: module 'tools.ontology' has no attribute 'OntologyClient'`

- [ ] **Step 3: 구현** — `tools/ontology.py`에 추가 (헬퍼 다음)

```python
class OntologyClient:
    """초기화 1회 후 tools/call 재사용. `with` 블록 권장. /mcp/ 직타(리다이렉트 미추종)."""

    def __init__(self, token: str, url: str = None, timeout: float = _TIMEOUT):
        self.url = _endpoint(url)
        self.token = token
        self._http = httpx.Client(timeout=timeout, follow_redirects=False)
        self._sid = None
        self._inited = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        try:
            self._http.close()
        except Exception:
            pass

    def _headers(self) -> dict:
        tok = self.token if self.token.lower().startswith("bearer ") else f"Bearer {self.token}"
        h = {
            "Authorization": tok,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _PROTOCOL,
        }
        if self._sid:
            h["Mcp-Session-Id"] = self._sid
        return h

    def _post(self, payload: dict):
        r = self._http.post(self.url, json=payload, headers=self._headers())
        sid = r.headers.get("mcp-session-id")
        if sid:
            self._sid = sid
        if r.status_code == 401:
            raise OntologyAuthError("ontology 401 unauthorized")
        return r

    @staticmethod
    def _parse(r):
        ct = r.headers.get("content-type", "") or ""
        if "event-stream" in ct:
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        m = json.loads(line[5:].strip())
                        if "result" in m or "error" in m:
                            return m
                    except Exception:
                        pass
            return None
        try:
            return r.json()
        except Exception:
            return None

    def _ensure_init(self):
        if self._inited:
            return
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": _PROTOCOL, "capabilities": {},
                                   "clientInfo": {"name": "meeting-agent", "version": "1.0"}}})
        if r.status_code != 200:
            raise RuntimeError(f"ontology initialize 실패: HTTP {r.status_code}")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._inited = True

    def call_tool(self, name: str, arguments: dict):
        """tools/call → result.content[].text의 `data` 봉투(JSON 파싱) 반환."""
        self._ensure_init()
        r = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments}})
        msg = self._parse(r)
        if not msg or "result" not in msg:
            raise RuntimeError(f"ontology {name} 실패: "
                               f"{json.dumps(msg, ensure_ascii=False)[:200] if msg else 'no result'}")
        blocks = msg["result"].get("content", []) or []
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        sc = msg["result"].get("structuredContent")
        if sc is not None:
            return sc
        try:
            parsed = json.loads(text)
            return parsed.get("data", parsed) if isinstance(parsed, dict) else parsed
        except Exception:
            return text

    def validate(self) -> bool:
        """등록 시 토큰 유효성: initialize 성공이면 True, 401이면 OntologyAuthError."""
        self._ensure_init()
        return True
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_client.py -v`
Expected: PASS (TestPureHelpers + TestOntologyClient)

- [ ] **Step 5: 커밋**

```bash
git add tools/ontology.py tests/test_ontology_client.py
git commit -m "feat(ontology): OntologyClient — Streamable-HTTP 핸드셰이크 + tools/call"
```

---

### Task 4: `tools/ontology.py` — `company_context()` 묶음

**Files:**
- Modify: `tools/ontology.py`
- Test: `tests/test_ontology_client.py`

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_client.py`에 추가

```python
class TestCompanyContext:
    def test_returns_normalized_cluster(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")
        calls = []

        class FakeClient:
            def __init__(self, token, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def call_tool(self, name, args):
                calls.append((name, args))
                if name == "entity_find":
                    return {"matches": [{"slug": "entity/komsa", "match_kind": "exact", "confidence": 0.95}]}
                return {"seed": "entity/komsa", "entities": [
                    {"slug": "entity/kca", "title": "KCA", "via": "related-to"}], "documents": []}

        monkeypatch.setattr(ont, "OntologyClient", FakeClient)
        out = ont.company_context("U1", "KOMSA", recent=True)
        assert out["seed"] == "entity/komsa"
        assert out["relations"][0]["relation"] == "related-to"
        assert calls[0][0] == "entity_find"
        assert calls[1][0] == "entity_cluster" and "time_range" in calls[1][1]

    def test_no_token_returns_none(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: None)
        assert ont.company_context("U1", "KOMSA") is None

    def test_no_match_returns_empty(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")

        class FakeClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def call_tool(self, name, args): return {"matches": []}

        monkeypatch.setattr(ont, "OntologyClient", FakeClient)
        out = ont.company_context("U1", "없는업체")
        assert out["seed"] is None and out["relations"] == []
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_client.py::TestCompanyContext -v`
Expected: FAIL — `AttributeError: module 'tools.ontology' has no attribute 'company_context'`

- [ ] **Step 3: 구현** — `tools/ontology.py` 끝에 추가

```python
def company_context(user_id: str, company_name: str, recent: bool = False) -> dict | None:
    """업체명 → entity_find → entity_cluster → 정규화 dict. 토큰 없으면 None.
    OntologyAuthError는 그대로 올림(호출부가 만료 처리). seed 없으면 빈 구조."""
    token = user_store.get_ontology_token(user_id)
    if not token:
        return None
    with OntologyClient(token) as oc:
        find = oc.call_tool("entity_find", {"name": company_name, "limit": 5})
        slug = _best_slug(find)
        if not slug:
            return {"seed": None, "relations": [], "documents": [], "entity_count": 0, "document_count": 0}
        args = {"seed": slug, "depth": 2, "include_documents": True,
                "limit_entities": 40, "limit_documents": 15}
        if recent:
            args["time_range"] = _recent_range()
        cluster = oc.call_tool("entity_cluster", args)
        return _normalize_cluster(cluster, slug)
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_client.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add tools/ontology.py tests/test_ontology_client.py
git commit -m "feat(ontology): company_context — entity_find→entity_cluster 묶음"
```

---

### Task 5: 사용자별 토큰 등록 플로우 (oauth.py)

**Files:**
- Modify: `server/oauth.py` (Trello 플로우 다음 ~537 이후)
- Test: `tests/test_ontology_register.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_register.py`

```python
"""server/oauth.py — 온톨로지 등록 엔드포인트 테스트"""
import os
os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
os.environ.setdefault("OAUTH_CALLBACK_URL", "https://test.ngrok.io/oauth/callback")
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

with patch("google_auth_oauthlib.flow.Flow.from_client_secrets_file"), \
     patch("store.user_store.init_db"):
    from server.oauth import app, _pending_ontology_states, build_ontology_register_url

client = TestClient(app)
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig"


def test_build_url_stores_state():
    _pending_ontology_states.clear()
    url = build_ontology_register_url("U1")
    assert "/ontology/register?state=U1-" in url
    assert any(k.startswith("U1-") for k in _pending_ontology_states)


def test_register_form_unknown_state_400():
    assert client.get("/ontology/register?state=nope").status_code == 400


def test_save_extracts_validates_and_stores():
    _pending_ontology_states.clear()
    _pending_ontology_states["U2-x"] = "U2"
    cfg = '{"headers":{"Authorization":"Bearer %s"}}' % _JWT
    fake_oc = MagicMock(); fake_oc.__enter__ = lambda s: s; fake_oc.__exit__ = lambda *a: None
    fake_oc.validate.return_value = True
    with patch("tools.ontology.OntologyClient", return_value=fake_oc), \
         patch("server.oauth.user_store") as store:
        r = client.post("/ontology/save", json={"state": "U2-x", "config": cfg})
    assert r.json()["ok"] is True
    store.save_ontology_token.assert_called_once_with("U2", _JWT)


def test_save_rejects_config_without_token():
    _pending_ontology_states.clear()
    _pending_ontology_states["U3-x"] = "U3"
    r = client.post("/ontology/save", json={"state": "U3-x", "config": "토큰 없음"})
    assert r.json()["ok"] is False
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_register.py -v`
Expected: FAIL — `ImportError: cannot import name '_pending_ontology_states'`

- [ ] **Step 3: 구현** — `server/oauth.py`, `/trello/save` 함수 정의 끝(~537) 다음에 추가

```python
# ── 온톨로지(lib-mesh) 토큰 등록 (PAT 붙여넣기) ──────────────────
_pending_ontology_states: dict[str, str] = {}


def build_ontology_register_url(slack_user_id: str) -> str:
    """온톨로지 등록 랜딩 URL. Slack에서 클릭 → /ontology/register 폼."""
    state = f"{slack_user_id}-{uuid.uuid4().hex[:12]}"
    _pending_ontology_states[state] = slack_user_id
    base_url = os.getenv("OAUTH_CALLBACK_URL", "").rsplit("/oauth/callback", 1)[0]
    return f"{base_url}/ontology/register?state={state}"


@app.get("/ontology/register")
async def ontology_register_form(request: Request):
    state = request.query_params.get("state", "")
    if not state or state not in _pending_ontology_states:
        return HTMLResponse(
            "<h2>❌ 세션이 만료되었습니다.</h2>"
            "<p>Slack에서 <b>/온톨로지</b> 를 다시 입력해주세요.</p>", status_code=400)
    return HTMLResponse(f"""
    <html><body style="font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 16px">
      <h2>🔗 온톨로지(lib-mesh) 연결</h2>
      <p>ont에서 <b>MCP 설정 복사</b> 후 아래 칸에 붙여넣고 저장하세요. (토큰은 서버로만 전송됩니다.)</p>
      <textarea id="cfg" style="width:100%;height:200px;font-family:monospace"></textarea><br><br>
      <button onclick="save()" style="padding:8px 16px">저장</button>
      <p id="status"></p>
      <script>
        function save() {{
          fetch('/ontology/save', {{
            method:'POST', headers:{{'Content-Type':'application/json'}},
            body: JSON.stringify({{state:'{state}', config: document.getElementById('cfg').value}})
          }}).then(function(r){{return r.json();}}).then(function(d){{
            document.getElementById('status').innerHTML = d.ok
              ? '<span style="color:green">✅ 연결 완료! Slack으로 돌아가세요.</span>'
              : '❌ ' + (d.error || '실패');
          }}).catch(function(e){{ document.getElementById('status').textContent = '❌ ' + e; }});
        }}
      </script>
    </body></html>""")


class _OntologySaveRequest(BaseModel):
    state: str
    config: str


@app.post("/ontology/save")
async def ontology_save(req: _OntologySaveRequest):
    """붙여넣은 MCP 설정에서 토큰 추출 → 유효성 검증 → 암호화 저장."""
    slack_user_id = _pending_ontology_states.pop(req.state, None)
    if not slack_user_id:
        return {"ok": False, "error": "세션 만료"}
    from tools import ontology
    token = ontology.extract_bearer_token(req.config)
    if not token:
        return {"ok": False, "error": "설정에서 토큰을 찾지 못했습니다 (MCP 설정 전체를 붙여넣어 주세요)"}
    try:
        with ontology.OntologyClient(token) as oc:
            oc.validate()
    except ontology.OntologyAuthError:
        return {"ok": False, "error": "토큰이 유효하지 않습니다 (401). ont에서 새로 복사해 주세요."}
    except Exception as e:
        return {"ok": False, "error": f"검증 실패: {e}"}
    try:
        user_store.save_ontology_token(slack_user_id, token)
        log.info(f"온톨로지 토큰 저장 완료: {slack_user_id}")
        if _slack_client:
            _slack_client.chat_postMessage(
                channel=slack_user_id,
                text="✅ 온톨로지 연결 완료! 브리핑에서 사내 지식 맥락(관계·문서)을 볼 수 있습니다.")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
```

(참고: `uuid`, `os`, `HTMLResponse`, `BaseModel`, `Request`, `user_store`, `_slack_client`는 Trello 플로우에서 이미 import/정의됨 — 추가 import 불필요.)

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_register.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add server/oauth.py tests/test_ontology_register.py
git commit -m "feat(ontology): 사용자별 토큰 등록 (랜딩 폼 붙여넣기 → 추출·검증·저장)"
```

---

### Task 6: `/온톨로지` 커맨드 + 등록 링크 DM (main.py)

**Files:**
- Modify: `main.py` (Trello 커맨드 등록부 ~3044 근처)

> main.py는 앱 전체 기동이 필요해 단위 테스트가 어렵다 → 전체 스위트 회귀 + 구문검사로 검증.

- [ ] **Step 1: 구현** — `main.py`의 `app.command("/trello")(_trello_setup_handler)` 줄 다음에 추가

```python
# ── 온톨로지(lib-mesh) 설정 ─────────────────────────────────────

def _send_ontology_setup_link(client, user_id: str) -> None:
    """온톨로지 등록 랜딩 링크를 DM으로 발송."""
    try:
        url = oauth_server.build_ontology_register_url(user_id)
    except Exception as e:
        client.chat_postMessage(channel=user_id, text=f"⚠️ 등록 링크 생성 실패: {e}")
        return
    client.chat_postMessage(
        channel=user_id,
        text=("🔗 *온톨로지(lib-mesh) 연결*\n"
              f"1) ont에서 *MCP 설정 복사*\n"
              f"2) <{url}|여기를 눌러> 붙여넣고 저장\n"
              "_토큰은 Slack을 거치지 않고 서버로만 전송됩니다._"),
    )


def _ontology_setup_handler(ack, body, client):
    ack()
    user_id = body["user_id"]
    if not _check_registered(client, user_id):
        return
    _send_ontology_setup_link(client, user_id)

app.command("/온톨로지")(_ontology_setup_handler)
app.command("/ontology")(_ontology_setup_handler)
```

(참고: `oauth_server`, `_check_registered`는 main.py에서 이미 사용 중 — Trello 핸들러가 동일 패턴.)

- [ ] **Step 2: 구문 검사**

Run: `.venv/bin/python -c "import ast; ast.parse(open('main.py').read()); print('main.py OK')"`
Expected: `main.py OK`

- [ ] **Step 3: 전체 회귀**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 4: 커밋**

```bash
git add main.py
git commit -m "feat(ontology): /온톨로지 커맨드 + 등록 링크 DM"
```

---

### Task 7: 브리핑 통합 — 게이팅 + ④이전맥락 주입 + 렌더링

**Files:**
- Modify: `agents/before.py` (`_ontology_enabled` 신규 + `_run_briefing_research` ④블록 ~1489-1506)
- Modify: `tools/slack_tools.py` (`build_context_block` ~311-355)
- Test: `tests/test_ontology_briefing.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_briefing.py`

```python
"""온톨로지 게이팅 + 컨텍스트 렌더링 테스트"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import patch
import agents.before as before
from tools.slack_tools import build_context_block


class TestGating:
    def test_disabled_when_not_in_beta(self, monkeypatch):
        monkeypatch.setenv("ONTOLOGY_BETA_USERS", "U_other")
        monkeypatch.setattr(before.user_store, "get_ontology_token", lambda uid: "tok")
        assert before._ontology_enabled("U1") is False

    def test_disabled_when_no_token(self, monkeypatch):
        monkeypatch.setenv("ONTOLOGY_BETA_USERS", "U1")
        monkeypatch.setattr(before.user_store, "get_ontology_token", lambda uid: None)
        assert before._ontology_enabled("U1") is False

    def test_enabled(self, monkeypatch):
        monkeypatch.setenv("ONTOLOGY_BETA_USERS", "U1,U2")
        monkeypatch.setattr(before.user_store, "get_ontology_token", lambda uid: "tok")
        assert before._ontology_enabled("U1") is True


class TestContextRender:
    def test_ontology_section_rendered(self):
        ctx = {"trello": [], "emails": [], "minutes": [], "ontology": {
            "seed": "entity/komsa",
            "relations": [{"relation": "related-to", "title": "KCA"}],
            "documents": [{"title": "KOMSA 마케팅 계획", "id": "doc/1"}]}}
        blocks = build_context_block(ctx)
        text = blocks[0]["text"]["text"]
        assert "온톨로지" in text and "KCA" in text and "KOMSA 마케팅 계획" in text

    def test_no_ontology_section_when_absent(self):
        ctx = {"trello": [], "emails": [], "minutes": []}
        text = build_context_block(ctx)[0]["text"]["text"]
        assert "온톨로지" not in text
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_briefing.py -v`
Expected: FAIL — `AttributeError: module 'agents.before' has no attribute '_ontology_enabled'`

- [ ] **Step 3a: 구현 — `agents/before.py`에 게이팅 헬퍼 추가** (파일 상단 import 근처, `research_company` 위 등 모듈 수준)

```python
def _ontology_enabled(user_id: str) -> bool:
    """ONTOLOGY_BETA_USERS allowlist에 있고 토큰이 등록된 사용자만 온톨로지 경로."""
    import os
    beta = {u.strip() for u in os.getenv("ONTOLOGY_BETA_USERS", "").split(",") if u.strip()}
    if user_id not in beta:
        return False
    try:
        return user_store.get_ontology_token(user_id) is not None
    except Exception:
        return False
```

- [ ] **Step 3b: 구현 — `agents/before.py` `_run_briefing_research`의 ④블록** (`context = get_previous_context(...)` 와 `context_blocks = build_context_block(context)` 사이, 기존 `if not context.get("emails") and drive_emails:` 보정 다음)

```python
        # 온톨로지(사내 지식) 주입 — 게이팅. 실패/만료는 섹션 생략(브리핑 안 깨짐)
        if _ontology_enabled(user_id):
            try:
                from tools import ontology
                onto = ontology.company_context(user_id, company_name, recent=True)
                if onto and (onto.get("relations") or onto.get("documents")):
                    context["ontology"] = onto
            except Exception as oe:
                from tools import ontology as _ot
                if isinstance(oe, _ot.OntologyAuthError):
                    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                          text="🔑 온톨로지 토큰이 만료된 것 같아요. `/온톨로지` 로 재등록해 주세요.")
                else:
                    log.warning(f"온톨로지 조회 실패({company_name}): {oe}")
```

- [ ] **Step 3c: 구현 — `tools/slack_tools.py` `build_context_block`** (이메일 섹션 `return` 직전에 온톨로지 섹션 추가)

`build_context_block`의 `return [{"type": "section", ...}]` 바로 위에 삽입:

```python
    onto = context.get("ontology")
    if onto and (onto.get("relations") or onto.get("documents")):
        lines.append("")
        lines.append("🔗  *온톨로지(사내 지식)*")
        for r in (onto.get("relations") or [])[:6]:
            lines.append(f"   • {r.get('relation')}: {r.get('title')}")
        for d in (onto.get("documents") or [])[:5]:
            lines.append(f"   • 문서: {d.get('title')}")
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_briefing.py tests/ -q`
Expected: PASS (신규 + 전체 회귀)

- [ ] **Step 5: 커밋**

```bash
git add agents/before.py tools/slack_tools.py tests/test_ontology_briefing.py
git commit -m "feat(ontology): 브리핑 ④이전맥락에 게이팅된 온톨로지 주입 + 렌더링"
```

---

### Task 8: 문서 + 최종 회귀

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: 구현 — `CLAUDE.md`에 "### 온톨로지(lib-mesh) 연동" 절 추가** (Trello 연동 절 다음)

```markdown
### 온톨로지(lib-mesh) 연동

사내 온톨로지(RAG, read-only)를 브리핑 ④이전맥락에 주입합니다. **사용자별 토큰**(각자 ont에서 "MCP 설정 복사" → `/온톨로지` 랜딩 폼 붙여넣기 → `users.ontology_token_enc` Fernet 저장).

**아키텍처:**
- `tools/ontology.py` — Streamable-HTTP MCP 클라이언트. **`https://ont.parametacorp.com/mcp/`(트레일링 슬래시 필수 — `/mcp`는 307, 리다이렉트 추종 시 Authorization 드롭)** 직타. `entity_find`→`entity_cluster`로 `company_context(user_id, company, recent)` 반환. `OntologyAuthError`(401).
- `server/oauth.py` — `/ontology/register`(폼)·`/ontology/save`(토큰 추출·검증·암호화 저장).
- `agents/before.py` — `_ontology_enabled(user_id)` 게이팅 후 `_run_briefing_research` ④블록에서 주입. 만료(401)→재등록 DM, 그 외 실패→섹션 생략(브리핑 안 깨짐).

**게이팅:** `ONTOLOGY_BETA_USERS`(쉼표 user_id, 기본 빈값=비활성). allowlist+토큰 보유자만 온톨로지 경로, 그 외 기존 동작.

**환경변수:** `ONTOLOGY_BETA_USERS`, `ONTOLOGY_MCP_URL`(기본 `https://ont.parametacorp.com/mcp/`), `ONTOLOGY_TIMEOUT`(기본 40).

**규칙:** read-only(쓰기 없음). 외부 뉴스는 온톨로지 밖(web_search 유지). 되먹임은 회의록만(Drive 크롤 자동).
```

- [ ] **Step 2: 최종 전체 회귀**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (전체)

- [ ] **Step 3: 커밋**

```bash
git add CLAUDE.md
git commit -m "docs: 온톨로지(lib-mesh) 연동 절 추가"
```

---

## 완료 기준 (Definition of Done)

- `users.ontology_token_enc` + save/get/clear.
- `/온톨로지` → 랜딩 폼 붙여넣기 → 토큰 추출·검증·암호화 저장.
- `tools/ontology.company_context()`가 `entity_find`→`entity_cluster`로 정규화 dict 반환(`/mcp/` 직타).
- `ONTOLOGY_BETA_USERS` 게이팅 — 미등록/비beta/만료/실패 시 기존 브리핑 동작 유지(안 깨짐).
- beta+등록 사용자 브리핑 ④이전맥락에 온톨로지 관계·문서 표시.
- `.venv/bin/python -m pytest tests/ -q` 전체 통과.

## 다음 증분 (이 계획 밖, 보류)

- 온톨로지 cluster 결과를 **Sonnet으로 프로즈 합성**(현재는 구조화 표시). `company_context`가 합성용 구조화 dict를 이미 반환하므로 추가 용이.
- ②파라메타 서비스 연결점 · ③인물 내부 접점에 온톨로지 주입(같은 모듈 재사용).
- `document_search`/`document_fetch`로 본문 인용.
- ⚠️ 채팅 노출 토큰 회전(ont regenerate 확인).
