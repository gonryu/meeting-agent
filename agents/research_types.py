"""회사리서치 구조화 타입 + 단일 파서/직렬화 (스트랭글러 단계0).

원칙: 마크다운은 저장·표시 포맷일 뿐 통신 포맷이 아니다. 파싱은 parse_trend_bullets
한 곳에서만(하류 정규식 재파싱 제거 목표), 마크다운 방출은 to_markdown 한 방향.
설계: docs/superpowers/specs/2026-06-26-architecture-audit-company-research.md
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
            # 볼드 제목 없으면 ':' 또는 URL/괄호 앞까지를 제목으로
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


def _news_to_md(items: list[NewsItem]) -> str:
    if not items:
        return "- 최근 공개된 정보 없음"
    lines = []
    for n in items:
        parts = [p for p in (n.date,) if p]
        if n.url:
            parts.append(n.url)
        meta = " (" + ", ".join(parts) + ")" if parts else ""
        body = n.summary if n.summary else ""
        lines.append(f"- **[{n.title}]**: {body}{meta}".rstrip())
    return "\n".join(lines)


def render_company_news_block(r: CompanyResearch) -> str:
    """오케스트레이터 산출물(개요 + 최근 동향)을 위키 `## 최근 동향` 본문으로 직렬화.

    레거시 run_company_research final_md 포맷과 동등: 개요 마크다운 그대로 +
    `### 최근 동향 (날짜 기준)` 하위헤더 + 뉴스 불릿. 호출부(before.research_company)는
    이 문자열을 기존 news_text 자리에 삽입한다(전환기: 위키/저장/렌더 불변)."""
    head = (r.overview or "").rstrip()
    block = f"### 최근 동향 ({r.searched_at} 기준)\n{_news_to_md(r.news)}"
    return (head + "\n\n" + block) if head else block


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
