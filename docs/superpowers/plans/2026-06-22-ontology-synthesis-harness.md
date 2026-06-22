# 온톨로지 합성 하네스(딥 리서치 + 품질관리) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 온디맨드 "업체 리서치"를 cluster→document_fetch→Sonnet 합성+grounding critic으로 사람이 읽는 출처기반 브리핑으로 만든다.

**Architecture:** `tools/ontology.py`(retrieval+R1 필터)가 `company_research_sources`로 관련 문서 본문을 모으고, 신규 `agents/ontology_synth.py`(Sonnet 합성 R2 + critic R3, `news_relevance.py` 모듈 패턴)가 출처기반 브리핑을 생성. `agents/before.deep_company_ontology`가 묶어 게이팅, `main._post_company_research_result`가 딥 브리핑을 렌더(실패 시 라이트 폴백).

**Tech Stack:** Python, httpx(MockTransport), anthropic(claude-sonnet-4-5 합성 / claude-haiku-4-5 critic), pytest. 선행: 배포된 `tools/ontology.py`(`OntologyClient`/`company_context`/`_best_slug`/`_normalize_cluster`/`_recent_range`).

**선행 스펙:** `docs/superpowers/specs/2026-06-22-ontology-synthesis-harness-design.md`
**범위:** 딥 리서치 티어 + 품질 하네스 + golden eval. (브리핑 티어는 다음 증분.)

---

### Task 1: `_normalize_cluster` 보강 + `document_fetch`

**Files:**
- Modify: `tools/ontology.py` (`_normalize_cluster` ~88-109, `OntologyClient`/모듈함수)
- Test: `tests/test_ontology_fetch.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_fetch.py`

```python
"""tools/ontology.py — _normalize_cluster 보강 + document_fetch"""
import os
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import httpx, json
import tools.ontology as ont


class TestNormalizeEnriched:
    def test_docs_carry_uri_space_ym_matched(self):
        cluster = {"seed": "entity/komsa", "entities": [
            {"slug": "entity/kca", "via": "related-to", "title": "KCA", "hop": 1}],
            "documents": [{
                "document_id": "raw://d1", "title": "KOMSA 제안서",
                "source_uri": "https://drive/x", "space_display": "Drive", "ym": "2026-05",
                "matched_via_entities": ["entity/komsa"]}]}
        out = ont._normalize_cluster(cluster, "entity/komsa")
        d = out["documents"][0]
        assert d["uri"] == "https://drive/x" and d["space"] == "Drive"
        assert d["ym"] == "2026-05" and "entity/komsa" in d["matched"]
        assert d["id"] == "raw://d1"


def _fetch_transport():
    def h(req):
        body = json.loads(req.content.decode())
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc":"2.0","id":1,"result":{
                "serverInfo":{"name":"lib-mesh"},"protocolVersion":"2025-06-18","capabilities":{}}})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202, json={})
        # tools/call document_fetch
        data = {"document_id":"raw://d1","title":"KOMSA 제안서",
                "body_markdown":"총 266억 규모 DID/VC 검증체계","source_uri":"https://drive/x",
                "frontmatter":{"space_display":"Drive"}}
        return httpx.Response(200, json={"jsonrpc":"2.0","id":2,"result":{
            "content":[{"type":"text","text":json.dumps({"data":data})}]}})
    return httpx.MockTransport(h)


class TestDocumentFetch:
    def test_fetch_returns_summary(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")
        oc = ont.OntologyClient("eyJa.b.c", url="https://ont.x/mcp")
        oc._http = httpx.Client(transport=_fetch_transport())
        monkeypatch.setattr(ont, "OntologyClient", lambda *a, **k: oc)
        out = ont.document_fetch("U1", "raw://d1")
        assert "266억" in out["summary"]
        assert out["title"] == "KOMSA 제안서" and out["uri"] == "https://drive/x"
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_fetch.py -v`
Expected: FAIL — `KeyError: 'uri'` / `AttributeError: ... has no attribute 'document_fetch'`

- [ ] **Step 3: 구현** — `tools/ontology.py`

(a) `_normalize_cluster`의 `doclist` 생성부 교체(문서 메타 보존):

```python
    doclist = [
        {"title": d.get("title") or d.get("name") or d.get("id"),
         "id": d.get("document_id") or d.get("id"),
         "uri": d.get("source_uri") or d.get("sourceUrl") or "",
         "space": d.get("space_display") or d.get("space") or "",
         "ym": d.get("ym") or "",
         "matched": d.get("matched_via_entities") or []}
        for d in docs
    ]
```

(b) 모듈 함수 `document_fetch` 추가(`company_context` 다음):

```python
def document_fetch(user_id: str, document_id: str, level: str = "summary",
                   max_chars: int = 3000) -> dict | None:
    """문서 요약/본문 가져오기. {title, summary, uri, space}. 토큰 없으면 None."""
    token = user_store.get_ontology_token(user_id)
    if not token:
        return None
    with OntologyClient(token) as oc:
        data = oc.call_tool("document_fetch", {
            "document_id": document_id, "level": level, "max_chars": max_chars})
    if not isinstance(data, dict):
        return {"title": "", "summary": str(data or ""), "uri": "", "space": ""}
    fm = data.get("frontmatter") or {}
    return {
        "title": data.get("title") or fm.get("title") or "",
        "summary": (data.get("body_markdown") or "").strip(),
        "uri": data.get("source_uri") or fm.get("sourceUrl") or "",
        "space": fm.get("space_display") or fm.get("space") or "",
    }
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_fetch.py tests/test_ontology_client.py -v`
Expected: PASS (기존 client 테스트 포함 — `_normalize_cluster` 기존 호출부 `_company_ontology`/`company_context`는 `relations`·`documents` 키 유지하므로 불변)

- [ ] **Step 5: 커밋**

```bash
git add tools/ontology.py tests/test_ontology_fetch.py
git commit -m "feat(ontology): _normalize_cluster 문서 메타 보강 + document_fetch"
```

---

### Task 2: `company_research_sources` (R1 필터 + 문서 선별 + 본문 수집)

**Files:**
- Modify: `tools/ontology.py`
- Test: `tests/test_ontology_sources.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_sources.py`

```python
"""tools/ontology.py — company_research_sources (R1 필터 + fetch 묶음)"""
import os
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import tools.ontology as ont


class TestResearchSources:
    def test_filters_offcompany_and_fetches(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")
        calls = {"fetch": []}

        class FakeClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def call_tool(self, name, args):
                if name == "entity_find":
                    return {"matches": [{"slug": "entity/komsa", "match_kind": "exact", "confidence": 0.95}]}
                # entity_cluster
                return {"seed": "entity/komsa", "entities": [
                    {"slug": "entity/kca", "via": "related-to", "title": "KCA"}],
                    "documents": [
                        {"document_id": "d1", "title": "KOMSA 제안서", "ym": "2026-05",
                         "source_uri": "u1", "space_display": "Drive",
                         "matched_via_entities": ["entity/komsa"]},
                        {"document_id": "d_off", "title": "타사 문서", "ym": "2026-05",
                         "source_uri": "u2", "space_display": "EN",
                         "matched_via_entities": ["entity/other"]},  # 업체 미연결 → R1 제거
                    ]}

        monkeypatch.setattr(ont, "OntologyClient", FakeClient)
        monkeypatch.setattr(ont, "document_fetch",
                            lambda uid, did, **k: {"title": did, "summary": f"본문 {did}",
                                                   "uri": "u", "space": "s"} or calls["fetch"].append(did))
        out = ont.company_research_sources("U1", "KOMSA", max_docs=4)
        ids = [d["id"] for d in out["docs"]]
        assert "d1" in ids and "d_off" not in ids        # R1: 업체 연결만
        assert out["docs"][0]["summary"].startswith("본문")  # fetch 본문 채워짐
        assert out["seed"] == "entity/komsa"

    def test_no_token_none(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: None)
        assert ont.company_research_sources("U1", "KOMSA") is None
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_sources.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'company_research_sources'`

- [ ] **Step 3: 구현** — `tools/ontology.py` 끝에 추가

```python
# 문서 우선순위: 제안서·계약·회의록 > 발표 > 주간보고 (낮을수록 우선)
def _doc_priority(title: str) -> int:
    t = (title or "")
    if any(k in t for k in ("제안서", "계약", "회의록", "RFP")):
        return 0
    if any(k in t for k in ("발표", "Proposal", "구성도", "설계")):
        return 1
    return 2


def company_research_sources(user_id: str, company_name: str, max_docs: int = 6) -> dict | None:
    """딥 리서치 입력 — entity_find→cluster→R1 필터→상위문서 document_fetch.
    토큰 없으면 None. Returns: {seed, relations[], docs:[{title,summary,uri,space,ym,id}]}.
    R1: 업체 엔티티에 직접 연결된 문서(matched_via_entities에 seed 포함)만."""
    token = user_store.get_ontology_token(user_id)
    if not token:
        return None
    with OntologyClient(token) as oc:
        find = oc.call_tool("entity_find", {"name": company_name, "limit": 5})
        slug = _best_slug(find)
        if not slug:
            return {"seed": None, "relations": [], "docs": []}
        cluster = oc.call_tool("entity_cluster", {
            "seed": slug, "depth": 2, "include_documents": True,
            "limit_entities": 40, "limit_documents": 30, "time_range": _recent_range(12)})
    norm = _normalize_cluster(cluster, slug)
    # R1: 업체 직접 연결 문서만 (matched에 seed 포함)
    connected = [d for d in norm["documents"] if slug in (d.get("matched") or [])]
    pool = connected or norm["documents"]  # 연결문서 0이면 전체에서라도
    pool = sorted(pool, key=lambda d: (_doc_priority(d.get("title", "")),
                                       -_ym_key(d.get("ym", ""))))[:max_docs]
    docs = []
    for d in pool:
        if not d.get("id"):
            continue
        fetched = None
        try:
            fetched = document_fetch(user_id, d["id"])
        except Exception as fe:
            log.warning(f"document_fetch 실패({d.get('title')}): {fe}")
        docs.append({**d, "summary": (fetched or {}).get("summary", ""),
                     "uri": d.get("uri") or (fetched or {}).get("uri", "")})
    return {"seed": slug, "relations": norm["relations"], "docs": docs}


def _ym_key(ym: str) -> int:
    """'2026-05' → 202605 (정렬용). 빈값 0."""
    try:
        return int((ym or "").replace("-", "")[:6] or 0)
    except Exception:
        return 0
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_sources.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add tools/ontology.py tests/test_ontology_sources.py
git commit -m "feat(ontology): company_research_sources — R1 필터 + 문서 선별·본문 수집"
```

---

### Task 3: 합성·critic 프롬프트 템플릿

**Files:**
- Create: `prompts/templates/ontology_brief.md`, `prompts/templates/ontology_grounding_check.md`

- [ ] **Step 1: `prompts/templates/ontology_brief.md` 작성**

```markdown
너는 파라메타(우리 회사)의 비즈니스 리서치 어시스턴트다. 아래 **사내 지식 출처**만으로 "{{company}}" 사내 지식 브리핑을 작성한다.

규칙(엄수):
- 제공된 출처 스니펫에 **명시된 사실만** 사용. 추론·추측·외부지식 금지.
- 핵심 수치·계약·일정 뒤에 `[출처: 문서명]` 표기.
- 출처에 근거 없는 문장은 쓰지 마라. 모르면 생략.
- 한국어. 군더더기 없이 사실 위주. 4~6개 불릿 + 첫 줄 1문장 요약.

[관계 그래프]
{{relations}}

[문서 출처]
{{sources}}

출력 형식(마크다운):
<1문장 요약>

• <핵심 사실> `[출처: ...]`
• ...
```

- [ ] **Step 2: `prompts/templates/ontology_grounding_check.md` 작성**

```markdown
너는 사실검증기다. 아래 "브리핑"의 각 문장이 "출처"에 의해 뒷받침되는지 검사한다.

[출처]
{{sources}}

[브리핑]
{{brief}}

작업: 브리핑에서 출처에 근거가 **없는**(환각/추측) 문장·수치를 식별하고, 그 문장을 제거한 **교정된 브리핑**을 출력한다. 출처로 뒷받침되는 내용은 그대로 둔다. 설명 없이 교정된 마크다운만 출력.
```

- [ ] **Step 3: 커밋**

```bash
git add prompts/templates/ontology_brief.md prompts/templates/ontology_grounding_check.md
git commit -m "feat(ontology): 딥 리서치 합성·grounding critic 프롬프트 템플릿"
```

---

### Task 4: `agents/ontology_synth.py` — 합성(R2) + grounding critic(R3)

**Files:**
- Create: `agents/ontology_synth.py`
- Test: `tests/test_ontology_synth.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_synth.py`

```python
"""agents/ontology_synth.py — 합성 + grounding critic"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import MagicMock
import agents.ontology_synth as synth


def _sources():
    return {"seed": "entity/komsa",
            "relations": [{"relation": "related-to", "title": "KISA 공공과제"}],
            "docs": [{"title": "KOMSA 제안서", "summary": "총 266억 규모 DID/VC 검증체계",
                      "uri": "https://drive/x", "space": "Drive", "ym": "2026-05"}]}


def test_synthesize_calls_llm_and_returns_brief(monkeypatch):
    resp_brief = MagicMock(); resp_brief.content = [MagicMock(text="KOMSA 요약\n\n• 총 266억 [출처: KOMSA 제안서]")]
    resp_crit = MagicMock(); resp_crit.content = [MagicMock(text="KOMSA 요약\n\n• 총 266억 [출처: KOMSA 제안서]")]
    calls = []
    def fake_create(**kw):
        calls.append(kw["model"]); return resp_brief if len(calls) == 1 else resp_crit
    monkeypatch.setattr(synth._claude.messages, "create", fake_create)
    out = synth.synthesize_company_brief("KOMSA", _sources())
    assert "266억" in out and "출처" in out
    assert calls[0].startswith("claude-sonnet")   # 합성=Sonnet
    assert len(calls) == 2                          # 합성 + critic


def test_empty_sources_returns_none():
    assert synth.synthesize_company_brief("KOMSA", {"seed": None, "relations": [], "docs": []}) is None


def test_synthesis_failure_returns_none(monkeypatch):
    def boom(**kw): raise RuntimeError("api down")
    monkeypatch.setattr(synth._claude.messages, "create", boom)
    assert synth.synthesize_company_brief("KOMSA", _sources()) is None


def test_critic_failure_falls_back_to_raw_synthesis(monkeypatch):
    resp_brief = MagicMock(); resp_brief.content = [MagicMock(text="원본 합성 [출처: KOMSA 제안서]")]
    n = {"i": 0}
    def fake_create(**kw):
        n["i"] += 1
        if n["i"] == 1: return resp_brief
        raise RuntimeError("critic down")
    monkeypatch.setattr(synth._claude.messages, "create", fake_create)
    out = synth.synthesize_company_brief("KOMSA", _sources())
    assert out == "원본 합성 [출처: KOMSA 제안서]"   # critic 실패 → 합성 결과 그대로
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_synth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.ontology_synth'`

- [ ] **Step 3: 구현** — `agents/ontology_synth.py` 생성

```python
"""온톨로지 딥 리서치 합성 — 출처기반 브리핑(R2) + grounding critic(R3).

news_relevance.py 패턴: 자체 anthropic 클라이언트 + 템플릿 핫리로드.
합성=Sonnet(고품질), critic=Haiku(검증은 가벼움). best-effort: 실패 시 None/폴백.
"""
import logging
import os
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_SYNTH_MODEL = "claude-sonnet-4-5"
_CRITIC_MODEL = "claude-haiku-4-5"
_TPL_DIR = Path(__file__).parent.parent / "prompts" / "templates"


def _load(name: str) -> str:
    try:
        return (_TPL_DIR / name).read_text(encoding="utf-8")
    except Exception as e:
        log.warning(f"{name} 로드 실패: {e}")
        return ""


def _fmt_relations(relations: list) -> str:
    return "\n".join(f"- {r.get('relation')}: {r.get('title')}" for r in (relations or [])) or "(없음)"


def _fmt_sources(docs: list) -> str:
    out = []
    for d in (docs or []):
        if not d.get("summary"):
            continue
        ym = f" ({d['ym']})" if d.get("ym") else ""
        out.append(f"### {d.get('title','')}{ym}\n{d.get('summary','')}")
    return "\n\n".join(out) or "(없음)"


def synthesize_company_brief(company: str, sources: dict) -> str | None:
    """출처기반 합성(R2) → grounding critic(R3) → 교정 마크다운. 출처 없으면 None."""
    if not sources or not (sources.get("docs") or sources.get("relations")):
        return None
    src_text = _fmt_sources(sources.get("docs"))
    if src_text == "(없음)":
        return None  # 본문 있는 문서가 하나도 없으면 합성 불가
    rel_text = _fmt_relations(sources.get("relations"))
    # R2 합성
    prompt = (_load("ontology_brief.md")
              .replace("{{company}}", company)
              .replace("{{relations}}", rel_text)
              .replace("{{sources}}", src_text))
    try:
        resp = _claude.messages.create(model=_SYNTH_MODEL, max_tokens=1500,
                                       messages=[{"role": "user", "content": prompt}])
        brief = resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"온톨로지 합성 실패({company}): {e}")
        return None
    if not brief:
        return None
    # R3 grounding critic (best-effort)
    try:
        check = (_load("ontology_grounding_check.md")
                 .replace("{{sources}}", src_text)
                 .replace("{{brief}}", brief))
        cresp = _claude.messages.create(model=_CRITIC_MODEL, max_tokens=1500,
                                        messages=[{"role": "user", "content": check}])
        corrected = cresp.content[0].text.strip()
        return corrected or brief
    except Exception as e:
        log.warning(f"grounding critic 실패({company}), 합성 결과 통과: {e}")
        return brief
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_synth.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add agents/ontology_synth.py tests/test_ontology_synth.py
git commit -m "feat(ontology): ontology_synth — 출처기반 합성(Sonnet) + grounding critic(Haiku)"
```

---

### Task 5: `agents/before.deep_company_ontology` + 렌더 분기

**Files:**
- Modify: `agents/before.py` (`_company_ontology` 다음)
- Test: `tests/test_deep_company_ontology.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_deep_company_ontology.py`

```python
"""agents/before.deep_company_ontology — 게이팅·합성·폴백"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import agents.before as before


def test_returns_brief_when_enabled(monkeypatch):
    monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
    import tools.ontology as ont
    monkeypatch.setattr(ont, "company_research_sources",
                        lambda uid, c, max_docs=6: {"seed": "entity/komsa", "relations": [],
                                                    "docs": [{"title": "제안서", "summary": "266억"}]})
    import agents.ontology_synth as synth
    monkeypatch.setattr(synth, "synthesize_company_brief", lambda c, s: "KOMSA 브리핑 266억")
    assert before.deep_company_ontology("U1", "KOMSA") == "KOMSA 브리핑 266억"


def test_none_when_disabled(monkeypatch):
    monkeypatch.setattr(before, "_ontology_enabled", lambda uid: False)
    assert before.deep_company_ontology("U1", "KOMSA") is None


def test_none_on_error(monkeypatch):
    monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
    import tools.ontology as ont
    def boom(uid, c, max_docs=6): raise RuntimeError("net")
    monkeypatch.setattr(ont, "company_research_sources", boom)
    assert before.deep_company_ontology("U1", "KOMSA") is None
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_deep_company_ontology.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'deep_company_ontology'`

- [ ] **Step 3: 구현** — `agents/before.py`의 `_company_ontology` 함수 정의 끝 다음에 추가

```python
def deep_company_ontology(user_id: str, company_name: str) -> str | None:
    """게이팅된 사용자 대상 딥 리서치 — 출처기반 합성 브리핑(마크다운) 반환.
    비활성/토큰없음/출처없음/실패 시 None(호출부가 라이트로 폴백). 렌더 전용(위키 미저장)."""
    if not _ontology_enabled(user_id):
        return None
    try:
        from tools import ontology
        from agents import ontology_synth
        sources = ontology.company_research_sources(user_id, company_name)
        if not sources or not sources.get("docs"):
            return None
        return ontology_synth.synthesize_company_brief(company_name, sources)
    except Exception as oe:
        log.warning(f"딥 온톨로지 리서치 실패({company_name}): {oe}")
        return None
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_deep_company_ontology.py tests/ -q`
Expected: PASS (신규 + 전체 회귀)

- [ ] **Step 5: 커밋**

```bash
git add agents/before.py tests/test_deep_company_ontology.py
git commit -m "feat(ontology): deep_company_ontology — 게이팅된 딥 리서치 묶음"
```

---

### Task 6: `main._post_company_research_result` 딥 브리핑 렌더 (게이팅·폴백)

**Files:**
- Modify: `main.py` (`_post_company_research_result` ~1174-1216), `tools/slack_tools.py` (블록에 딥 브리핑 표시)
- Test: `tests/test_company_research_ontology.py` (기존 파일에 추가)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_company_research_ontology.py`에 추가

```python
class TestDeepBriefRender:
    def test_block_renders_deep_brief_text(self):
        from tools.slack_tools import build_company_research_block
        blocks = build_company_research_block(
            "KOMSA", [], [], [], None, None, "", "",
            ontology_brief="KOMSA 요약\n\n• 총 266억 `[출처: 제안서]`")
        text = blocks[0]["text"]["text"]
        assert "온톨로지(사내 지식)" in text and "266억" in text
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_company_research_ontology.py::TestDeepBriefRender -v`
Expected: FAIL — `build_company_research_block() got an unexpected keyword argument 'ontology_brief'`

- [ ] **Step 3a: 구현 — `tools/slack_tools.py` `build_company_research_block`**

시그니처에 `ontology_brief: str | None = None` 추가(`ontology` 파라미터 다음). 그리고 `ontology` 원시 렌더 블록 앞에 우선 분기 추가 — 기존:
```python
    if ontology and (ontology.get("relations") or ontology.get("documents")):
        lines.append("")
        lines.append("🧠  *온톨로지(사내 지식)*")
```
교체:
```python
    if ontology_brief:
        lines.append("")
        lines.append("🧠  *온톨로지(사내 지식)*")
        for bl in ontology_brief.splitlines():
            if bl.strip():
                lines.append(bl.rstrip())
    elif ontology and (ontology.get("relations") or ontology.get("documents")):
        lines.append("")
        lines.append("🧠  *온톨로지(사내 지식)*")
```
(elif 이후 기존 원시 렌더 라인들은 그대로 둔다 — 딥 브리핑 없을 때 라이트 폴백.)

- [ ] **Step 3b: 구현 — `main.py` `_post_company_research_result`**

기존:
```python
    # 온톨로지(사내 지식) — 렌더 시점에만 주입(위키 미저장, 게이팅·best-effort)
    onto = before_agent._company_ontology(user_id, company)

    blocks = before_agent.build_company_research_block(
        company, news_lines, parascope_lines, connection_lines, update_lines,
        trello_summary, trello_card_name, trello_url, ontology=onto,
    )
```
교체:
```python
    # 온톨로지 — 게이팅 사용자는 딥 리서치 브리핑(합성), 실패 시 라이트 cluster 폴백
    onto_brief = before_agent.deep_company_ontology(user_id, company)
    onto = None if onto_brief else before_agent._company_ontology(user_id, company)

    blocks = before_agent.build_company_research_block(
        company, news_lines, parascope_lines, connection_lines, update_lines,
        trello_summary, trello_card_name, trello_url,
        ontology=onto, ontology_brief=onto_brief,
    )
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_company_research_ontology.py tests/ -q`
Expected: PASS (전체 회귀)

- [ ] **Step 5: 커밋**

```bash
git add main.py tools/slack_tools.py tests/test_company_research_ontology.py
git commit -m "feat(ontology): 업체 리서치에 딥 브리핑 렌더(게이팅·라이트 폴백)"
```

---

### Task 7: golden eval(개발타임) + 문서 + 최종 회귀

**Files:**
- Create: `tests/eval_ontology_grounding.py`, `tests/golden/ontology_grounding.jsonl`
- Modify: `CLAUDE.md`

- [ ] **Step 1: 골든셋 작성** — `tests/golden/ontology_grounding.jsonl` (2건)

```jsonl
{"id": "komsa-grounded", "company": "KOMSA", "sources": ["KOMSA 제안서(2026-05): 총 266억 규모 DID/VC 검증체계, 파라메타·아일리스프런티어·이웃 컨소시엄"], "brief": "KOMSA는 KISA 공공과제 수요기관입니다.\n\n• 총 266억 규모 DID/VC 검증체계 [출처: KOMSA 제안서]", "expected_grounded": true}
{"id": "komsa-hallucinated", "company": "KOMSA", "sources": ["KOMSA 제안서(2026-05): 총 266억 규모 DID/VC 검증체계"], "brief": "KOMSA는 2024년 매출 5조원의 대기업입니다.\n\n• 직원 1만명 [출처: 없음]", "expected_grounded": false}
```

- [ ] **Step 2: eval 하네스 작성** — `tests/eval_ontology_grounding.py` (`eval_news_relevance.py` 패턴)

```python
"""온톨로지 합성 grounding eval — 브리핑 주장이 출처에 근거하는지 (LLM-as-judge).

사용:
    .venv/bin/python tests/eval_ontology_grounding.py            # oracle (sanity)
    .venv/bin/python tests/eval_ontology_grounding.py --mode sonnet   # 실 호출(요금)
"""
import argparse, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
_GOLDEN = Path(__file__).parent / "golden" / "ontology_grounding.jsonl"
_SONNET = "claude-sonnet-4-5"


def load_golden() -> list[dict]:
    return [json.loads(l) for l in _GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]


def judge_oracle(row: dict) -> bool:
    return row["expected_grounded"]


def judge_sonnet(row: dict) -> bool:
    import anthropic
    c = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = (f"출처:\n{chr(10).join(row['sources'])}\n\n브리핑:\n{row['brief']}\n\n"
              "브리핑의 모든 사실이 출처에 근거하면 GROUNDED, 출처에 없는 주장(환각)이 "
              "하나라도 있으면 HALLUCINATED만 출력.")
    r = c.messages.create(model=_SONNET, max_tokens=10,
                          messages=[{"role": "user", "content": prompt}])
    return "GROUNDED" in r.content[0].text.upper()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["oracle", "sonnet"], default="oracle")
    args = ap.parse_args()
    judge = {"oracle": judge_oracle, "sonnet": judge_sonnet}[args.mode]
    rows = load_golden()
    correct = sum(1 for r in rows if judge(r) == r["expected_grounded"])
    acc = correct / len(rows) if rows else 0.0
    print(f"\n=== ontology grounding eval (mode={args.mode}, n={len(rows)}) ===")
    print(f"accuracy: {acc:.3f}")
    threshold = 1.0 if args.mode == "oracle" else 0.5
    if acc < threshold:
        print(f"FAIL: {acc:.3f} < {threshold}"); return 1
    print(f"PASS: {acc:.3f} >= {threshold}"); return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: oracle eval 통과 확인**

Run: `.venv/bin/python tests/eval_ontology_grounding.py`
Expected: `PASS: 1.000 >= 1.0`

- [ ] **Step 4: 구현 — `CLAUDE.md` "온톨로지(lib-mesh) 연동" 절에 추가**

`agents/before.py` 불릿 다음에:
```markdown
딥 리서치(온디맨드 "{업체} 리서치", 게이팅 사용자): `before.deep_company_ontology` → `tools/ontology.company_research_sources`(cluster→R1 업체연결 필터→상위문서 `document_fetch`) → `agents/ontology_synth.synthesize_company_brief`(Sonnet 출처기반 합성 R2 + Haiku grounding critic R3). 렌더 전용·위키 미저장(오염 방지), 실패 시 라이트 cluster 폴백. 품질 회귀: `tests/eval_ontology_grounding.py`(golden `tests/golden/ontology_grounding.jsonl`).
```

- [ ] **Step 5: 최종 전체 회귀 + 커밋**

```bash
.venv/bin/python -m pytest tests/ -q
git add tests/eval_ontology_grounding.py tests/golden/ontology_grounding.jsonl CLAUDE.md
git commit -m "feat(ontology): grounding golden eval(개발타임) + 문서"
```

---

## 완료 기준 (DoD)

- `document_fetch`로 문서 본문(요약) 수집, `company_research_sources`가 R1 필터+상위문서 본문 묶음 반환.
- `ontology_synth.synthesize_company_brief`가 출처기반 합성(Sonnet) + grounding critic(Haiku), 실패 시 폴백.
- 게이팅 사용자의 "업체 리서치"가 딥 브리핑 렌더, 미게이팅/실패는 라이트/기존 동작.
- 위키 미저장(오염 방지). grounding golden eval oracle 통과.
- `pytest tests/ -q` 전체 통과.

## 다음 증분 (범위 밖)

- 브리핑 티어(제목→초점 `document_search` + person_context + 라이트 합성).
- golden 케이스 확장 + sonnet 모드 정기 측정.
