# 출력 포맷 하네스(결정론적 표준화) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 온톨로지 렌더 정리(한국어 라벨·노이즈필터·문서링크) + 담당자 이름 해석 통일 + mrkdwn 볼드 정규화 — 결정론적 포맷 유틸로 카탈로그 높음3 해소.

**Architecture:** `tools/slack_tools.py`에 순수 포맷 유틸(관계 라벨·노이즈·문서라벨·mrkdwn)을 두고 온톨로지 렌더 2곳에 적용. `tools/ontology._normalize_cluster`는 hop 보존 + 번호섹션 노이즈 1차 필터(인물 블록 동시 정리). `agents/before.py` 담당자 루프는 헤더와 동일 리졸버로 표시명 통일(검색키 분리). `after.py`·`trello_report.py`·`trello.py` 사용자 노출 문자열에 `to_slack_mrkdwn` 적용.

**Tech Stack:** Python, pytest. 순수함수(LLM·IO 없음). 선행: 배포된 `tools/ontology._normalize_cluster`(docs에 uri/space/ym/matched 포함), `tools/slack_tools` 빌더.

**선행 스펙:** `docs/superpowers/specs/2026-06-23-output-format-harness-design.md`

---

### Task 1: 포맷 유틸 (slack_tools)

**Files:**
- Modify: `tools/slack_tools.py` (모듈 상단 유틸 영역)
- Test: `tests/test_format_utils.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_format_utils.py`

```python
"""tools/slack_tools.py — 출력 포맷 유틸"""
import tools.slack_tools as st


class TestRelationLabel:
    def test_known(self):
        assert st._relation_label("part-of") == "소속"
        assert st._relation_label("related-to") == "관련"
        assert st._relation_label("uses") == "활용"
        assert st._relation_label("instance-of") == "유형"
    def test_unknown_passthrough(self):
        assert st._relation_label("custom-rel") == "custom-rel"


class TestNoiseRelation:
    def test_numbered_section_is_noise(self):
        assert st._is_noise_relation("01. Cluster 구성하기") is True
        assert st._is_noise_relation("0102. PrivateKey 관리") is True
        assert st._is_noise_relation("  02 백엔드") is True
    def test_normal_not_noise(self):
        assert st._is_noise_relation("KISA 공공과제") is False
        assert st._is_noise_relation("InfraTeam") is False


class TestDocLabel:
    def test_strips_ext(self):
        assert st._doc_label("발표자료_KOMSA.pdf") == "발표자료_KOMSA"
        assert st._doc_label("점검.xlsx") == "점검"
        assert st._doc_label("2026-04-02 인프라기술실") == "2026-04-02 인프라기술실"


class TestToSlackMrkdwn:
    def test_double_to_single(self):
        assert st.to_slack_mrkdwn("**굵게**") == "*굵게*"
        assert st.to_slack_mrkdwn("a **b** c **d**") == "a *b* c *d*"
    def test_single_preserved(self):
        assert st.to_slack_mrkdwn("*이미*") == "*이미*"
    def test_none_safe(self):
        assert st.to_slack_mrkdwn(None) is None
        assert st.to_slack_mrkdwn("") == ""
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_format_utils.py -v`
Expected: FAIL — `AttributeError: module 'tools.slack_tools' has no attribute '_relation_label'`

- [ ] **Step 3: 구현** — `tools/slack_tools.py` 상단(기존 `_strip_display_markdown` 근처)에 추가

```python
_KO_RELATION = {
    "part-of": "소속", "related-to": "관련", "uses": "활용",
    "depends-on": "의존", "implements": "구현", "instance-of": "유형",
    "alias-of": "별칭", "mentioned": "언급", "supersedes": "대체",
}
_NOISE_RE = re.compile(r"^\s*\d{1,4}[.\s]")
_DOC_EXT_RE = re.compile(r"\.(pdf|pptx?|xlsx?|docx?|md|csv|txt)$", re.IGNORECASE)


def _relation_label(rel: str) -> str:
    """온톨로지 관계타입 영어→한국어. 미매핑은 원문 유지."""
    return _KO_RELATION.get((rel or "").strip().lower(), rel or "")


def _is_noise_relation(title: str) -> bool:
    """번호섹션 제목(01. …, 0102. …)은 그래프 노이즈로 렌더 제외."""
    return bool(_NOISE_RE.match(title or ""))


def _doc_label(title: str) -> str:
    """문서 제목 표시용 — 끝 확장자만 제거(과한 정리 금지)."""
    return _DOC_EXT_RE.sub("", (title or "").strip())


def to_slack_mrkdwn(text):
    """Markdown 볼드(**x**)를 Slack mrkdwn(*x*)로. None/빈값 안전, 단일 *는 보존."""
    if not text:
        return text
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
```

(참고: `import re`는 이미 파일 상단에 있음.)

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_format_utils.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add tools/slack_tools.py tests/test_format_utils.py
git commit -m "feat(format): slack_tools 포맷 유틸 (관계라벨·노이즈·문서라벨·mrkdwn)"
```

---

### Task 2: 온톨로지 렌더에 유틸 적용 (slack_tools)

**Files:**
- Modify: `tools/slack_tools.py` (`build_company_research_block` ~276-282, `build_context_block` ~380-387)
- Test: `tests/test_ontology_render.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_render.py`

```python
"""온톨로지 섹션 렌더 — 한국어 라벨·노이즈 필터·문서 링크"""
from tools.slack_tools import build_company_research_block, build_context_block


def _onto():
    return {"relations": [
                {"relation": "related-to", "title": "KISA 공공과제"},
                {"relation": "instance-of", "title": "01. Cluster 구성하기"},  # 노이즈
            ],
            "documents": [
                {"title": "발표자료_KOMSA.pdf", "uri": "https://drive/x"},
                {"title": "회의록", "uri": ""},
            ]}


def test_research_block_korean_label_noise_link():
    text = build_company_research_block("KOMSA", [], [], [], None, None, "", "",
                                        ontology=_onto())[0]["text"]["text"]
    assert "관련: KISA 공공과제" in text          # 한국어 라벨
    assert "01. Cluster" not in text              # 노이즈 제외
    assert "<https://drive/x|발표자료_KOMSA>" in text  # 링크 + 확장자 제거
    assert "• 문서: 회의록" in text               # uri 없으면 평문


def test_context_block_same_rules():
    text = build_context_block({"trello": [], "emails": [], "minutes": [],
                                "ontology": _onto()})[0]["text"]["text"]
    assert "관련: KISA 공공과제" in text
    assert "01. Cluster" not in text
    assert "<https://drive/x|발표자료_KOMSA>" in text
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_render.py -v`
Expected: FAIL — `assert '관련: KISA 공공과제' in text`(현재 `related-to: …` 영어)

- [ ] **Step 3a: 구현 — `build_company_research_block`의 `elif ontology …` 블록**

기존:
```python
    elif ontology and (ontology.get("relations") or ontology.get("documents")):
        lines.append("")
        lines.append("🧠  *온톨로지(사내 지식)*")
        for r in (ontology.get("relations") or [])[:6]:
            lines.append(f"• {r.get('relation')}: {r.get('title')}")
        for d in (ontology.get("documents") or [])[:5]:
            lines.append(f"• 문서: {d.get('title')}")
```
교체:
```python
    elif ontology and (ontology.get("relations") or ontology.get("documents")):
        lines.append("")
        lines.append("🧠  *온톨로지(사내 지식)*")
        _shown = 0
        for r in (ontology.get("relations") or []):
            title = r.get("title", "")
            if _is_noise_relation(title):
                continue
            lines.append(f"• {_relation_label(r.get('relation'))}: {title}")
            _shown += 1
            if _shown >= 6:
                break
        for d in (ontology.get("documents") or [])[:5]:
            label = _doc_label(d.get("title", ""))
            uri = d.get("uri")
            lines.append(f"• 문서: <{uri}|{label}>" if uri else f"• 문서: {label}")
```

- [ ] **Step 3b: 구현 — `build_context_block`의 온톨로지 블록**

기존:
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
교체:
```python
    onto = context.get("ontology")
    if onto and (onto.get("relations") or onto.get("documents")):
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

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_render.py tests/test_company_research_ontology.py tests/test_ontology_briefing.py -v`
Expected: PASS (기존 온톨로지 렌더 테스트 포함 — 한국어 라벨로 단언이 바뀐 신규만 새로 통과)

- [ ] **Step 5: 커밋**

```bash
git add tools/slack_tools.py tests/test_ontology_render.py
git commit -m "feat(format): 온톨로지 렌더 한국어 라벨·노이즈 필터·문서 링크"
```

---

### Task 3: `_normalize_cluster` hop 보존 + 노이즈 1차 필터 (ontology)

**Files:**
- Modify: `tools/ontology.py` (`_normalize_cluster` ~88-109)
- Test: `tests/test_ontology_client.py` (기존 파일에 추가)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_ontology_client.py`에 추가

```python
class TestNormalizeNoiseFilter:
    def test_drops_numbered_section_and_keeps_hop(self):
        cluster = {"seed": "entity/komsa", "entities": [
            {"slug": "entity/kca", "via": "related-to", "title": "KISA 공공과제", "hop": 1},
            {"slug": "entity/n", "via": "instance-of", "title": "01. Cluster 구성하기", "hop": 2},
        ], "documents": []}
        out = ont._normalize_cluster(cluster, "entity/komsa")
        titles = [r["title"] for r in out["relations"]]
        assert "KISA 공공과제" in titles
        assert "01. Cluster 구성하기" not in titles   # 번호섹션 노이즈 제거
        assert out["relations"][0]["hop"] == 1         # hop 보존
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_client.py::TestNormalizeNoiseFilter -v`
Expected: FAIL — `01. Cluster 구성하기`가 relations에 포함됨 / `KeyError: 'hop'`

- [ ] **Step 3: 구현** — `tools/ontology.py`

(a) 모듈 상단(`_recent_range` 근처)에 노이즈 정규식 + 헬퍼:
```python
_NOISE_TITLE_RE = re.compile(r"^\s*\d{1,4}[.\s]")


def _is_noise_entity(title: str) -> bool:
    """번호섹션 엔티티(01. …)는 그래프 노이즈."""
    return bool(_NOISE_TITLE_RE.match(title or ""))
```

(b) `_normalize_cluster`의 relations 루프 교체:
```python
    relations = []
    for e in ents:
        via = e.get("via")
        title = e.get("title") or e.get("slug")
        if via and e.get("slug") != slug and not _is_noise_entity(title):
            relations.append({"relation": via, "title": title, "hop": e.get("hop", 1)})
    relations.sort(key=lambda r: r.get("hop", 1))  # 가까운 관계 우선
```

(참고: `import re`는 `tools/ontology.py` 상단에 이미 있음.)

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_ontology_client.py tests/test_ontology_sources.py tests/test_ontology_person.py -v`
Expected: PASS (relations 키 `relation`·`title` 유지 + `hop` 추가, person_context 회귀 포함)

- [ ] **Step 5: 커밋**

```bash
git add tools/ontology.py tests/test_ontology_client.py
git commit -m "feat(format): _normalize_cluster hop 보존 + 번호섹션 노이즈 필터"
```

---

### Task 4: 담당자 이름 해석 통일 (before)

**Files:**
- Modify: `agents/before.py` (`_run_briefing_research` person 루프 ~1525-1545)
- Test: `tests/test_person_name_resolve.py` (신규)

> 헤더 👥는 `_resolve_attendee_names`(displayName→Slack→Contacts→전체이메일)를 쓰는데 담당자 블록만 `email.split("@")[0]`(→`min`). 표시명을 같은 리졸버로, 검색키는 분리.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_person_name_resolve.py`

```python
"""담당자 표시명/검색키 분리 헬퍼"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import agents.before as before


def test_external_persons_display_and_search(monkeypatch):
    # displayName 없는 외부 참석자 → 표시명은 전체 이메일(localpart 아님), 검색키는 localpart
    monkeypatch.setattr(before, "_resolve_attendee_names",
                        lambda atts, uid, sc: [a.get("name") or a.get("email", "") for a in atts])
    attendees = [
        {"email": "min@icon.foundation"},                 # 외부, 이름 없음
        {"email": "kim@parametacorp.com", "name": "김파"},  # 사내 → 제외
        {"email": "park@kakao.com", "name": "박카카오"},     # 외부, 이름 있음
    ]
    persons = before._build_person_targets(attendees, "U1", None)
    names = [p["name"] for p in persons]
    searches = [p["search"] for p in persons]
    assert "min@icon.foundation" in names           # 표시명 = 전체 이메일
    assert "min" not in names                        # localpart 아님
    assert "박카카오" in names
    assert "김파" not in names                        # 사내 제외
    assert "min" in searches                          # 검색키 = localpart
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_person_name_resolve.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_build_person_targets'`

- [ ] **Step 3a: 구현 — `agents/before.py`에 헬퍼 추가** (`_resolve_attendee_names` 정의 다음)

```python
def _build_person_targets(attendees: list[dict], user_id: str, slack_client) -> list[dict]:
    """외부 참석자(사내 도메인 제외)의 표시명/검색키 분리.
    표시명: 헤더와 동일 리졸버(displayName→Slack→Contacts→전체이메일).
    검색키: research_person 인자용(이름 또는 이메일 localpart)."""
    internal = _internal_domains_set() if "_internal_domains_set" in globals() else set(
        os.getenv("INTERNAL_DOMAINS", "parametacorp.com,iconloop.com").split(","))
    external = [a for a in attendees
                if a.get("email", "").split("@")[-1] not in internal]
    display_names = _resolve_attendee_names(external, user_id, slack_client)
    targets = []
    for a, disp in zip(external, display_names):
        search = a.get("name") or a.get("email", "").split("@")[0]
        targets.append({"name": disp, "search": search})
    return targets
```

- [ ] **Step 3b: 구현 — person 루프 교체** (`agents/before.py` `_run_briefing_research`)

기존:
```python
        _internal_domains = set(
            os.getenv("INTERNAL_DOMAINS", "parametacorp.com,iconloop.com").split(","))
        person_names = [a.get("name") or a.get("email", "").split("@")[0]
                        for a in meeting.get("attendees", [])
                        if a.get("email", "").split("@")[-1] not in _internal_domains]
        persons_info: list[dict] = []
        for name in person_names[:3]:
            progress_resp = _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text=f"👤 *{name}* 인물 리서치 중...")
            progress_ts = progress_resp.get("ts") if progress_resp else None
            try:
                info, _ = research_person(user_id, name, company_name)
            except Exception:
                info = ""
            persons_info.append({"name": name, "raw": info,
                                 "meetings": _person_meetings(user_id, name)})
```
교체:
```python
        targets = _build_person_targets(meeting.get("attendees", []), user_id, slack_client)
        persons_info: list[dict] = []
        for t in targets[:3]:
            disp, search = t["name"], t["search"]
            progress_resp = _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text=f"👤 *{disp}* 인물 리서치 중...")
            progress_ts = progress_resp.get("ts") if progress_resp else None
            try:
                info, _ = research_person(user_id, search, company_name)
            except Exception:
                info = ""
            persons_info.append({"name": disp, "raw": info,
                                 "meetings": _person_meetings(user_id, search)})
```

(참고: 이후 `build_persons_block([{"name": p["name"], "meetings": ...} for p in persons_info])` 는 이미 `p["name"]`(=표시명) 사용하므로 불변.)

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_person_name_resolve.py tests/ -q`
Expected: PASS (신규 + 전체 회귀)

- [ ] **Step 5: 커밋**

```bash
git add agents/before.py tests/test_person_name_resolve.py
git commit -m "feat(format): 담당자 이름 해석 통일(표시명 리졸버+검색키 분리)"
```

---

### Task 5: mrkdwn 볼드 정규화 적용 (after·trello_report·trello)

**Files:**
- Modify: `agents/after.py`, `agents/trello_report.py`, `tools/trello.py`
- Test: `tests/test_mrkdwn_apply.py` (신규)

> `to_slack_mrkdwn`(Task1)을 사용자 노출 문자열 생성부에 적용. 광범위 치환 위험을 줄이려 **발송 텍스트를 만드는 함수의 반환 직전**에만 적용한다.

- [ ] **Step 1: 대상 식별** — 각 파일에서 `**`를 포함해 사용자에게 보내는 문자열을 만드는 함수를 grep으로 확인

Run: `grep -n '\*\*' agents/after.py agents/trello_report.py tools/trello.py`
대상: 결과로 나온 각 문자열의 **최종 조립 지점**(예: Slack 발송 text/blocks에 들어가는 mrkdwn 문자열).

- [ ] **Step 2: 실패 테스트 작성** — `tests/test_mrkdwn_apply.py`

```python
"""mrkdwn 정규화 적용 — 대표 경로에 ** 미잔존"""
import tools.slack_tools as st


def test_util_contract():
    # 적용 함수가 ** 를 남기지 않음(유틸 계약 — 호출부는 이 유틸로 정규화)
    samples = ["**카드**: 내용", "정상 *볼드*", "**A** 그리고 **B**"]
    for s in samples:
        out = st.to_slack_mrkdwn(s)
        assert "**" not in out
```

(주: 호출부별 단위테스트는 함수 시그니처에 의존하므로, 본 태스크는 유틸 계약 테스트 + 호출부 적용 후 전체 회귀로 검증.)

- [ ] **Step 3: 구현 — 각 파일 사용자 노출 문자열 정규화**

- `agents/after.py`: Step 1에서 식별된, Slack으로 보내는 mrkdwn 텍스트(예: 액션아이템 안내·담당자 DM 본문)를 조립한 직후 `from tools.slack_tools import to_slack_mrkdwn` 후 `text = to_slack_mrkdwn(text)`로 감싼다. 내부 로깅/Drive 저장용 문자열은 건드리지 않음.
- `agents/trello_report.py`: Slack 발송용 요약 mrkdwn(`_summarize_*` 결과를 Slack에 싣는 지점)에 `to_slack_mrkdwn` 적용. **Google Docs용 Markdown(상세본)은 제외**(Docs는 `**` 정식).
- `tools/trello.py`: 사용자에게 반환되는 안내/요약 문자열에 적용(카드 설명 원본 등 Trello에 쓰는 값은 제외).

각 적용 지점에 한국어 주석 `# Slack mrkdwn 정규화(** → *)` 추가.

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_mrkdwn_apply.py tests/ -q`
Expected: PASS (전체 회귀 — 기존 Trello/액션 테스트가 `**`를 단언하지 않는지 확인; 단언하면 그 테스트도 `*`로 갱신)

- [ ] **Step 5: 커밋**

```bash
git add agents/after.py agents/trello_report.py tools/trello.py tests/test_mrkdwn_apply.py
git commit -m "feat(format): Slack 발송 문자열 mrkdwn 볼드(**→*) 정규화"
```

---

### Task 6: 문서 + 최종 회귀

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: 구현 — `CLAUDE.md` "코드 규칙" 또는 "메시지 관측" 인근에 한 줄 추가**

```markdown
- Slack 발송 텍스트는 `tools/slack_tools.to_slack_mrkdwn()`로 `**볼드**`→`*볼드*` 정규화. 온톨로지 관계타입은 `_relation_label()`(한국어), 번호섹션 엔티티는 `_is_noise_relation()`로 렌더 제외, 문서는 uri 있으면 `<uri|제목>` 링크. 담당자/참석자 이름은 `_resolve_attendee_names` 리졸버 통일(폴백=전체 이메일, localpart 금지).
```

- [ ] **Step 2: 최종 전체 회귀**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (전체)

- [ ] **Step 3: 커밋**

```bash
git add CLAUDE.md
git commit -m "docs: 출력 포맷 하네스 규칙 반영"
```

---

## 완료 기준 (DoD)

- 온톨로지 섹션(브리핑·리서치)이 한국어 관계 라벨 + 노이즈 제외 + 문서 클릭 링크로 렌더.
- `_normalize_cluster`가 hop 보존·번호섹션 노이즈 필터(인물 블록 동시 정리), 기존 키 유지.
- 담당자 블록 이름이 헤더와 동일 리졸버(폴백=전체 이메일), `• min` 류 localpart 소멸.
- Slack 발송 mrkdwn `**`→`*` 정규화(Docs용 Markdown 제외).
- `pytest tests/ -q` 전체 통과.

## 다음 증분 (범위 밖)

- 브리핑 온톨로지 LLM 프로즈(브리핑 티어). 회의록 검색/목록 포맷 통합. 에러 톤 4단계 템플릿. 진행메시지·D-day 공유 유틸. 사내 도메인 이름 라벨링.
