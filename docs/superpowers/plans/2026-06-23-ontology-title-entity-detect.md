# 온톨로지 기반 제목 엔티티 감지(업체추론 폴백) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 업체 추론 체인 마지막에 온톨로지 `entity_find` 폴백을 추가해 KISA·이데일리·과기부 등 미감지 기관을 사실 기반으로 태깅.

**Architecture:** `tools/ontology.detect_company_in_title`(제목 토큰화→entity_find→exact+organization 필터→best). `agents/before.run_briefing`의 추론 체인 4번째 폴백으로 게이팅 배선. 기존 흐름(extendedProperties 저장→research_queue) 재사용.

**Tech Stack:** Python, pytest(httpx MockTransport). 선행: 배포된 `tools/ontology.OntologyClient`, `_ontology_enabled`.

**선행 스펙:** `docs/superpowers/specs/2026-06-23-ontology-title-entity-detect-design.md`

---

### Task 1: `detect_company_in_title` (ontology)

**Files:**
- Modify: `tools/ontology.py`
- Test: `tests/test_ontology_detect.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_detect.py`

```python
"""tools/ontology.detect_company_in_title — 제목 엔티티 감지"""
import os
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import tools.ontology as ont


# entity_find 응답을 토큰별로 흉내내는 Fake
_DB = {
    "KISA": {"slug": "entity/kisa", "title": "KISA", "etype": "organization",
             "match_kind": "exact", "importance": 1.0, "sources_count": 262},
    "과기부": {"slug": "entity/과기부", "title": "과기부", "etype": "organization",
            "match_kind": "exact", "importance": 0.8, "sources_count": 9},
    "이데일리": {"slug": "entity/이데일리", "title": "이데일리", "etype": "organization",
             "match_kind": "exact", "importance": 0.8, "sources_count": 1},
    "이정훈기자님": {"slug": "entity/이정훈", "title": "이정훈", "etype": "person",
               "match_kind": "fuzzy", "importance": 0.6, "sources_count": 3},
    "6ixgo": {"slug": "entity/go", "title": "Go", "etype": "technology",
              "match_kind": "substring", "importance": 1.0, "sources_count": 42},
    "InfraTeam": {"slug": "entity/infrateam", "title": "InfraTeam", "etype": "organization",
                  "match_kind": "exact", "importance": 0.9, "sources_count": 50},
}


def _mk_client(monkeypatch):
    monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def call_tool(self, name, args):
            tok = args.get("name", "")
            m = _DB.get(tok)
            return {"matches": [m]} if m else {"matches": []}

    monkeypatch.setattr(ont, "OntologyClient", FakeClient)


class TestDetect:
    def test_kisa_gwagibu_picks_highest_importance(self, monkeypatch):
        _mk_client(monkeypatch)
        assert ont.detect_company_in_title("U1", "KISA, 과기부 간담회") == "KISA"

    def test_media_org(self, monkeypatch):
        _mk_client(monkeypatch)
        assert ont.detect_company_in_title("U1", "이데일리 이정훈기자님") == "이데일리"

    def test_internal_work_no_clean_match(self, monkeypatch):
        _mk_client(monkeypatch)
        # 6ixgo→Go(substring/technology), MoU/촬영/제작=stopword → None
        assert ont.detect_company_in_title("U1", "6ixgo MoU 촬영 - 백이미지 제작") is None

    def test_own_org_denylisted(self, monkeypatch):
        _mk_client(monkeypatch)
        assert ont.detect_company_in_title("U1", "InfraTeam 회의") is None

    def test_no_token_none(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: None)
        assert ont.detect_company_in_title("U1", "KISA 간담회") is None

    def test_empty_title(self, monkeypatch):
        _mk_client(monkeypatch)
        assert ont.detect_company_in_title("U1", "") is None
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_detect.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'detect_company_in_title'`

- [ ] **Step 3: 구현** — `tools/ontology.py` 끝에 추가

```python
_ORG_ETYPES = {"organization", "company"}
_STOPWORDS = {
    "간담회", "미팅", "회의", "촬영", "제작", "협의", "진행", "후속", "논의",
    "주간", "정기", "mou", "poc", "킥오프", "킥오프미팅", "백이미지", "기자님",
    "대리", "차장", "부장", "팀장", "이사", "대표", "관련", "건", "그룹",
}
_OWN_ORG_DENYLIST = {
    "parametacorp", "parameta", "파라메타", "iconloop", "아이콘루프",
    "infrateam", "enterprise", "icon",
}
_TOKEN_SPLIT_RE = re.compile(r"[\s,/·\-_()\[\]:|]+")


def _title_tokens(title: str) -> list[str]:
    """제목을 후보 토큰으로 분해 — 구두점 분리, 길이<2·스톱워드 제거, 최대 5개."""
    out = []
    for raw in _TOKEN_SPLIT_RE.split(title or ""):
        t = raw.strip()
        if len(t) < 2 or t.lower() in _STOPWORDS:
            continue
        out.append(t)
        if len(out) >= 5:
            break
    return out


def detect_company_in_title(user_id: str, title: str) -> str | None:
    """제목 토큰을 entity_find로 검증해 알려진 조직 엔티티면 그 title 반환.
    채택: match_kind=exact & etype∈조직 & importance>=0.5 & 자사 denylist 아님.
    토큰/매칭 없으면 None. best-effort(예외 시 None)."""
    token = user_store.get_ontology_token(user_id)
    if not token:
        return None
    tokens = _title_tokens(title)
    if not tokens:
        return None
    best = None  # (importance, sources, title)
    try:
        with OntologyClient(token) as oc:
            for tok in tokens:
                res = oc.call_tool("entity_find", {"name": tok, "limit": 2})
                for m in (res or {}).get("matches", []) if isinstance(res, dict) else []:
                    if not isinstance(m, dict):
                        continue
                    if m.get("match_kind") != "exact":
                        continue
                    if (m.get("etype") or "").lower() not in _ORG_ETYPES:
                        continue
                    if (m.get("importance") or 0) < 0.5:
                        continue
                    name = (m.get("title") or "").strip()
                    if not name or name.lower() in _OWN_ORG_DENYLIST:
                        continue
                    key = (m.get("importance") or 0, m.get("sources_count") or 0)
                    if best is None or key > best[0]:
                        best = (key, name)
    except OntologyAuthError:
        return None
    except Exception as e:
        log.warning(f"detect_company_in_title 실패: {e}")
        return None
    return best[1] if best else None
```

(참고: `import re`·`OntologyAuthError`·`user_store`·`OntologyClient`·`log`는 `tools/ontology.py`에 이미 있음.)

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_detect.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add tools/ontology.py tests/test_ontology_detect.py
git commit -m "feat(detect): detect_company_in_title — 온톨로지 entity_find 업체 감지"
```

---

### Task 2: run_briefing 폴백 배선 (게이팅)

**Files:**
- Modify: `agents/before.py` (run_briefing 참석자 역추론 직후 ~1290-1292)
- Test: (배선은 앱 전체 필요 → 구문검사 + 전체 회귀)

- [ ] **Step 1: 구현** — `agents/before.py`의 참석자 역추론 블록 다음(FR-B15 extendedProperties 저장 `# FR-B15:` 주석 줄 앞)에 삽입

기존:
```python
                if inferred:
                    company_names = [inferred]
                    log.info(f"업체명 추론 성공 (참석자): '{meeting.get('summary')}' → {company_names}")

        # FR-B15: 추론 결과를 extendedProperties에 저장 (다음 조회 시 재사용)
```
교체(역추론 블록과 FR-B15 사이에 4번째 폴백 추가):
```python
                if inferred:
                    company_names = [inferred]
                    log.info(f"업체명 추론 성공 (참석자): '{meeting.get('summary')}' → {company_names}")

        # FR-B17: 온톨로지 엔티티 감지 (게이팅) — 위 추론 모두 실패 시 사실 기반 폴백
        if not company_names and _ontology_enabled(user_id):
            try:
                from tools import ontology
                detected = ontology.detect_company_in_title(user_id, meeting.get("summary", ""))
                if detected:
                    company_names = [detected]
                    log.info(f"업체명 추론 성공 (온톨로지): '{meeting.get('summary')}' → {detected}")
            except Exception as oe:
                log.warning(f"온톨로지 제목 감지 실패: {oe}")

        # FR-B15: 추론 결과를 extendedProperties에 저장 (다음 조회 시 재사용)
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
git commit -m "feat(detect): run_briefing에 온톨로지 제목 감지 폴백 배선(게이팅)"
```

---

### Task 3: 문서 + 최종 회귀

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: 구현 — `CLAUDE.md` "브리핑 업체명 추론" 인근에 추가**

```markdown
**업체 추론 폴백 (온톨로지)**: extendedProperties→LLM 제목추론→참석자 역추론이 모두 실패하면, 게이팅 사용자에 한해 `tools/ontology.detect_company_in_title`이 제목 토큰을 `entity_find`로 검증해 **exact + organization** 엔티티(예: KISA·이데일리·과기부)면 관련 업체로 태깅. 6ixgo(내부작업)·기술·인물·자사팀(denylist)은 배제. 결과는 extendedProperties에 캐시.
```

- [ ] **Step 2: 최종 전체 회귀**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 3: 커밋**

```bash
git add CLAUDE.md
git commit -m "docs: 온톨로지 제목 엔티티 감지(업체추론 폴백) 반영"
```

---

## 완료 기준 (DoD)

- `detect_company_in_title`이 exact+organization 매칭만 채택(KISA·이데일리·과기부 감지, 6ixgo·기술·인물·자사팀 배제).
- run_briefing 4번째 폴백이 게이팅 사용자에서만 동작, 실패 시 기존 동작.
- 감지 결과가 extendedProperties 캐시 → 리서치/온톨로지 경로 진입.
- `pytest tests/ -q` 전체 통과.

## 다음 증분 (범위 밖)

- 인물 엔티티 기반 담당자 보강. project etype 채택. 복수 업체 동시 태깅.
