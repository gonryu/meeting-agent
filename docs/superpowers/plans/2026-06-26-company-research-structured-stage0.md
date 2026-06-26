# 회사리서치 구조화 — 스트랭글러 단계 0 (타입+단일파서+직렬화)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development 또는 executing-plans. 체크박스(`- [ ]`).

**Goal:** 손실 왕복을 끊을 **구조화 타입 + 단일 파서 + 단방향 직렬화**를 데드코드로 신설(아무도 안 씀, 동작 불변). 스트랭글러의 안전한 토대.

**Architecture:** `agents/research_types.py` 신규 — `NewsItem`/`CompanyResearch` dataclass, `parse_trend_bullets()`(trend 마크다운 → NewsItem 리스트, **파싱은 여기 한 곳에서만**), `to_markdown()`(객체 → 위키 마크다운, **한 방향**). 기존 extractor가 to_markdown 출력을 그대로 파싱 가능해야(전환기 호환).

**Tech Stack:** Python `@dataclass`(외부 라이브러리 없음), pytest. 선행 설계: `docs/superpowers/specs/2026-06-26-architecture-audit-company-research.md`.

**범위:** 단계 0만. 단계 1(run_company_research가 객체 생성)~5는 후속 증분. 이 PR은 **데드코드 추가뿐 — 기존 동작 0 변경**.

---

### Task 1: 데이터 타입 + 단일 파서

**Files:**
- Create: `agents/research_types.py`
- Test: `tests/test_research_types.py` (신규)

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_research_types.py`

```python
"""회사리서치 구조화 타입 + 단일 파서(스트랭글러 단계0)"""
from agents.research_types import NewsItem, CompanyResearch, parse_trend_bullets


class TestParseTrendBullets:
    def test_title_summary_url_date(self):
        md = ("- **[2026 블록체인 밋업데이(BCMD) 교육생 모집]**: "
              "KISA가 블록체인 인력 양성을 위해 모집한다 (2026.06.23, https://www.kisa.or.kr/k)\n"
              "- **[N2SF 도입 본격화]**: N2SF 공공 확산에 예산 투입 (2026.06.17, https://www.kisa.or.kr/n)")
        items = parse_trend_bullets(md)
        assert len(items) == 2
        a = items[0]
        assert a.title == "2026 블록체인 밋업데이(BCMD) 교육생 모집"
        assert "인력 양성" in a.summary
        assert a.url == "https://www.kisa.or.kr/k"
        assert a.date == "2026.06.23"

    def test_no_info_returns_empty(self):
        assert parse_trend_bullets("- 파라메타 사업 맥락의 최근 공개 정보 없음") == []
        assert parse_trend_bullets("") == []

    def test_bullet_without_url(self):
        items = parse_trend_bullets("- **[제목만]**: 요약 내용")
        assert len(items) == 1 and items[0].url is None and items[0].title == "제목만"

    def test_plain_bullet_no_bold_title(self):
        items = parse_trend_bullets("- 그냥 제목 요약 (https://x.com)")
        assert len(items) == 1 and items[0].url == "https://x.com"
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_research_types.py::TestParseTrendBullets -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agents.research_types'`

- [ ] **Step 3: 구현** — `agents/research_types.py` 생성

```python
"""회사리서치 구조화 타입 + 단일 파서/직렬화 (스트랭글러 단계0).

원칙: 마크다운은 저장·표시 포맷일 뿐 통신 포맷이 아니다. 파싱은 parse_trend_bullets
한 곳에서만(하류 정규식 재파싱 제거 목표), 마크다운 방출은 to_markdown 한 방향.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class NewsItem:
    title: str
    summary: str = ""
    url: str | None = None
    date: str = ""          # 'YYYY.MM.DD' 등 표기 그대로
    relevance: str = ""     # 'high' | 'mid' (단계3 판정에서 채움)
    source: str = ""        # '웹 검색' | '오케스트레이터'


@dataclass
class CompanyResearch:
    company_name: str
    company_type: str = "normal"   # 'normal' | 'media'
    overview: str = ""             # 합성 개요(표시 전용, 재파싱 안 함)
    news: list[NewsItem] = field(default_factory=list)
    connections: list[str] = field(default_factory=list)
    email_context: str = ""        # 기존 '## 이메일 맥락' 본문(전환기엔 문자열 유지)
    trello_context: str = ""       # 기존 '## Trello 맥락' 본문
    parascope: list[str] = field(default_factory=list)
    searched_at: str = ""          # YYYY-MM-DD


# 트렌드 불릿 한 줄 파싱: "- **[제목]**: 요약 (날짜, URL)" 변형 폭넓게 수용
_BOLD_TITLE_RE = re.compile(r"\*\*\[?(.+?)\]?\*\*")
_URL_RE = re.compile(r"https?://[^\s)]+")
_DATE_RE = re.compile(r"\b(\d{4}[.\-]\d{1,2}(?:[.\-]\d{1,2})?)\b")


def parse_trend_bullets(trend_md: str) -> list[NewsItem]:
    """trend 마크다운 불릿 → NewsItem 리스트. '정보 없음'·빈 줄은 제외.
    파싱은 이 함수 한 곳에서만 수행(하류 재파싱 금지)."""
    out: list[NewsItem] = []
    for raw in (trend_md or "").splitlines():
        line = raw.strip()
        if not line.startswith(("- ", "• ")):
            continue
        line = line[2:].strip()
        if not line or "정보 없음" in line:
            continue
        url_m = _URL_RE.search(line)
        url = url_m.group(0) if url_m else None
        date_m = _DATE_RE.search(line)
        date = date_m.group(1) if date_m else ""
        bold = _BOLD_TITLE_RE.search(line)
        if bold:
            title = bold.group(1).strip()
            rest = line[bold.end():]
        else:
            # 볼드 제목 없으면 ':' 또는 URL 앞까지를 제목으로
            head = re.split(r"\(|https?://", line, maxsplit=1)[0]
            title = re.split(r"[:：]", head, maxsplit=1)[0].strip()
            rest = line[len(title):]
        # 요약: 제목 이후 텍스트에서 URL·괄호 메타·마커 제거
        summary = rest
        if url:
            summary = summary.replace(url, "")
        summary = re.sub(r"\(\s*[\d.\-]*\s*,?\s*\)", "", summary)  # 빈/날짜만 괄호
        summary = summary.strip(" :：—-()").strip()
        title = title.strip(" :：[]").strip()
        if title or summary:
            out.append(NewsItem(title=title or summary[:60], summary=summary,
                                 url=url, date=date))
    return out
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_research_types.py::TestParseTrendBullets -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add agents/research_types.py tests/test_research_types.py
git commit -m "feat(research): 구조화 타입 NewsItem/CompanyResearch + 단일 파서 parse_trend_bullets (단계0)"
```

---

### Task 2: 단방향 직렬화 `to_markdown` (+ 기존 추출기 호환)

**Files:**
- Modify: `agents/research_types.py`
- Test: `tests/test_research_types.py`

> **호환 제약(전환기 핵심):** `to_markdown` 출력은 **기존 `_extract_company_content_sections`가 그대로 파싱 가능**해야 한다(단계4 전까지 추출기 폴백이 살아있으므로). 그래서 `### 최근 동향` 하위헤더 + `- **[제목]**: 요약 (URL)` 포맷을 그대로 방출한다.

- [ ] **Step 1: 실패 테스트 작성** — `tests/test_research_types.py`에 추가

```python
import agents.before as before
from agents.research_types import to_markdown


class TestToMarkdown:
    def _research(self):
        return CompanyResearch(
            company_name="KISA", company_type="normal", searched_at="2026-06-25",
            overview="- **산업 위치**: 정보보호 전문기관",
            news=[NewsItem(title="N2SF 도입 본격화", summary="N2SF 공공 확산 예산 투입",
                           url="https://www.kisa.or.kr/n", date="2026.06.17")],
            connections=["loopchain ↔ K-BTF 보안표준"],
            email_context="## 이메일 맥락\n- 2026-06-01 | 협의",
            trello_context="## Trello 맥락\n- 카드: KISA",
        )

    def test_emits_expected_sections(self):
        md = to_markdown(self._research())
        assert "## 최근 동향" in md
        assert "### 최근 동향 (2026-06-25 기준)" in md
        assert "N2SF 도입 본격화" in md and "https://www.kisa.or.kr/n" in md
        assert "## 파라메타 서비스 연결점" in md and "K-BTF 보안표준" in md
        assert "## 이메일 맥락" in md and "## Trello 맥락" in md

    def test_roundtrip_existing_extractor_recovers_news(self):
        # 전환기 호환: to_markdown 출력을 기존 추출기가 파싱해 뉴스를 복원해야 함
        md = "---\ntitle: KISA\n---\n# KISA\n\n" + to_markdown(self._research())
        news_lines, _p, conn, _e, _u = before._extract_company_content_sections(md)
        assert any("N2SF" in n for n in news_lines)
        assert any("K-BTF 보안표준" in c for c in conn)

    def test_no_news_emits_no_info(self):
        r = CompanyResearch(company_name="X", searched_at="2026-06-25")
        md = to_markdown(r)
        assert "최근 공개된 정보 없음" in md
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_research_types.py::TestToMarkdown -v`
Expected: FAIL — `ImportError: cannot import name 'to_markdown'`

- [ ] **Step 3: 구현** — `agents/research_types.py`에 추가

```python
def _news_to_md(items: list[NewsItem]) -> str:
    if not items:
        return "- 최근 공개된 정보 없음"
    lines = []
    for n in items:
        meta = ""
        parts = [p for p in (n.date,) if p]
        if n.url:
            parts.append(n.url)
        if parts:
            meta = " (" + ", ".join(parts) + ")"
        body = f"{n.summary}" if n.summary else ""
        lines.append(f"- **[{n.title}]**: {body}{meta}".rstrip())
    return "\n".join(lines)


def to_markdown(r: CompanyResearch, preserved_sections: str = "",
                sources_log_line: str = "") -> str:
    """CompanyResearch → 위키 마크다운 본문(# 헤더부터). 한 방향 방출.
    기존 _extract_company_content_sections가 파싱 가능한 포맷 유지(전환기 호환).
    frontmatter는 호출부가 별도로 prepend한다."""
    parts = [f"# {r.company_name}\n"]
    parts.append(f"## 최근 동향\n- last_searched: {r.searched_at}\n"
                 f"### 최근 동향 ({r.searched_at} 기준)\n{_news_to_md(r.news)}\n")
    if r.email_context.strip():
        parts.append(r.email_context.rstrip() + "\n")
    if r.trello_context.strip():
        parts.append(r.trello_context.rstrip() + "\n")
    conn_md = "\n".join(f"- {c}" for c in r.connections) if r.connections else "- 분석 정보 없음"
    parts.append(f"## 파라메타 서비스 연결점\n{conn_md}\n")
    if r.parascope:
        ps = "\n".join(f"- {p}" for p in r.parascope)
        parts.append(f"## ParaScope 브리핑\n{ps}\n")
    if preserved_sections.strip():
        parts.append(preserved_sections.rstrip() + "\n")
    if sources_log_line:
        parts.append(f"## 출처 로그\n{sources_log_line}\n")
    return "\n".join(parts).rstrip() + "\n"
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_research_types.py tests/ -q`
Expected: PASS (신규 + 전체 회귀 — 데드코드라 기존 불변)

- [ ] **Step 5: 커밋**

```bash
git add agents/research_types.py tests/test_research_types.py
git commit -m "feat(research): to_markdown 단방향 직렬화 + 기존 추출기 호환 라운드트립 테스트 (단계0)"
```

---

### Task 3: 문서 — 스트랭글러 진행 메모

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: 구현 — `CLAUDE.md`에 "회사리서치 구조화(진행중)" 한 줄 추가** (회의록 생성 절 인근)

```markdown
### 회사리서치 구조화 (스트랭글러, 진행중)

회사리서치/렌더의 '마크다운 손실 왕복'(구조화→문자열→정규식 재파싱)을 끊는 중. `agents/research_types.py`의 `CompanyResearch`/`NewsItem` 구조화 객체를 단일 진실로 흐르게 하고, 마크다운은 `to_markdown()` 한 방향 방출(저장)·렌더는 객체 직접 소비로 전환. 단계: 0 타입/파서(완료) → 1 run_company_research가 객체 생성 → 2 렌더 객체화(플래그) → 3 판정 단일화 → 4 추출기 제거. 설계: `docs/superpowers/specs/2026-06-26-architecture-audit-company-research.md`.
```

- [ ] **Step 2: 최종 회귀**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 3: 커밋**

```bash
git add CLAUDE.md
git commit -m "docs: 회사리서치 구조화 스트랭글러 진행 메모 (단계0)"
```

---

## 완료 기준 (DoD)
- `agents/research_types.py`: `NewsItem`/`CompanyResearch`/`parse_trend_bullets`/`to_markdown`.
- `parse_trend_bullets`가 trend 불릿(제목·요약·URL·날짜·정보없음)을 정확히 구조화.
- `to_markdown` 출력을 **기존 `_extract_company_content_sections`가 파싱 복원**(전환기 호환 라운드트립 통과).
- 데드코드 — 기존 호출부 0 변경, 전체 `pytest` 통과.

## 다음 단계 (이 PR 밖)
1. `run_company_research`가 `CompanyResearch` 생성 → `to_markdown()`으로 직렬화(외부 인터페이스 불변, 골든 회귀).
2. 렌더 객체화(`STRUCTURED_RENDER` 플래그, 추출기 폴백).
3. 판정 단일화(`judge(list[NewsItem])`, 골든셋 게이트).
4. 추출기·`_format_news_line_for_slack`·`_RESEARCH_HEADERS` 제거(폴백 0회 입증 후).
5. (후순위) 브리핑 오케스트레이션 정리.
