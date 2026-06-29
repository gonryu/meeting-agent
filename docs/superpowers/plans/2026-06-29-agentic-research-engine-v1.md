# 에이전트형 리서치 엔진 v1 (온디맨드) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development(권장) 또는 executing-plans. 단계는 체크박스(`- [ ]`).

**Goal:** `{업체} 리서치` 온디맨드 경로를, 고정 파이프라인 대신 **Claude tool-use 에이전트**(Gmail 스레드·Drive검색[hwpx 포함]·Slack 채널·Trello·웹·온톨로지 다중홉)로 처리해 "풍부+정확"한 `CompanyResearch`를 생성. 킬스위치 뒤, 실패 시 기존 파이프라인 폴백.

**Architecture:** 봇 껍데기 불변. `run_company_research` 내부만 `AGENTIC_RESEARCH=true`일 때 `research_agent.run_agentic_research`로 위임. 에이전트는 read-only 도구로 다중홉 탐색 → `submit_research` 도구로 구조화 출력 → critic 3종(URL그라운딩 Haiku / 동명타사 capable / 커버리지) → 렌더는 기존 구조화 경로(스트랭글러) 재사용.

**Tech Stack:** Python, `anthropic` tool-use(`claude-sonnet-4-5`, critic은 `claude-haiku-4-5`), 기존 `tools/{gmail,drive,trello,ontology}.py`·`tools/slack` 클라이언트, pytest(+`unittest.mock`). 설계: `docs/superpowers/specs/2026-06-29-agentic-research-engine-design.md`.

**범위:** 온디맨드 v1만. 스케줄 브리핑 전환·hwp(레거시 바이너리)·Slack 전역검색은 비범위. v1 검증 게이트(스펙 §11): hwpx 추출·Slack 채널·sharedWithMe·동명타사.

**테스트 원칙:** 기존 패턴 따름 — `os.environ.setdefault`로 키 설정 후 `anthropic.Anthropic`·Google/Slack 클라이언트를 `unittest.mock.patch`로 차단하고 import. LLM 응답은 스크립트된 mock으로 주입(실제 호출 없음).

---

## 파일 구조

| 파일 | 역할 | 변경 |
|---|---|---|
| `agents/research_types.py` | `CompanyResearch` 필드 확장(summary_line·deal_context·source_docs·attendees·talking_points) | Modify |
| `tools/gmail.py` | `read_thread(creds, thread_id)` 추가 | Modify |
| `tools/drive.py` | `search_files()` + hwpx/문서 텍스트 추출 추가 | Modify |
| `tools/slack_read.py` | `channel_history(client, channel_id)` (allowlist) | Create |
| `agents/research_agent.py` | 도구 스펙·dispatch·에이전트 루프·critic 3종·`run_agentic_research` | Create |
| `agents/research_orchestrator.py` | `run_company_research`가 플래그 시 에이전트 위임 + 폴백 | Modify |
| `tools/slack_tools.py` | `build_company_research_block`에 확장 필드 렌더 | Modify |
| `tests/test_research_agent.py` 외 | 각 태스크 테스트 | Create |

---

### Task 1: CompanyResearch 확장 필드

**Files:** Modify `agents/research_types.py` · Test `tests/test_research_types_v2.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_research_types_v2.py`
```python
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from agents.research_types import CompanyResearch, SourceDoc, Attendee


def test_extended_fields_default_empty():
    r = CompanyResearch(company_name="X")
    assert r.summary_line == "" and r.deal_context == ""
    assert r.source_docs == [] and r.attendees == [] and r.talking_points == []


def test_holds_rich_payload():
    r = CompanyResearch(
        company_name="KOMSA", summary_line="홍보 용역 범위 협의",
        deal_context="6/11 RFQ→6/15 견적→6/26 확정",
        source_docs=[SourceDoc(title="견적서.pdf", url="https://drive/x", why="견적 항목")],
        attendees=[Attendee(name="이성룡", role="국장", contact="a@d-antwort.com")],
        talking_points=["굿즈가 견적 45%"],
    )
    assert r.source_docs[0].title == "견적서.pdf"
    assert r.attendees[0].contact == "a@d-antwort.com"
    assert "굿즈" in r.talking_points[0]
```

- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_research_types_v2.py -v` → `ImportError: cannot import name 'SourceDoc'`

- [ ] **Step 3: 구현** — `agents/research_types.py`의 dataclass 영역에 추가
```python
@dataclass
class SourceDoc:
    title: str
    url: str = ""
    why: str = ""          # 왜 관련된지 한 줄


@dataclass
class Attendee:
    name: str
    role: str = ""
    contact: str = ""
    note: str = ""
```
그리고 `CompanyResearch`에 필드 추가(기존 필드 아래):
```python
    summary_line: str = ""                    # 이 미팅/건 한 줄 요약
    deal_context: str = ""                    # 거래·관계 진행 흐름(prose)
    source_docs: list[SourceDoc] = field(default_factory=list)
    attendees: list[Attendee] = field(default_factory=list)
    talking_points: list[str] = field(default_factory=list)
```

- [ ] **Step 4: 통과 확인** — `.venv/bin/python -m pytest tests/test_research_types_v2.py tests/test_research_types.py -q` → PASS(기존 회귀 포함)

- [ ] **Step 5: 커밋**
```bash
git add agents/research_types.py tests/test_research_types_v2.py
git commit -m "feat(research): CompanyResearch 확장 — summary/deal/source_docs/attendees/talking_points (에이전트 v1)"
```

---

### Task 2: Gmail 스레드 본문 읽기

**Files:** Modify `tools/gmail.py` · Test `tests/test_gmail_thread.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_gmail_thread.py`
```python
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock, patch
import tools.gmail as gmail


def _thread_payload():
    return {"messages": [
        {"payload": {"headers": [{"name": "From", "value": "이성룡 <a@d-antwort.com>"},
                                  {"name": "Date", "value": "Sun, 15 Jun 2026 10:00:00 +0900"},
                                  {"name": "Subject", "value": "KOMSA 견적서"}],
                     "body": {"data": ""},
                     "parts": [{"mimeType": "text/plain",
                                "body": {"data": "VG90YWwgNTUsMDQwLDAwMA=="}}]}},  # "Total 55,040,000"
    ]}


def test_read_thread_returns_messages_with_body():
    with patch.object(gmail, "_service") as msvc:
        msvc.return_value.users.return_value.threads.return_value.get.return_value.execute.return_value = _thread_payload()
        out = gmail.read_thread(MagicMock(), "thread123")
    assert out and out[0]["from"].startswith("이성룡")
    assert "55,040,000" in out[0]["body"]
    assert out[0]["subject"] == "KOMSA 견적서"
```

- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_gmail_thread.py -v` → `AttributeError: module 'tools.gmail' has no attribute 'read_thread'`

- [ ] **Step 3: 구현** — `tools/gmail.py`에 추가(기존 `_service`·`_decode_body` 재사용)
```python
def read_thread(creds: Credentials, thread_id: str, max_messages: int = 20) -> list[dict]:
    """스레드의 메시지들을 헤더+본문으로 반환. 거래 흐름·수치 재구성용.
    Returns: [{date, from, subject, body}] (오래된→최신)."""
    svc = _service(creds)
    data = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
    out: list[dict] = []
    for msg in (data.get("messages") or [])[:max_messages]:
        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        out.append({
            "date": headers.get("date", ""),
            "from": headers.get("from", ""),
            "subject": headers.get("subject", ""),
            "body": _decode_body(payload).strip(),
        })
    return out
```
> `_decode_body`가 `parts`/`body.data`를 base64 디코드하는 기존 헬퍼임을 확인하고 시그니처에 맞춰 호출. 다르면 본문 추출만 해당 헬퍼로 위임.

- [ ] **Step 4: 통과 확인** — `.venv/bin/python -m pytest tests/test_gmail_thread.py -q` → PASS

- [ ] **Step 5: 커밋**
```bash
git add tools/gmail.py tests/test_gmail_thread.py
git commit -m "feat(gmail): read_thread — 스레드 본문 읽기(거래 흐름 재구성, 에이전트 v1)"
```

---

### Task 3: Drive 검색 + 첨부 추출(hwpx 포함)

**Files:** Modify `tools/drive.py` · Test `tests/test_drive_search.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_drive_search.py`
```python
import io, os, zipfile
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock, patch
import tools.drive as drive


def test_search_files_scopes_folder_owner_shared():
    captured = {}
    def _list(q=None, fields=None, pageSize=None, **kw):
        captured["q"] = q
        m = MagicMock(); m.execute.return_value = {"files": [
            {"id": "f1", "name": "KOMSA 견적서.pdf", "mimeType": "application/pdf"}]}
        return m
    with patch.object(drive, "_service") as msvc:
        msvc.return_value.files.return_value.list.side_effect = _list
        out = drive.search_files(MagicMock(), "KOMSA 견적", folder_id="FOLDER1")
    assert out and out[0]["name"] == "KOMSA 견적서.pdf"
    # 폴더·소유·sharedWithMe 범위가 쿼리에 반영
    assert "FOLDER1" in captured["q"]
    assert "sharedWithMe" in captured["q"] or "'me' in owners" in captured["q"]


def test_extract_hwpx_text():
    # .hwpx = zip(+section xml). 최소 픽스처로 텍스트 추출 검증
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Contents/section0.xml",
                   "<hml><p><run><t>총 55,040,000원</t></run></p></hml>")
    text = drive._extract_hwpx(buf.getvalue())
    assert "55,040,000" in text
```

- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_drive_search.py -v` → `AttributeError: ... 'search_files'`

- [ ] **Step 3: 구현** — `tools/drive.py`에 추가
```python
import io, re, zipfile

def search_files(creds: Credentials, query: str, folder_id: str = None,
                 include_shared: bool = True, page_size: int = 15) -> list[dict]:
    """영업/제안 공유폴더 + 본인 소유 + sharedWithMe 범위에서 파일 검색.
    Returns: [{id, name, mimeType, modifiedTime}] (관련도/최신순은 호출부/모델이 판단)."""
    svc = _service(creds)
    terms = re.sub(r"['\\\\]", " ", query or "").strip()
    name_q = f"name contains '{terms}'" if terms else ""
    scope_clauses = []
    if folder_id:
        scope_clauses.append(f"'{folder_id}' in parents")
    scope_clauses.append("'me' in owners")
    if include_shared:
        scope_clauses.append("sharedWithMe")
    scope = " or ".join(f"({c})" for c in scope_clauses)
    q = f"({name_q}) and ({scope}) and trashed=false" if name_q else f"({scope}) and trashed=false"
    result = svc.files().list(
        q=q, pageSize=page_size, orderBy="modifiedTime desc",
        fields="files(id,name,mimeType,modifiedTime)",
        includeItemsFromAllDrives=True, supportsAllDrives=True,
    ).execute()
    return result.get("files", [])


_HWPX_TAG_RE = re.compile(r"<[^>]+>")

def _extract_hwpx(raw: bytes) -> str:
    """.hwpx(zip+XML) → 텍스트. section*.xml의 태그 제거."""
    out = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for name in z.namelist():
            if name.lower().endswith(".xml") and ("section" in name.lower() or "content" in name.lower()):
                xml = z.read(name).decode("utf-8", "ignore")
                out.append(_HWPX_TAG_RE.sub(" ", xml))
    return re.sub(r"\s+", " ", " ".join(out)).strip()


def read_file_text(creds: Credentials, file_id: str, mime_type: str = "",
                   name: str = "", max_chars: int = 12000) -> str:
    """파일 본문을 텍스트로 추출. Google문서=export, pdf/hwpx/docx/xlsx=get_media+추출.
    추출 불가 포맷은 빈 문자열(graceful)."""
    svc = _service(creds)
    lname = (name or "").lower()
    try:
        if mime_type.startswith("application/vnd.google-apps"):
            text = svc.files().export(fileId=file_id, mimeType="text/plain").execute()
            text = text.decode("utf-8", "ignore") if isinstance(text, bytes) else str(text)
        else:
            raw = svc.files().get_media(fileId=file_id).execute()
            if lname.endswith(".hwpx"):
                text = _extract_hwpx(raw)
            elif lname.endswith(".pdf"):
                text = _extract_pdf(raw)         # 기존 문서 추출 헬퍼 재사용(during.py 경로)
            elif lname.endswith((".docx", ".xlsx", ".pptx")):
                text = _extract_office(raw, lname)
            else:
                text = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else str(raw)
    except Exception as e:
        log.warning(f"파일 추출 실패({file_id}, {lname}): {e}")
        return ""
    return (text or "")[:max_chars]
```
> `_extract_pdf`/`_extract_office`: 세션 문서 업로드(F4) 경로에 이미 텍스트 추출이 있으면 그 헬퍼를 재사용(중복 구현 금지). 없으면 `pdfminer`/`python-docx`/`openpyxl`로 최소 구현. `.hwp`(레거시 바이너리)는 v1 비범위 — 빈 문자열 + 로그.

- [ ] **Step 4: 통과 확인** — `.venv/bin/python -m pytest tests/test_drive_search.py -q` → PASS

- [ ] **Step 5: 커밋**
```bash
git add tools/drive.py tests/test_drive_search.py
git commit -m "feat(drive): search_files(공유폴더+본인+sharedWithMe) + hwpx/문서 텍스트 추출 (에이전트 v1)"
```

---

### Task 4: Slack 채널 history(allowlist)

**Files:** Create `tools/slack_read.py` · Test `tests/test_slack_read.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_slack_read.py`
```python
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock
from tools.slack_read import channel_history, allowed_channels


def test_allowlist_blocks_unlisted(monkeypatch):
    monkeypatch.setenv("SLACK_BIZ_CHANNELS", "C_BIZ1,C_BIZ2")
    assert "C_BIZ1" in allowed_channels()
    client = MagicMock()
    # 미허용 채널 → 호출 없이 빈 결과
    assert channel_history(client, "C_OTHER") == []
    client.conversations_history.assert_not_called()


def test_returns_recent_messages(monkeypatch):
    monkeypatch.setenv("SLACK_BIZ_CHANNELS", "C_BIZ1")
    client = MagicMock()
    client.conversations_history.return_value = {"messages": [
        {"text": "NH PoC 농협 일정 9월로 연기", "ts": "1.0", "user": "U1"},
        {"text": "펌뱅킹 7/14 확정", "ts": "2.0", "user": "U2"}]}
    out = channel_history(client, "C_BIZ1", limit=10)
    assert any("9월로 연기" in m["text"] for m in out)
```

- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_slack_read.py -v` → `ModuleNotFoundError: tools.slack_read`

- [ ] **Step 3: 구현** — `tools/slack_read.py`
```python
"""Slack 채널 history 읽기 — allowlist된 biz 채널 한정(전역 search 아님)."""
import logging
import os

log = logging.getLogger(__name__)


def allowed_channels() -> set[str]:
    return {c.strip() for c in os.getenv("SLACK_BIZ_CHANNELS", "").split(",") if c.strip()}


def channel_history(client, channel_id: str, limit: int = 30) -> list[dict]:
    """allowlist 채널의 최근 메시지. 미허용/실패 시 빈 리스트(graceful)."""
    if channel_id not in allowed_channels():
        log.info(f"slack channel_history 차단(allowlist 외): {channel_id}")
        return []
    try:
        resp = client.conversations_history(channel=channel_id, limit=limit)
        return [{"text": m.get("text", ""), "ts": m.get("ts", ""), "user": m.get("user", "")}
                for m in (resp.get("messages") or []) if m.get("text")]
    except Exception as e:
        log.warning(f"slack channel_history 실패({channel_id}): {e}")
        return []
```

- [ ] **Step 4: 통과 확인** — `.venv/bin/python -m pytest tests/test_slack_read.py -q` → PASS

- [ ] **Step 5: 커밋**
```bash
git add tools/slack_read.py tests/test_slack_read.py
git commit -m "feat(slack): channel_history allowlist 읽기 — biz 채널 자유서술 맥락 (에이전트 v1)"
```

---

### Task 5: 에이전트 도구 스펙 + dispatch

**Files:** Create `agents/research_agent.py` · Test `tests/test_research_agent_dispatch.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_research_agent_dispatch.py`
```python
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock, patch
import agents.research_agent as ra


def test_tool_specs_include_all_sources():
    names = {t["name"] for t in ra._tool_specs()}
    assert {"gmail_search", "gmail_read_thread", "drive_search", "drive_read",
            "slack_channel_history", "trello_lookup", "web_search",
            "ontology_lookup", "submit_research"} <= names


def test_dispatch_routes_to_drive_search():
    ctx = ra.ToolContext(user_id="U1", creds=MagicMock(), slack_client=MagicMock(),
                         folder_id="F1")
    with patch("agents.research_agent.drive.search_files", return_value=[{"name": "x.pdf"}]) as ms:
        out = ra._dispatch("drive_search", {"query": "KOMSA"}, ctx)
    ms.assert_called_once()
    assert "x.pdf" in out


def test_dispatch_unknown_tool_returns_error_string():
    ctx = ra.ToolContext(user_id="U1", creds=MagicMock(), slack_client=None, folder_id="")
    out = ra._dispatch("nope", {}, ctx)
    assert "unknown" in out.lower() or "알 수 없" in out
```

- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_research_agent_dispatch.py -v` → `ModuleNotFoundError`

- [ ] **Step 3: 구현** — `agents/research_agent.py`(도구 스펙 + dispatch 부분)
```python
"""에이전트형 리서치 엔진 — Claude tool-use 다중홉 + critic 3종 (온디맨드 v1).
설계: docs/superpowers/specs/2026-06-29-agentic-research-engine-design.md"""
import json
import logging
import os
from dataclasses import dataclass

import anthropic

from tools import drive, gmail, trello, ontology, slack_read
from agents.research_types import CompanyResearch, NewsItem, SourceDoc, Attendee

log = logging.getLogger(__name__)
_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_MODEL = "claude-sonnet-4-5"


def agentic_enabled() -> bool:
    return os.getenv("AGENTIC_RESEARCH", "false").lower() == "true"


@dataclass
class ToolContext:
    user_id: str
    creds: object
    slack_client: object = None
    folder_id: str = ""


def _tool_specs() -> list[dict]:
    s = lambda **p: {"type": "object", "properties": p}
    return [
        {"name": "gmail_search", "description": "회사·인물명으로 메일 검색(헤더·snippet)",
         "input_schema": s(query={"type": "string"})},
        {"name": "gmail_read_thread", "description": "스레드 본문 읽기(거래 흐름·수치)",
         "input_schema": s(thread_id={"type": "string"})},
        {"name": "drive_search", "description": "영업/제안 공유폴더+본인+공유받은 문서 검색",
         "input_schema": s(query={"type": "string"})},
        {"name": "drive_read", "description": "파일 본문 추출(PDF·hwpx·docx·xlsx)",
         "input_schema": s(file_id={"type": "string"}, mime_type={"type": "string"}, name={"type": "string"})},
        {"name": "slack_channel_history", "description": "biz 채널 최근 논의(자유서술)",
         "input_schema": s(channel={"type": "string"})},
        {"name": "trello_lookup", "description": "업체 파이프라인 카드(체크리스트·코멘트)",
         "input_schema": s(company={"type": "string"})},
        {"name": "web_search", "description": "외부 최근 동향 웹 검색",
         "input_schema": s(query={"type": "string"})},
        {"name": "ontology_lookup", "description": "사내 온톨로지 엔티티·문서",
         "input_schema": s(name={"type": "string"})},
        {"name": "submit_research", "description": "리서치 완료 — 구조화 결과 제출",
         "input_schema": _SUBMIT_SCHEMA},
    ]


def _dispatch(name: str, args: dict, ctx: ToolContext) -> str:
    try:
        if name == "gmail_search":
            return json.dumps(gmail.search_recent_emails(ctx.creds, args.get("query", ""), args.get("query", "")), ensure_ascii=False)
        if name == "gmail_read_thread":
            return json.dumps(gmail.read_thread(ctx.creds, args.get("thread_id", "")), ensure_ascii=False)
        if name == "drive_search":
            return json.dumps(drive.search_files(ctx.creds, args.get("query", ""), folder_id=ctx.folder_id), ensure_ascii=False)
        if name == "drive_read":
            return drive.read_file_text(ctx.creds, args.get("file_id", ""), args.get("mime_type", ""), args.get("name", ""))
        if name == "slack_channel_history":
            return json.dumps(slack_read.channel_history(ctx.slack_client, args.get("channel", "")), ensure_ascii=False)
        if name == "trello_lookup":
            return json.dumps(trello.get_card_context(ctx.user_id, args.get("company", ""), limit_comments=3) or {}, ensure_ascii=False)
        if name == "web_search":
            from agents import before
            return before._search(args.get("query", ""))
        if name == "ontology_lookup":
            return json.dumps(ontology.company_context(ctx.user_id, args.get("name", ""), recent=True) or {}, ensure_ascii=False)
        return f"unknown tool: {name}"
    except Exception as e:
        log.warning(f"도구 {name} 실패: {e}")
        return f"(도구 {name} 실패: {str(e)[:120]})"
```
그리고 submit 스키마(파일 상단):
```python
_SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_line": {"type": "string"},
        "company_identity_confirmed": {"type": "string",
            "description": "이 회사가 누구인지 확정(동명 타사 배제 근거). 예: 'komsa=한국해양교통안전공단, 독일 KOMSA AG 아님'"},
        "deal_context": {"type": "string"},
        "news": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "summary": {"type": "string"},
            "url": {"type": "string"}, "source": {"type": "string"}}}},
        "connections": {"type": "array", "items": {"type": "string"}},
        "source_docs": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "url": {"type": "string"}, "why": {"type": "string"}}}},
        "attendees": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "role": {"type": "string"},
            "contact": {"type": "string"}, "note": {"type": "string"}}}},
        "talking_points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary_line", "company_identity_confirmed"],
}
```

- [ ] **Step 4: 통과 확인** — `.venv/bin/python -m pytest tests/test_research_agent_dispatch.py -q` → PASS

- [ ] **Step 5: 커밋**
```bash
git add agents/research_agent.py tests/test_research_agent_dispatch.py
git commit -m "feat(research): 에이전트 도구 스펙 + dispatch(기존 도구 read-only 노출) (v1)"
```

---

### Task 6: 에이전트 루프 + submit 파싱

**Files:** Modify `agents/research_agent.py` · Test `tests/test_research_agent_loop.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_research_agent_loop.py`
```python
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import agents.research_agent as ra


def _block(**kw):
    return SimpleNamespace(**kw)


def test_loop_runs_tools_then_submit():
    # 1라운드: drive_search 호출 → 2라운드: submit_research
    r1 = SimpleNamespace(content=[_block(type="tool_use", id="t1", name="drive_search", input={"query": "KOMSA"})])
    r2 = SimpleNamespace(content=[_block(type="tool_use", id="t2", name="submit_research",
            input={"summary_line": "홍보 용역", "company_identity_confirmed": "komsa=해양교통안전공단",
                   "news": [{"title": "전자증서", "summary": "블록체인 발급", "url": "https://x"}],
                   "talking_points": ["굿즈 45%"]})])
    with patch.object(ra._claude.messages, "create", side_effect=[r1, r2]) as mc, \
         patch("agents.research_agent.drive.search_files", return_value=[{"name": "견적서.pdf", "id": "f1"}]), \
         patch.object(ra, "_run_critics", side_effect=lambda r, ctx, called: r):
        ctx = ra.ToolContext(user_id="U1", creds=MagicMock(), folder_id="F1")
        out = ra._agent_loop("KOMSA", "", ctx)
    assert out is not None
    assert out.summary_line == "홍보 용역"
    assert out.news[0].title == "전자증서"
    assert out.talking_points == ["굿즈 45%"]
    assert mc.call_count == 2


def test_loop_returns_none_if_no_submit_in_budget():
    busy = SimpleNamespace(content=[_block(type="tool_use", id="t", name="web_search", input={"query": "x"})])
    with patch.object(ra._claude.messages, "create", return_value=busy), \
         patch("agents.research_agent.before._search", return_value="..."), \
         patch.object(ra, "_MAX_ROUNDS", 3):
        ctx = ra.ToolContext(user_id="U1", creds=MagicMock(), folder_id="F1")
        out = ra._agent_loop("X", "", ctx)
    assert out is None   # 폴백 신호
```

- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_research_agent_loop.py -v` → `AttributeError: ... '_agent_loop'`

- [ ] **Step 3: 구현** — `agents/research_agent.py`에 추가
```python
_MAX_ROUNDS = int(os.getenv("AGENTIC_MAX_ROUNDS", "12"))

_SYSTEM = """당신은 파라메타(parametacorp) 사업개발 리서치 에이전트다. 목표: '제대로'(풍부+정확).
파라메타 사업분야: 블록체인(loopchain), 디지털자산·STO·RWA, DID/MyID, 결제·금융 인프라,
공공·국가 블록체인(K-BTF), 보안·인증(CSAP)·AI보안, 핀테크, 규제 대응.

원칙:
1. 다중홉: 한 도구 결과(파일명·회사명·thread_id)를 다음 검색 쿼리에 적극 사용하라.
   예) 제목→gmail_search→스레드의 견적서 파일명→그 이름으로 drive_search→drive_read.
2. 여러 소스를 교차로 확인하라. Gmail만 보고 끝내지 말 것 — Drive(견적/제안/deck)·Trello·
   (내부/biz 미팅이면) slack_channel_history·web을 관련되면 반드시 들러라.
3. 동명 타사 주의: 회사 동일성을 확정하라(예: komsa=한국해양교통안전공단 vs 독일 KOMSA AG).
4. talking_points는 수집이 아니라 조합 — 전체 맥락에서 미팅 논의 포인트를 도출하라.
5. 충분히 모았으면 submit_research를 호출하라. 모든 주장에 가능한 한 출처를 남겨라."""


def _initial_prompt(company_name: str, meeting_context: str) -> str:
    ctx = f"\n\n미팅 맥락:\n{meeting_context}" if meeting_context else ""
    return f"'{company_name}'에 대해 파라메타 미팅 사전 리서치를 수행하라.{ctx}"


def _agent_loop(company_name: str, meeting_context: str, ctx: ToolContext) -> CompanyResearch | None:
    tools = _tool_specs()
    messages = [{"role": "user", "content": _initial_prompt(company_name, meeting_context)}]
    called: set[str] = set()
    for _round in range(_MAX_ROUNDS):
        resp = _claude.messages.create(model=_MODEL, max_tokens=4096, system=_SYSTEM,
                                       tools=tools, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            break
        results = []
        submit_input = None
        for tu in tool_uses:
            called.add(tu.name)
            if tu.name == "submit_research":
                submit_input = tu.input
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "접수됨"})
            else:
                out = _dispatch(tu.name, tu.input, ctx)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": (out or "")[:8000]})
        messages.append({"role": "user", "content": results})
        if submit_input is not None:
            research = _to_company_research(submit_input, company_name)
            return _run_critics(research, ctx, called)
    return None


def _to_company_research(d: dict, company_name: str) -> CompanyResearch:
    return CompanyResearch(
        company_name=company_name,
        summary_line=d.get("summary_line", ""),
        deal_context=d.get("deal_context", ""),
        news=[NewsItem(title=n.get("title", ""), summary=n.get("summary", ""),
                       url=n.get("url") or None, source=n.get("source", ""))
              for n in (d.get("news") or [])],
        connections=list(d.get("connections") or []),
        source_docs=[SourceDoc(title=s.get("title", ""), url=s.get("url", ""), why=s.get("why", ""))
                     for s in (d.get("source_docs") or [])],
        attendees=[Attendee(name=a.get("name", ""), role=a.get("role", ""),
                            contact=a.get("contact", ""), note=a.get("note", ""))
                   for a in (d.get("attendees") or [])],
        talking_points=list(d.get("talking_points") or []),
    )
```
> `_run_critics`는 Task 7에서 구현. 이 태스크에선 테스트가 `_run_critics`를 patch하므로, 임시로 `def _run_critics(r, ctx, called): return r` 스텁만 둬 통과시키고 Task 7에서 교체.

- [ ] **Step 4: 통과 확인** — `.venv/bin/python -m pytest tests/test_research_agent_loop.py -q` → PASS

- [ ] **Step 5: 커밋**
```bash
git add agents/research_agent.py tests/test_research_agent_loop.py
git commit -m "feat(research): 에이전트 다중홉 루프 + submit 파싱 → CompanyResearch (v1)"
```

---

### Task 7: critic 3종 (URL그라운딩·동명타사·커버리지)

**Files:** Modify `agents/research_agent.py` · Test `tests/test_research_agent_critics.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_research_agent_critics.py`
```python
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock, patch
import agents.research_agent as ra
from agents.research_types import CompanyResearch, NewsItem


def test_coverage_critic_flags_unvisited_sources():
    # gmail/drive 안 들름 → 커버리지 부족 True
    assert ra._coverage_gap({"web_search"}) is True
    assert ra._coverage_gap({"gmail_search", "drive_search", "web_search"}) is False


def test_url_grounding_drops_unsourced_claims():
    r = CompanyResearch(company_name="X", news=[
        NewsItem(title="근거 있음", summary="s", url="https://ok"),
        NewsItem(title="근거 없음", summary="s", url=None)])
    # Haiku가 1번(인덱스1) 미근거로 판정
    with patch.object(ra, "_url_grounding_keep", return_value={0}):
        out = ra._apply_url_grounding(r)
    titles = [n.title for n in out.news]
    assert "근거 있음" in titles and "근거 없음" not in titles


def test_run_critics_keeps_when_all_grounded(monkeypatch):
    r = CompanyResearch(company_name="X", summary_line="ok",
                        news=[NewsItem(title="t", summary="s", url="https://ok")])
    monkeypatch.setattr(ra, "_url_grounding_keep", lambda r: {0})
    ctx = ra.ToolContext(user_id="U", creds=MagicMock(), folder_id="F")
    out = ra._run_critics(r, ctx, called={"gmail_search", "drive_search"})
    assert out.news and out.summary_line == "ok"
```

- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_research_agent_critics.py -v` → FAIL(`_coverage_gap` 등 없음)

- [ ] **Step 3: 구현** — `agents/research_agent.py`(스텁 `_run_critics` 교체)
```python
_HAIKU = "claude-haiku-4-5"
_KEY_SOURCES = {"gmail_search", "drive_search"}   # 최소 들러야 할 소스


def _coverage_gap(called: set[str]) -> bool:
    """핵심 소스(gmail/drive)를 안 들렀으면 커버리지 부족(조기종료 의심)."""
    return not _KEY_SOURCES.issubset(called)


def _url_grounding_keep(r: CompanyResearch) -> set[int]:
    """Haiku 기계적 패스: news 각 항목이 출처(url/source)에 근거하는지 → 유지 인덱스."""
    items = [f"{i}. {n.title} | url={n.url or ''} src={n.source or ''}" for i, n in enumerate(r.news)]
    if not items:
        return set()
    prompt = ("아래 뉴스 항목 중 **출처(url 또는 src)가 실재하는** 항목의 번호만 JSON으로.\n"
              '형식: {"keep":[0,2]}\n\n' + "\n".join(items))
    try:
        resp = _claude.messages.create(model=_HAIKU, max_tokens=256,
                                       messages=[{"role": "user", "content": prompt}])
        raw = resp.content[0].text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return {int(i) for i in json.loads(raw).get("keep", [])}
    except Exception as e:
        log.warning(f"url grounding 실패, 전체 유지: {e}")
        return set(range(len(r.news)))


def _apply_url_grounding(r: CompanyResearch) -> CompanyResearch:
    keep = _url_grounding_keep(r)
    r.news = [n for i, n in enumerate(r.news) if i in keep]
    return r


def _run_critics(r: CompanyResearch, ctx: ToolContext, called: set[str]) -> CompanyResearch:
    """① URL 그라운딩(Haiku) 적용. ②동명타사=합성 모델 책임(submit의 company_identity_confirmed로
    이미 강제, 비면 로그). ③커버리지는 루프에서 한 번 nudge(아래 Task6 연계는 v1 로그로 관측)."""
    r = _apply_url_grounding(r)
    if _coverage_gap(called):
        log.info(f"[AGENTIC] 커버리지 부족(들른 소스={called}) — {r.company_name}")
    return r
```
> 동명타사(critic ②)는 capable 합성 모델이 `submit_research`의 `company_identity_confirmed`를 채우게 강제(스키마 required)함으로써 책임지움 — 별도 Haiku 호출 안 함(스펙 §6-2). 커버리지 nudge의 능동 재탐색은 v1에선 로그 관측만, 실측 후 루프 내 강제로 승격(스펙 §11 게이트).

- [ ] **Step 4: 통과 확인** — `.venv/bin/python -m pytest tests/test_research_agent_critics.py tests/test_research_agent_loop.py -q` → PASS

- [ ] **Step 5: 커밋**
```bash
git add agents/research_agent.py tests/test_research_agent_critics.py
git commit -m "feat(research): critic — URL그라운딩(Haiku)+동명타사(스키마 강제)+커버리지 로그 (v1)"
```

---

### Task 8: `run_agentic_research` 진입점 + run_company_research 위임/폴백

**Files:** Modify `agents/research_agent.py`, `agents/research_orchestrator.py` · Test `tests/test_agentic_wiring.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_agentic_wiring.py`
```python
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock, patch
import agents.research_orchestrator as ro
from agents.research_types import CompanyResearch


def test_flag_off_uses_legacy(monkeypatch):
    monkeypatch.delenv("AGENTIC_RESEARCH", raising=False)
    with patch("agents.research_agent.run_agentic_research") as ag, \
         patch.object(ro, "_company_industry", lambda *a, **k: {}), \
         patch.object(ro, "_company_competitors", lambda *a, **k: {"peers": []}), \
         patch.object(ro, "_company_trends", lambda *a, **k: "- 정보 없음"), \
         patch("agents.news_relevance.judge", lambda items, c: items), \
         patch.object(ro, "_company_synthesis", lambda **k: "개요"):
        out = ro.run_company_research(company_name="X")
    ag.assert_not_called()
    assert isinstance(out, CompanyResearch)


def test_flag_on_delegates_to_agent(monkeypatch):
    monkeypatch.setenv("AGENTIC_RESEARCH", "true")
    fake = CompanyResearch(company_name="KOMSA", summary_line="에이전트 결과")
    with patch("agents.research_agent.run_agentic_research", return_value=fake) as ag:
        out = ro.run_company_research(company_name="KOMSA", user_id="U1", creds=MagicMock())
    ag.assert_called_once()
    assert out.summary_line == "에이전트 결과"


def test_flag_on_agent_fail_falls_back(monkeypatch):
    monkeypatch.setenv("AGENTIC_RESEARCH", "true")
    with patch("agents.research_agent.run_agentic_research", return_value=None), \
         patch.object(ro, "_company_industry", lambda *a, **k: {}), \
         patch.object(ro, "_company_competitors", lambda *a, **k: {"peers": []}), \
         patch.object(ro, "_company_trends", lambda *a, **k: "- 정보 없음"), \
         patch("agents.news_relevance.judge", lambda items, c: items), \
         patch.object(ro, "_company_synthesis", lambda **k: "개요"):
        out = ro.run_company_research(company_name="KOMSA", user_id="U1", creds=MagicMock())
    assert isinstance(out, CompanyResearch) and out.company_name == "KOMSA"  # 폴백 경로
```

- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_agentic_wiring.py -v` → FAIL

- [ ] **Step 3: 구현**
`agents/research_agent.py` 진입점:
```python
def run_agentic_research(*, company_name: str, user_id: str, creds, slack_client=None,
                         meeting_context: str = "") -> CompanyResearch | None:
    """에이전트 리서치. 성공 시 CompanyResearch, 실패/미완 시 None(호출부 폴백)."""
    folder_id = os.getenv("DRIVE_RESEARCH_FOLDER_ID", "")
    ctx = ToolContext(user_id=user_id, creds=creds, slack_client=slack_client, folder_id=folder_id)
    try:
        return _agent_loop(company_name, meeting_context, ctx)
    except Exception as e:
        log.exception(f"에이전트 리서치 실패, 폴백 ({company_name}): {e}")
        return None
```
`agents/research_orchestrator.py` `run_company_research` 시그니처에 `user_id`/`creds`/`slack_client` 옵션 추가 + 위임:
```python
def run_company_research(*, company_name: str, knowledge_md: str = "",
                          gmail_context: str = "", user_id: str = "",
                          creds=None, slack_client=None) -> "CompanyResearch":
    from agents.research_types import CompanyResearch, parse_trend_bullets
    # 단계: 에이전트 플래그 ON + 자격 있으면 위임, 실패 시 레거시 폴백
    try:
        from agents import research_agent
        if research_agent.agentic_enabled() and user_id and creds is not None:
            agent_out = research_agent.run_agentic_research(
                company_name=company_name, user_id=user_id, creds=creds,
                slack_client=slack_client, meeting_context=gmail_context)
            if agent_out is not None:
                return agent_out
            log.info(f"에이전트 미완 → 레거시 폴백 ({company_name})")
    except Exception as e:
        log.warning(f"에이전트 경로 오류, 레거시 폴백 ({company_name}): {e}")
    # ── 레거시 고정 파이프라인(기존 본문 그대로) ──
    ...
```
> 기존 `run_company_research` 본문(병렬 수집→judge→synthesis→CompanyResearch)은 그대로 폴백으로 남긴다. 호출부 `before.research_company`는 `user_id`/`creds`/`slack_client`를 넘기도록 한 줄 보강(이미 creds 보유).

- [ ] **Step 4: 통과 확인** — `.venv/bin/python -m pytest tests/test_agentic_wiring.py tests/ -q` → PASS(전체 회귀)

- [ ] **Step 5: 커밋**
```bash
git add agents/research_agent.py agents/research_orchestrator.py agents/before.py tests/test_agentic_wiring.py
git commit -m "feat(research): run_company_research 에이전트 위임 + 폴백 + 킬스위치(AGENTIC_RESEARCH) (v1)"
```

---

### Task 9: 확장 필드 Slack 렌더

**Files:** Modify `tools/slack_tools.py` · Test `tests/test_render_extended.py`

- [ ] **Step 1: 실패 테스트** — `tests/test_render_extended.py`
```python
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from tools.slack_tools import build_company_research_block_v2
from agents.research_types import CompanyResearch, NewsItem, SourceDoc, Attendee


def test_renders_all_sections():
    r = CompanyResearch(
        company_name="KOMSA", summary_line="홍보 용역 범위 협의",
        deal_context="6/11 RFQ→6/15 견적→6/26 확정",
        news=[NewsItem(title="전자증서", summary="블록체인 발급", url="https://x")],
        connections=["loopchain ↔ 전자증서"],
        source_docs=[SourceDoc(title="견적서.pdf", url="https://drive/x", why="견적 항목")],
        attendees=[Attendee(name="이성룡", role="국장", contact="a@d-antwort.com")],
        talking_points=["굿즈가 견적 45%"])
    text = build_company_research_block_v2(r)[0]["text"]["text"]
    assert "홍보 용역 범위 협의" in text          # summary_line
    assert "RFQ" in text                          # deal_context
    assert "<https://x|전자증서>" in text          # news 링크
    assert "견적서.pdf" in text                    # source_docs
    assert "이성룡" in text and "국장" in text      # attendees
    assert "굿즈가 견적 45%" in text               # talking_points


def test_cold_meeting_graceful():
    r = CompanyResearch(company_name="신규업체", summary_line="첫 미팅")
    text = build_company_research_block_v2(r)[0]["text"]["text"]
    assert "신규업체" in text   # 빈 이력이어도 정상 렌더(에러 없음)
```

- [ ] **Step 2: 실패 확인** — `.venv/bin/python -m pytest tests/test_render_extended.py -v` → `ImportError: ... build_company_research_block_v2`

- [ ] **Step 3: 구현** — `tools/slack_tools.py`에 `build_company_research_block_v2(r: CompanyResearch)` 추가. 기존 `_format_news_item_for_slack`·`_strip_display_markdown` 재사용. 섹션 순서: 요약 → 업체 동향 → 거래 맥락 → 연결점 → 참석자 → 자료 → 논의 포인트. 각 섹션 빈 값이면 생략(콜드 graceful).
```python
def build_company_research_block_v2(r) -> list[dict]:
    L = ["━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", f"*🏢 {r.company_name} 리서치 결과*"]
    if r.summary_line:
        L += ["", f"📌 {r.summary_line}"]
    L += ["", "📰  *업체 동향*"]
    news = [s for s in (_format_news_item_for_slack(
        {"title": n.title, "summary": n.summary, "url": n.url}) for n in r.news) if s]
    L += [f"• {s}" for s in news[:3]] or ["• 최근 동향 정보 없음"]
    if r.deal_context:
        L += ["", "🔄  *거래 맥락*", _strip_display_markdown(r.deal_context)]
    if r.connections:
        L += ["", "🔗  *파라메타 서비스 연결점*"] + [f"• {_strip_display_markdown(c)}" for c in r.connections[:3]]
    if r.attendees:
        L += ["", "👤  *참석자*"]
        for a in r.attendees[:5]:
            bits = " · ".join(x for x in (a.role, a.contact) if x)
            L.append(f"• {a.name}" + (f" ({bits})" if bits else ""))
    if r.source_docs:
        L += ["", "📎  *자료*"]
        for d in r.source_docs[:5]:
            L.append(f"• <{d.url}|{_doc_label(d.title)}>" + (f" — {d.why}" if d.why else "") if d.url
                     else f"• {_doc_label(d.title)}" + (f" — {d.why}" if d.why else ""))
    if r.talking_points:
        L += ["", "✅  *오늘 논의 포인트*"] + [f"• {_strip_display_markdown(t)}" for t in r.talking_points[:5]]
    return [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(L)}}]
```
> 온디맨드 경로(`main._post_company_research_result`)는 `run_company_research`가 에이전트 결과(확장 필드 보유)를 반환하면 `build_company_research_block_v2`로 렌더하도록 분기(확장 필드 비면 기존 `build_company_research_block` 사용 — 레거시 폴백 호환). 이 분기는 Task 8의 호출부 보강과 함께.

- [ ] **Step 4: 통과 확인** — `.venv/bin/python -m pytest tests/test_render_extended.py tests/ -q` → PASS(전체)

- [ ] **Step 5: 커밋**
```bash
git add tools/slack_tools.py main.py tests/test_render_extended.py
git commit -m "feat(render): 확장 CompanyResearch 렌더(요약·거래맥락·참석자·자료·논의포인트) (v1)"
```

---

## 완료 기준 (DoD)
- `AGENTIC_RESEARCH=true` + 사용자 creds일 때 `{업체} 리서치`가 에이전트 다중홉으로 `CompanyResearch`(확장) 생성, 실패 시 레거시 폴백. 전체 `pytest` 통과.
- 도구 read-only, 사용자별 OAuth, Slack allowlist·Drive 폴더 범위 한정.
- critic: URL그라운딩 적용, 동명타사 스키마 강제, 커버리지 로그.

## 라이브 검증 게이트 (스펙 §11 — 배포 후 수동, on-demand로 실측)
- [ ] `.env`에 `DRIVE_RESEARCH_FOLDER_ID`, `SLACK_BIZ_CHANNELS` 설정 + 봇을 biz 채널 초대 + `channels:history` 스코프.
- [ ] **hwpx**: 한글 견적서/SOW가 실제 추출돼 항목분해까지 나오는지.
- [ ] **Slack 채널**: 내부/biz 미팅에서 자유서술 맥락(PoC 상태·일정)이 잡히는지.
- [ ] **sharedWithMe**: 동료 공유 deck·첨부가 후보에 잡히는지.
- [ ] **동명타사**: 디안트보르트·komsa류 오인 0건(`company_identity_confirmed` 확인).
- [ ] 비용/지연 실측(미팅당 도구호출 수·시간) → 스케줄 전환 전 예산 확정.

## 다음 (이 계획 밖)
- 스케줄 브리핑을 엔진으로 전환(비용 최적화: 외부미팅 한정·캐시).
- 커버리지 critic 능동 재탐색(로그 관측 후 루프 내 강제로 승격).
- `.hwp`(레거시 바이너리) 추출, 첨부 OCR.
