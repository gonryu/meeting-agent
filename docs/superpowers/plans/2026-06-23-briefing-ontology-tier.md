# 브리핑 온톨로지 티어(목적인지 라이트 합성) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 브리핑 ④이전맥락의 온톨로지를 원시 cluster → 제목 목적에 맞춘 "최근 상황 2~3줄" 프로즈로(라이트·저비용).

**Architecture:** `tools/ontology.recent_company_docs`(document_search 업체명질의+score순+R1필터, snippet 회수) → `agents/ontology_synth.synthesize_recent_situation`(Haiku 라이트, fetch 없음) → `agents/before.briefing_ontology_summary`(게이팅) → `_run_briefing_research` ④에서 프로즈 우선/구조화 폴백 → `build_context_block` 프로즈 렌더.

**Tech Stack:** Python, pytest(httpx MockTransport·mock LLM). 선행: 배포된 `tools/ontology`(OntologyClient/_best_slug), `agents/ontology_synth`(_claude/_load/_CRITIC_MODEL), `_ontology_enabled`, `build_context_block`.

**선행 스펙:** `docs/superpowers/specs/2026-06-23-briefing-ontology-tier-design.md`

---

### Task 1: `recent_company_docs` (search + R1 필터)

**Files:**
- Modify: `tools/ontology.py`
- Test: `tests/test_ontology_recent.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_recent.py`

```python
"""tools/ontology.recent_company_docs — 검색 + R1 필터"""
import os
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import tools.ontology as ont


class TestRecentCompanyDocs:
    def test_filters_offcompany_keeps_connected_with_query_prepend(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")
        seen = {}

        class FakeClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def call_tool(self, name, args):
                if name == "entity_find":
                    return {"matches": [{"slug": "entity/komsa", "match_kind": "exact", "confidence": 0.95}]}
                seen["query"] = args.get("query"); seen["sort"] = args.get("sort_by")
                return {"results": [
                    {"title": "KISA KOMSA 우선협상", "snippet": "우선협상대상자 선정 완료",
                     "source_uri": "u1", "ym": "2026-06", "min_hop": 0, "matched_via_entities": ["entity/komsa"]},
                    {"title": "타사 마케팅", "snippet": "다른 회사", "source_uri": "u2",
                     "ym": "2026-06", "min_hop": 2, "matched_via_entities": ["entity/other"]},
                    {"title": "빈 스니펫", "snippet": "", "source_uri": "u3",
                     "ym": "2026-06", "min_hop": 0, "matched_via_entities": ["entity/komsa"]},
                ]}

        monkeypatch.setattr(ont, "OntologyClient", FakeClient)
        out = ont.recent_company_docs("U1", "KOMSA", "마케팅 진행협의", max_docs=4)
        titles = [d["title"] for d in out["docs"]]
        assert "KISA KOMSA 우선협상" in titles      # 연결 + 스니펫 있음
        assert "타사 마케팅" not in titles            # R1: 미연결 제거
        assert "빈 스니펫" not in titles              # snippet 없으면 제외
        assert seen["query"].startswith("KOMSA")     # 업체명 prepend
        assert seen["sort"] == "score"               # 관련도순(recent 아님)

    def test_no_token_none(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: None)
        assert ont.recent_company_docs("U1", "KOMSA", "x") is None

    def test_no_slug_empty(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")

        class FC:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def call_tool(self, name, args): return {"matches": []}

        monkeypatch.setattr(ont, "OntologyClient", FC)
        out = ont.recent_company_docs("U1", "없는업체", "x")
        assert out["slug"] is None and out["docs"] == []
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_recent.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'recent_company_docs'`

- [ ] **Step 3: 구현** — `tools/ontology.py` 끝에 추가

```python
def recent_company_docs(user_id: str, company_name: str, focus_query: str,
                        max_docs: int = 4) -> dict | None:
    """브리핑용 — 제목 목적에 맞춘 최근 문서 스니펫. 토큰 없으면 None.
    document_search(업체명 prepend 질의·score순) + R1 필터(업체 직접연결).
    Returns: {slug, docs:[{title, snippet, uri, ym}]} (snippet 있는 것만)."""
    token = user_store.get_ontology_token(user_id)
    if not token:
        return None
    with OntologyClient(token) as oc:
        find = oc.call_tool("entity_find", {"name": company_name, "limit": 5})
        slug = _best_slug(find)
        if not slug:
            return {"slug": None, "docs": []}
        query = f"{company_name} {focus_query}".strip()
        res = oc.call_tool("document_search", {
            "query": query, "seed_entity": slug, "mode": "hybrid",
            "sort_by": "score", "top_k": 8})
    results = res.get("results", []) if isinstance(res, dict) else []
    docs = []
    for r in results:
        connected = r.get("min_hop") == 0 or slug in (r.get("matched_via_entities") or [])
        snippet = (r.get("snippet") or "").strip()
        if not connected or not snippet:
            continue
        docs.append({"title": r.get("title", ""), "snippet": snippet,
                     "uri": r.get("source_uri") or "", "ym": r.get("ym") or ""})
        if len(docs) >= max_docs:
            break
    return {"slug": slug, "docs": docs}
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_recent.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add tools/ontology.py tests/test_ontology_recent.py
git commit -m "feat(briefing): recent_company_docs — 목적인지 검색 + R1 필터"
```

---

### Task 2: 라이트 합성 프롬프트 + `synthesize_recent_situation`

**Files:**
- Create: `prompts/templates/ontology_recent.md`
- Modify: `agents/ontology_synth.py`
- Test: `tests/test_ontology_synth.py` (기존 파일에 추가)

- [ ] **Step 1: 프롬프트 작성** — `prompts/templates/ontology_recent.md`

```markdown
너는 파라메타(우리 회사)의 미팅 브리핑 어시스턴트다. 아래 **사내 문서 스니펫**만 근거로 "{{company}}"의 **최근 상황**을 2~3문장으로 요약한다.

규칙(엄수):
- 제공된 스니펫에 명시된 사실만. 추측·외부지식 금지. 근거 없으면 생략.
- 날짜가 있으면 포함(예: 2026-06 수주 확정). 핵심 진행/이슈 위주, 군더더기 없이.
- 한국어 2~3문장. 불릿 없이 자연스러운 문장.

[문서 스니펫]
{{snippets}}

최근 상황 요약(2~3문장):
```

- [ ] **Step 2: 실패 테스트 작성** — `tests/test_ontology_synth.py`에 추가

```python
class TestRecentSituation:
    def test_haiku_synth_returns_summary_and_links(self, monkeypatch):
        from unittest.mock import MagicMock
        resp = MagicMock(); resp.content = [MagicMock(text="KOMSA는 2026-06 수주 확정. 홍보예산 턴키 협의 중.")]
        calls = []
        def fake(**kw): calls.append(kw["model"]); return resp
        monkeypatch.setattr(synth._claude.messages, "create", fake)
        out = synth.synthesize_recent_situation("KOMSA", {"slug": "entity/komsa", "docs": [
            {"title": "KISA KOMSA", "snippet": "우선협상 선정", "uri": "u1", "ym": "2026-06"}]})
        assert "수주 확정" in out["summary"]
        assert out["docs"][0]["uri"] == "u1"
        assert calls[0].startswith("claude-haiku")   # 라이트=Haiku

    def test_no_docs_returns_none(self):
        assert synth.synthesize_recent_situation("KOMSA", {"slug": "entity/komsa", "docs": []}) is None

    def test_failure_returns_none(self, monkeypatch):
        def boom(**kw): raise RuntimeError("api down")
        monkeypatch.setattr(synth._claude.messages, "create", boom)
        out = synth.synthesize_recent_situation("KOMSA", {"slug": "x", "docs": [
            {"title": "t", "snippet": "s", "uri": "u", "ym": "2026-06"}]})
        assert out is None
```

- [ ] **Step 3: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_synth.py::TestRecentSituation -v`
Expected: FAIL — `AttributeError: ... has no attribute 'synthesize_recent_situation'`

- [ ] **Step 4: 구현** — `agents/ontology_synth.py` 끝에 추가

```python
def _fmt_snippets(docs: list) -> str:
    out = []
    for d in (docs or []):
        ym = f" ({d['ym']})" if d.get("ym") else ""
        out.append(f"- {d.get('title','')}{ym}: {d.get('snippet','')}")
    return "\n".join(out)


def synthesize_recent_situation(company: str, recent: dict) -> dict | None:
    """브리핑 라이트 합성 — Haiku로 스니펫→최근상황 2~3문장 + 문서 링크.
    docs 없으면 None. critic 없음(표면 작음). best-effort → 실패 시 None.
    Returns: {summary: str, docs: [{title, uri}]}"""
    docs = (recent or {}).get("docs") or []
    if not docs:
        return None
    prompt = (_load("ontology_recent.md")
              .replace("{{company}}", company)
              .replace("{{snippets}}", _fmt_snippets(docs)))
    try:
        resp = _claude.messages.create(model=_CRITIC_MODEL, max_tokens=600,
                                       messages=[{"role": "user", "content": prompt}])
        summary = resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"브리핑 온톨로지 라이트 합성 실패({company}): {e}")
        return None
    if not summary:
        return None
    links = [{"title": d.get("title", ""), "uri": d.get("uri", "")}
             for d in docs[:3] if d.get("uri")]
    return {"summary": summary, "docs": links}
```

(참고: `_CRITIC_MODEL`=`claude-haiku-4-5`, `_load`/`_claude`/`log`는 기존 모듈에 있음.)

- [ ] **Step 5: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_synth.py -v`
Expected: PASS (기존 딥 합성 테스트 포함)

- [ ] **Step 6: 커밋**

```bash
git add prompts/templates/ontology_recent.md agents/ontology_synth.py tests/test_ontology_synth.py
git commit -m "feat(briefing): synthesize_recent_situation — Haiku 라이트 합성(최근상황)"
```

---

### Task 3: `briefing_ontology_summary` 묶음 (before)

**Files:**
- Modify: `agents/before.py` (`deep_company_ontology` 다음)
- Test: `tests/test_briefing_ontology.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_briefing_ontology.py`

```python
"""agents/before.briefing_ontology_summary — 게이팅·focus 추출·폴백"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import agents.before as before


def test_returns_summary_when_enabled(monkeypatch):
    monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
    import tools.ontology as ont
    captured = {}
    def fake_recent(uid, company, focus, max_docs=4):
        captured["focus"] = focus
        return {"slug": "entity/komsa", "docs": [{"title": "t", "snippet": "s", "uri": "u", "ym": "2026-06"}]}
    monkeypatch.setattr(ont, "recent_company_docs", fake_recent)
    import agents.ontology_synth as synth
    monkeypatch.setattr(synth, "synthesize_recent_situation",
                        lambda c, r: {"summary": "최근 수주 확정", "docs": [{"title": "t", "uri": "u"}]})
    out = before.briefing_ontology_summary("U1", "KOMSA", "Komsa 마케팅 진행협의_박종도대리")
    assert out["summary"] == "최근 수주 확정"
    assert captured["focus"] == "Komsa 마케팅 진행협의"   # '_' 이후 절단

def test_none_when_disabled(monkeypatch):
    monkeypatch.setattr(before, "_ontology_enabled", lambda uid: False)
    assert before.briefing_ontology_summary("U1", "KOMSA", "제목") is None

def test_none_on_error(monkeypatch):
    monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
    import tools.ontology as ont
    def boom(uid, c, f, max_docs=4): raise RuntimeError("net")
    monkeypatch.setattr(ont, "recent_company_docs", boom)
    assert before.briefing_ontology_summary("U1", "KOMSA", "제목") is None
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_briefing_ontology.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'briefing_ontology_summary'`

- [ ] **Step 3: 구현** — `agents/before.py`의 `deep_company_ontology` 정의 다음에 추가

```python
def briefing_ontology_summary(user_id: str, company_name: str, title: str) -> dict | None:
    """브리핑용 라이트 온톨로지 — 제목 목적 기반 최근상황 프로즈+링크.
    게이팅·실패·미등록 시 None(호출부가 구조화 cluster로 폴백)."""
    if not _ontology_enabled(user_id):
        return None
    try:
        from tools import ontology
        from agents import ontology_synth
        focus = (title or "").split("_")[0].strip()  # "제목_담당자" → "제목"
        recent = ontology.recent_company_docs(user_id, company_name, focus)
        if not recent or not recent.get("docs"):
            return None
        return ontology_synth.synthesize_recent_situation(company_name, recent)
    except Exception as oe:
        from tools import ontology as _ot
        if isinstance(oe, _ot.OntologyAuthError):
            raise  # 만료는 호출부가 재등록 DM 처리
        log.warning(f"브리핑 온톨로지 요약 실패({company_name}): {oe}")
        return None
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_briefing_ontology.py tests/ -q`
Expected: PASS (신규 + 전체 회귀)

- [ ] **Step 5: 커밋**

```bash
git add agents/before.py tests/test_briefing_ontology.py
git commit -m "feat(briefing): briefing_ontology_summary — 게이팅 라이트 묶음"
```

---

### Task 4: 브리핑 ④ 배선 (프로즈 우선 / 구조화 폴백)

**Files:**
- Modify: `agents/before.py` (`_run_briefing_research` ④ 온톨로지 주입 ~1579-1592)
- Test: (배선은 앱 전체 필요 → 전체 회귀 + 구문검사로 검증)

- [ ] **Step 1: 구현** — `_run_briefing_research`의 온톨로지 주입 블록 교체

기존:
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
교체:
```python
        # 온톨로지(사내 지식) 주입 — 게이팅. 프로즈(최근상황) 우선, 실패 시 구조화 cluster 폴백.
        # 만료/실패는 섹션 생략(브리핑 안 깨짐).
        if _ontology_enabled(user_id):
            try:
                from tools import ontology
                recent = briefing_ontology_summary(user_id, company_name, meeting.get("summary", ""))
                if recent and recent.get("summary"):
                    context["ontology_recent"] = recent
                else:
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

- [ ] **Step 2: 구문 검사**

Run: `.venv/bin/python -c "import ast; ast.parse(open('agents/before.py').read()); print('before.py OK')"`
Expected: `before.py OK`

- [ ] **Step 3: 전체 회귀**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 4: 커밋**

```bash
git add agents/before.py
git commit -m "feat(briefing): ④에 온톨로지 프로즈 우선 주입(구조화 폴백)"
```

---

### Task 5: `build_context_block` 프로즈 렌더

**Files:**
- Modify: `tools/slack_tools.py` (`build_context_block` 온톨로지 블록 ~423-435)
- Test: `tests/test_ontology_render.py` (기존 파일에 추가)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_render.py`에 추가

```python
class TestBriefingProseRender:
    def test_prose_rendered_with_links(self):
        from tools.slack_tools import build_context_block
        ctx = {"trello": [], "emails": [], "minutes": [], "ontology_recent": {
            "summary": "KOMSA는 2026-06 수주 확정. 홍보예산 턴키 협의 중.",
            "docs": [{"title": "KISA KOMSA", "uri": "https://x"}]}}
        text = build_context_block(ctx)[0]["text"]["text"]
        assert "온톨로지(사내 지식)" in text
        assert "수주 확정" in text
        assert "<https://x|KISA KOMSA>" in text

    def test_prose_takes_priority_over_structured(self):
        from tools.slack_tools import build_context_block
        ctx = {"trello": [], "emails": [], "minutes": [],
               "ontology_recent": {"summary": "프로즈 요약", "docs": []},
               "ontology": {"relations": [{"relation": "related-to", "title": "KCA"}], "documents": []}}
        text = build_context_block(ctx)[0]["text"]["text"]
        assert "프로즈 요약" in text
        assert "관련: KCA" not in text   # 프로즈 있으면 구조화 미렌더
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_render.py::TestBriefingProseRender -v`
Expected: FAIL — 프로즈 미렌더

- [ ] **Step 3: 구현** — `tools/slack_tools.py` `build_context_block`의 온톨로지 블록 앞에 프로즈 분기 추가

기존(온톨로지 구조화 블록):
```python
    onto = context.get("ontology")
    if onto and (onto.get("relations") or onto.get("documents")):
        lines.append("")
        lines.append("🔗  *온톨로지(사내 지식)*")
        ...
```
앞에 프로즈 분기를 두고 구조화는 `elif`로:
```python
    recent = context.get("ontology_recent")
    onto = context.get("ontology")
    if recent and recent.get("summary"):
        lines.append("")
        lines.append("🔗  *온톨로지(사내 지식)*")
        lines.append(f"   {recent['summary']}")
        for d in (recent.get("docs") or [])[:3]:
            label = _doc_label(d.get("title", ""))
            uri = d.get("uri")
            lines.append(f"   • 문서: <{uri}|{label}>" if uri else f"   • 문서: {label}")
    elif onto and (onto.get("relations") or onto.get("documents")):
        lines.append("")
        lines.append("🔗  *온톨로지(사내 지식)*")
        _shown = 0
        for r in (onto.get("relations") or []):
            title = r.get("title", "")
            if _is_noise_relation(title):
                continue
            lines.append(f"   • {_relation_label(r.get('relation'))}: {title}")
            _shown += 1
            if _shown >= 6:
                break
        for d in (onto.get("documents") or [])[:5]:
            label = _doc_label(d.get("title", ""))
            uri = d.get("uri")
            lines.append(f"   • 문서: <{uri}|{label}>" if uri else f"   • 문서: {label}")
```

(주: 기존 구조화 블록 본문을 `elif`로 옮기되 내용은 #52와 동일하게 유지.)

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_render.py tests/ -q`
Expected: PASS (전체 회귀)

- [ ] **Step 5: 커밋**

```bash
git add tools/slack_tools.py tests/test_ontology_render.py
git commit -m "feat(briefing): build_context_block 온톨로지 프로즈 렌더(구조화 폴백)"
```

---

### Task 6: 문서 + 최종 회귀

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: 구현 — `CLAUDE.md` "온톨로지(lib-mesh) 연동" 절에 추가**

```markdown
브리핑 ④이전맥락은 게이팅 사용자 대상 **목적인지 라이트 합성**(`before.briefing_ontology_summary` → `ontology.recent_company_docs`[제목 목적 `document_search` 업체명질의·score순·R1필터] → `ontology_synth.synthesize_recent_situation`[Haiku, 최근상황 2~3문장]). 프로즈 성공 시 구조화 cluster 대신 프로즈+문서링크 렌더, 실패/미등록 시 구조화(`company_context`) 폴백, ont 다운 시 생략. `document_fetch` 없이 검색 snippet만 사용(저비용). 딥 리서치(research)는 Sonnet+critic+fetch, 브리핑은 Haiku+snippet으로 분리.
```

- [ ] **Step 2: 최종 전체 회귀**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 3: 커밋**

```bash
git add CLAUDE.md
git commit -m "docs: 브리핑 온톨로지 티어(목적인지 라이트 합성) 반영"
```

---

## 완료 기준 (DoD)

- `recent_company_docs`가 업체명질의+score순+R1필터로 연결 문서 스니펫 반환.
- `synthesize_recent_situation`이 Haiku로 최근상황 2~3문장+링크, docs 없으면/실패 None.
- 브리핑 ④가 프로즈 우선·구조화 폴백·만료 재등록 DM·ont다운 생략(3단 폴백).
- `build_context_block`이 프로즈+링크 렌더(프로즈 우선).
- `pytest tests/ -q` 전체 통과.

## 다음 증분 (범위 밖)

- 외부 참석자 "지난 논의 내용" 합성(현재 미팅이력 제목만). 회의록 검색/목록 통합. 에러 톤 템플릿.
