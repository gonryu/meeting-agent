# 온톨로지 인물 미팅이력 — 브리핑 ③담당자 확장 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 브리핑 ③담당자 블록에 외부 참석자의 사내 미팅이력("언제 함께 미팅했나")을 온톨로지에서 가져와 표시한다.

**Architecture:** `tools/ontology.py`에 `person_context(user_id, name)` 추가(`entity_find`→`entity_cluster`로 미팅성 엔티티 추출). `agents/before.py`의 기존 인물 루프(이미 `INTERNAL_DOMAINS` 제외 = @parametacorp.com 자동 제외)에 게이팅된 주입. `tools/slack_tools.build_persons_block`이 `meetings`를 렌더. 미등록/만료/실패 시 기존 동작.

**Tech Stack:** Python, httpx(MockTransport 테스트), pytest. 선행: 배포된 `tools/ontology.py`(`company_context`/`OntologyClient`/`_best_slug`), 게이팅 `agents.before._ontology_enabled`.

**제약(사용자 결정):** **@parametacorp.com 등 사내 도메인 직원은 인물 컨텍스트 미생성.** 기존 인물 루프가 `_internal_domains`로 외부 참석자만 순회하므로, 그 루프 안에서만 호출해 충족한다(코드로 도메인 재검사 불필요).

---

### Task 1: `person_context()` + 미팅 판별 헬퍼 (ontology.py)

**Files:**
- Modify: `tools/ontology.py` (`company_context` 다음, 파일 끝)
- Test: `tests/test_ontology_person.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_person.py`

```python
"""tools/ontology.py — person_context (인물 미팅이력) 테스트"""
import os
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import tools.ontology as ont


class TestIsMeetingTitle:
    def test_keyword(self):
        assert ont._is_meeting_title("2024-08-02 KISA 월간업무보고 회의") is True
        assert ont._is_meeting_title("komsa 간담회") is True
        assert ont._is_meeting_title("12-06 Interview (w. X)") is True

    def test_date_only(self):
        assert ont._is_meeting_title("20190109 내부") is True

    def test_non_meeting(self):
        assert ont._is_meeting_title("Brand 파트") is False
        assert ont._is_meeting_title("lib-mesh") is False


class TestPersonContext:
    def test_returns_meetings(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")

        class FakeClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def call_tool(self, name, args):
                if name == "entity_find":
                    return {"matches": [{"slug": "entity/ryu", "match_kind": "exact",
                                         "confidence": 0.95, "sources_count": 118}]}
                return {"seed": "entity/ryu", "entities": [
                    {"slug": "entity/m1", "via": "part-of", "title": "2024-08-02 KISA 월간업무보고 회의"},
                    {"slug": "entity/x", "via": "part-of", "title": "Brand 파트"},
                ]}

        monkeypatch.setattr(ont, "OntologyClient", FakeClient)
        out = ont.person_context("U1", "류혁곤")
        assert out["seed"] == "entity/ryu"
        assert out["sources_count"] == 118
        assert "2024-08-02 KISA 월간업무보고 회의" in out["meetings"]
        assert "Brand 파트" not in out["meetings"]   # 미팅 아님 제외

    def test_no_token_none(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: None)
        assert ont.person_context("U1", "류혁곤") is None

    def test_no_match_empty(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")

        class FakeClient:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def call_tool(self, name, args): return {"matches": []}

        monkeypatch.setattr(ont, "OntologyClient", FakeClient)
        out = ont.person_context("U1", "없는사람")
        assert out["seed"] is None and out["meetings"] == []
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_person.py -v`
Expected: FAIL — `AttributeError: module 'tools.ontology' has no attribute '_is_meeting_title'`

- [ ] **Step 3: 구현** — `tools/ontology.py` 끝에 추가

```python
_MEETING_RE = re.compile(r"(회의|미팅|interview|회의록|월간업무보고|간담회|워크숍|workshop)", re.I)
_MEETING_DATE_RE = re.compile(r"\d{4}[-.\s]?\d{1,2}")


def _is_meeting_title(title: str) -> bool:
    """엔티티 제목이 '미팅/회의'성인지 — 키워드 또는 날짜 패턴."""
    t = title or ""
    return bool(_MEETING_RE.search(t) or _MEETING_DATE_RE.search(t))


def person_context(user_id: str, person_name: str) -> dict | None:
    """인물명 → entity_find → entity_cluster → 미팅이력 추출.
    토큰 없으면 None. OntologyAuthError는 호출부로 올림. seed 없으면 빈 구조.
    Returns: {seed, meetings[], sources_count}"""
    token = user_store.get_ontology_token(user_id)
    if not token:
        return None
    with OntologyClient(token) as oc:
        find = oc.call_tool("entity_find", {"name": person_name, "limit": 3})
        slug = _best_slug(find)
        if not slug:
            return {"seed": None, "meetings": [], "sources_count": 0}
        sources = 0
        for m in (find or {}).get("matches", []):
            if m.get("slug") == slug:
                sources = m.get("sources_count", 0)
                break
        cluster = oc.call_tool("entity_cluster", {
            "seed": slug, "depth": 1, "include_documents": False, "limit_entities": 60})
        ents = cluster.get("entities", []) if isinstance(cluster, dict) else []
        meetings = [e.get("title") for e in ents
                    if e.get("via") and _is_meeting_title(e.get("title", ""))]
        return {"seed": slug, "meetings": meetings[:6], "sources_count": sources}
```

(참고: `re`, `user_store`, `OntologyClient`, `_best_slug`는 파일 상단/앞에서 이미 정의됨.)

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_person.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add tools/ontology.py tests/test_ontology_person.py
git commit -m "feat(ontology): person_context — 인물 미팅이력 추출"
```

---

### Task 2: `build_persons_block`에 미팅이력 렌더

**Files:**
- Modify: `tools/slack_tools.py` (`build_persons_block` ~288-308)
- Test: `tests/test_persons_block.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_persons_block.py`

```python
"""tools/slack_tools.py — build_persons_block 미팅이력 렌더 테스트"""
from tools.slack_tools import build_persons_block


def test_renders_meetings():
    blocks = build_persons_block([
        {"name": "박종도", "role": "대리",
         "meetings": ["2024-08-02 KISA 월간업무보고 회의", "2024-01-18 정기 미팅"]}])
    text = blocks[0]["text"]["text"]
    assert "박종도" in text and "대리" in text
    assert "2024-08-02 KISA 월간업무보고 회의" in text
    assert "함께한 미팅" in text


def test_no_meetings_section_when_absent():
    text = build_persons_block([{"name": "김외부"}])[0]["text"]["text"]
    assert "김외부" in text and "함께한 미팅" not in text


def test_empty_list_returns_empty():
    assert build_persons_block([]) == []
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_persons_block.py -v`
Expected: FAIL — `assert '함께한 미팅' in text` (현재 meetings 미렌더)

- [ ] **Step 3: 구현** — `tools/slack_tools.py` `build_persons_block`의 `memo` 렌더 블록 다음(같은 for 루프 안, `lines.append(line)` 이후)에 추가

기존:
```python
        lines.append(line)
        if memo:
            lines.append(f"  └ 메모: {memo}")
```
교체:
```python
        lines.append(line)
        if memo:
            lines.append(f"  └ 메모: {memo}")
        meetings = p.get("meetings") or []
        if meetings:
            lines.append("  └ 함께한 미팅:")
            for mt in meetings[:5]:
                lines.append(f"      • {mt}")
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_persons_block.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add tools/slack_tools.py tests/test_persons_block.py
git commit -m "feat(ontology): 담당자 블록에 사내 미팅이력 렌더"
```

---

### Task 3: 브리핑 인물 루프에 온톨로지 주입 (게이팅·외부 한정)

**Files:**
- Modify: `agents/before.py` (`_run_briefing_research` 인물 루프 ~1466-1487)
- Test: `tests/test_ontology_person_briefing.py` (신규)

> 외부 한정은 기존 `person_names` 산출(`_internal_domains` 제외)로 이미 보장 — 이 루프 안에서만 호출.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_person_briefing.py`

```python
"""인물 온톨로지 주입 헬퍼 테스트 (게이팅·attach)"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import agents.before as before


class TestPersonMeetingsHelper:
    def test_attaches_when_enabled(self, monkeypatch):
        monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
        import tools.ontology as ont
        monkeypatch.setattr(ont, "person_context",
                            lambda uid, name: {"seed": "entity/x", "meetings": ["2024-01-18 정기 미팅"], "sources_count": 5})
        out = before._person_meetings("U1", "박종도")
        assert out == ["2024-01-18 정기 미팅"]

    def test_empty_when_disabled(self, monkeypatch):
        monkeypatch.setattr(before, "_ontology_enabled", lambda uid: False)
        assert before._person_meetings("U1", "박종도") == []

    def test_empty_on_error(self, monkeypatch):
        monkeypatch.setattr(before, "_ontology_enabled", lambda uid: True)
        import tools.ontology as ont
        def boom(uid, name): raise RuntimeError("net down")
        monkeypatch.setattr(ont, "person_context", boom)
        assert before._person_meetings("U1", "박종도") == []
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_person_briefing.py -v`
Expected: FAIL — `AttributeError: module 'agents.before' has no attribute '_person_meetings'`

- [ ] **Step 3a: 구현 — `agents/before.py`에 헬퍼 추가** (`_ontology_enabled` 함수 정의 다음)

```python
def _person_meetings(user_id: str, person_name: str) -> list:
    """게이팅된 사용자에 한해 온톨로지에서 인물 미팅이력 반환. 실패/비활성 시 []."""
    if not _ontology_enabled(user_id):
        return []
    try:
        from tools import ontology
        pc = ontology.person_context(user_id, person_name)
        return (pc or {}).get("meetings", []) or []
    except Exception as pe:
        log.warning(f"온톨로지 인물 조회 실패({person_name}): {pe}")
        return []
```

- [ ] **Step 3b: 구현 — 인물 루프에서 attach** (`agents/before.py` `_run_briefing_research`)

기존:
```python
            persons_info.append({"name": name, "raw": info})
```
교체:
```python
            persons_info.append({"name": name, "raw": info,
                                 "meetings": _person_meetings(user_id, name)})
```

그리고 블록 생성부 — 기존:
```python
            person_blocks = build_persons_block([{"name": p["name"]} for p in persons_info])
```
교체(미팅이력 전달):
```python
            person_blocks = build_persons_block(
                [{"name": p["name"], "meetings": p.get("meetings", [])} for p in persons_info])
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_person_briefing.py tests/ -q`
Expected: PASS (신규 + 전체 회귀)

- [ ] **Step 5: 커밋**

```bash
git add agents/before.py tests/test_ontology_person_briefing.py
git commit -m "feat(ontology): 브리핑 인물 루프에 게이팅된 미팅이력 주입(외부 한정)"
```

---

### Task 4: 문서 + 최종 회귀

**Files:**
- Modify: `CLAUDE.md` (온톨로지 연동 절)

- [ ] **Step 1: 구현 — `CLAUDE.md` "온톨로지(lib-mesh) 연동" 절의 아키텍처 불릿에 추가**

`agents/before.py` 불릿 끝에 다음 문장 추가:

```markdown
인물 블록(③담당자)은 외부 참석자(사내 도메인 `INTERNAL_DOMAINS` 제외)에 한해 `tools/ontology.person_context`로 "함께한 미팅이력"을 주입한다(`_person_meetings` 게이팅·best-effort).
```

- [ ] **Step 2: 최종 전체 회귀**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 3: 커밋**

```bash
git add CLAUDE.md
git commit -m "docs: 온톨로지 인물 미팅이력(③담당자) 반영"
```

---

## 완료 기준 (Definition of Done)

- `tools/ontology.person_context()`가 인물 미팅이력 반환(미팅성 필터).
- `build_persons_block`이 `meetings`를 "함께한 미팅"으로 렌더.
- 브리핑 인물 루프(외부 참석자 한정·게이팅)에서 미팅이력 주입, 비활성/실패 시 기존 동작.
- @parametacorp.com 등 사내 직원은 인물 컨텍스트 미생성(기존 도메인 제외 로직으로 보장).
- `pytest tests/ -q` 전체 통과.
