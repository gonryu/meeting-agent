"""Slack output quality eval.

Deterministic, no LLM/API calls. This complements unit tests by scoring rendered
Slack text against category-specific output guidelines.

Usage:
    .venv/bin/python tests/eval_output_quality.py
    .venv/bin/python tests/eval_output_quality.py --category company_research
    .venv/bin/python tests/eval_output_quality.py --show-text
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.slack_tools import (  # noqa: E402
    build_company_research_block,
    build_context_block,
    build_meeting_header_block,
)


@dataclass
class Rule:
    name: str
    check: Callable[[str], bool]
    guide: str


@dataclass
class Case:
    id: str
    category: str
    description: str
    render: Callable[[], str]
    rules: list[Rule] = field(default_factory=list)


def _text(blocks: list[dict]) -> str:
    return "\n".join(
        block.get("text", {}).get("text", "")
        for block in blocks
        if block.get("type") == "section"
    )


def _contains(s: str) -> Callable[[str], bool]:
    return lambda text: s in text


def _not_contains(s: str) -> Callable[[str], bool]:
    return lambda text: s not in text


def _matches(pattern: str) -> Callable[[str], bool]:
    rx = re.compile(pattern, re.MULTILINE)
    return lambda text: bool(rx.search(text))


COMMON_RULES = [
    Rule("no_double_star", _not_contains("**"), "Slack mrkdwn must not expose GitHub-style **bold**."),
    Rule("no_bookkeeping", _not_contains("last_searched"), "Internal bookkeeping lines must not be visible."),
    Rule("no_broken_news", _not_contains("• **"), "Malformed markdown-only news bullets must not render."),
]


def _case_company_research_structured() -> Case:
    def render() -> str:
        return _text(build_company_research_block(
            "KISA",
            news_lines=[],
            parascope_lines=[],
            connection_lines=["loopchain ↔ 공공 보안 인프라"],
            news_items=[
                {
                    "title": "N2SF 도입 본격화",
                    "summary": "공공 보안체계 확산 예산 투입",
                    "url": "https://kisa.or.kr/n2sf",
                },
                {
                    "title": "블록체인 밋업데이 교육생 모집",
                    "summary": "블록체인 신뢰인프라 인력 양성",
                    "url": "https://kisa.or.kr/bcmd",
                },
            ],
        ))

    return Case(
        id="company-structured-news",
        category="company_research",
        description="Structured NewsItem output renders clickable title links and one-line summaries.",
        render=render,
        rules=[
            *COMMON_RULES,
            Rule("has_title", _contains("*🏢 KISA 리서치 결과*"), "Company research output needs a clear title."),
            Rule("has_news_section", _contains("📰  *업체 동향*"), "News section must be present."),
            Rule("clickable_title_links", _matches(r"• <https://kisa\.or\.kr/n2sf\|N2SF 도입 본격화> — 공공 보안체계"), "News must be title-linked with summary after dash."),
            Rule("no_raw_url", _not_contains(" (https://"), "Structured news should not expose raw parenthesized URLs."),
            Rule("not_empty", _not_contains("최근 동향 정보 없음"), "Non-empty structured news must not fall back to no-info."),
            Rule("has_connections", _contains("🔗  *파라메타 서비스 연결점*"), "Connection section must be present."),
        ],
    )


def _case_company_research_empty() -> Case:
    def render() -> str:
        return _text(build_company_research_block(
            "무명업체",
            news_lines=[],
            parascope_lines=[],
            connection_lines=["분석 정보 없음"],
            news_items=[],
        ))

    return Case(
        id="company-empty-news",
        category="company_research",
        description="Empty news output is explicit and compact.",
        render=render,
        rules=[
            *COMMON_RULES,
            Rule("has_no_info_once", lambda text: text.count("최근 동향 정보 없음") == 1, "No-news state should be one clear line."),
            Rule("no_empty_bullets", _not_contains("• \n"), "No empty bullets."),
        ],
    )


def _case_media_company() -> Case:
    def render() -> str:
        return _text(build_company_research_block(
            "토큰포스트",
            news_lines=[],
            parascope_lines=[],
            connection_lines=["언론사로 분류되어 파라메타 서비스 연결점은 해당하지 않습니다."],
            news_items=[],
        ))

    return Case(
        id="media-company",
        category="media_company",
        description="Media companies skip business-news research and show fixed non-applicable connection text.",
        render=render,
        rules=[
            *COMMON_RULES,
            Rule("no_news", _contains("• 최근 동향 정보 없음"), "Media company should not fabricate company news."),
            Rule("media_connection", _contains("언론사로 분류되어 파라메타 서비스 연결점은 해당하지 않습니다."), "Media connection section should explain non-applicability."),
            Rule("no_fake_connection", _not_contains("협력 가능성"), "Media path must not invent service opportunities."),
        ],
    )


def _case_ontology_render() -> Case:
    def render() -> str:
        ontology = {
            "relations": [
                {"relation": "related-to", "title": "KISA 공공과제"},
                {"relation": "part-of", "title": "업비트"},
                {"relation": "related-to", "title": "01. Cluster 구성하기"},
            ],
            "documents": [
                {"title": "KISA_보안운영_명세서.xlsx", "uri": "https://drive/doc1"},
                {"title": "회의 메모", "uri": ""},
            ],
        }
        return _text(build_company_research_block(
            "KISA",
            news_lines=[],
            parascope_lines=[],
            connection_lines=["loopchain ↔ 공공 보안 인프라"],
            news_items=[],
            ontology=ontology,
        ))

    return Case(
        id="ontology-render",
        category="ontology_render",
        description="Ontology output uses Korean relation labels, filters numbered noise, and links documents.",
        render=render,
        rules=[
            *COMMON_RULES,
            Rule("has_ontology_section", _contains("🧠  *온톨로지(사내 지식)*"), "Ontology section must be clearly labeled."),
            Rule("ko_relation_related", _contains("• 관련: KISA 공공과제"), "English relation labels should be localized."),
            Rule("ko_relation_part_of", _contains("• 소속: 업비트"), "part-of should render as 소속."),
            Rule("no_raw_relation", _not_contains("related-to"), "Raw relation labels should not leak."),
            Rule("noise_removed", _not_contains("01. Cluster"), "Numbered section noise should be filtered."),
            Rule("doc_linked", _contains("• 문서: <https://drive/doc1|KISA_보안운영_명세서>"), "Drive documents should be clickable and extension-trimmed."),
        ],
    )


def _case_context_block() -> Case:
    def render() -> str:
        return _text(build_context_block({
            "trello_summary": ["제안서 초안 검토 필요"],
            "trello_card_name": "KISA DPP",
            "trello_url": "https://trello/card",
            "emails": [{"date": "2026-06-20", "subject": "Re: DPP 협의", "snippet": "협의"}],
            "minutes": [{"name": "KISA_DPP_킥오프_내부용.md", "modifiedTime": "2026-06-21T00:00:00Z", "id": "file1"}],
            "ontology_recent": {
                "summary": "KISA는 DPP 사업 발주기관이며 CSAP 협의 맥락이 있다.",
                "docs": [{"title": "K-BTF_Base_Partnership Proposal.pdf", "uri": "https://drive/kbtf"}],
            },
        }))

    return Case(
        id="context-block",
        category="context_block",
        description="Previous-context block separates Trello, email, minutes, and ontology recent situation.",
        render=render,
        rules=[
            *COMMON_RULES,
            Rule("has_previous_context", _contains("📌  *이전 미팅 맥락*"), "Previous context header required."),
            Rule("has_email", _contains("📧  *이메일 맥락*"), "Email context header required."),
            Rule("has_ontology_summary", _contains("KISA는 DPP 사업 발주기관"), "Ontology recent summary should be visible when available."),
            Rule("has_doc_link", _contains("<https://drive/kbtf|K-BTF_Base_Partnership Proposal>"), "Ontology docs should be linked and extension-trimmed."),
            Rule("no_false_empty_context", _not_contains("이전 미팅 기록 없음"), "Do not show empty state when context exists."),
        ],
    )


def _case_meeting_header() -> Case:
    def render() -> str:
        return _text(build_meeting_header_block(
            {
                "id": "evt1",
                "summary": "KISA DPP 점검",
                "start_time": "2026-06-27T15:00:00+09:00",
                "meet_link": "https://meet.google.com/abc",
                "location": "수서센터",
                "description": "DPP 중간점검\nCSAP 협의",
            },
            "KISA",
            attendee_names=["김민환", "kisa@example.com"],
        ))

    return Case(
        id="meeting-header",
        category="meeting_header",
        description="Meeting header shows time, Meet link, location, company, attendees, and agenda.",
        render=render,
        rules=[
            *COMMON_RULES,
            Rule("has_title", _contains("*📋 KISA DPP 점검"), "Header title should be visible."),
            Rule("has_meet_link", _contains("<https://meet.google.com/abc|Google Meet>"), "Meet URL should be rendered as a Slack link."),
            Rule("has_location", _contains("📍수서센터"), "Location should be visible when present."),
            Rule("has_company", _contains("🏢  *관련 업체*: KISA"), "Company label required."),
            Rule("has_attendees", _contains("👥  *참석자*: 김민환, kisa@example.com"), "Attendee display should use resolved full names/emails."),
            Rule("has_agenda", _contains("• DPP 중간점검"), "Agenda lines should be bulletized."),
        ],
    )


CASES = [
    _case_company_research_structured(),
    _case_company_research_empty(),
    _case_media_company(),
    _case_ontology_render(),
    _case_context_block(),
    _case_meeting_header(),
]


def run(category: str | None, show_text: bool) -> int:
    selected = [c for c in CASES if not category or c.category == category]
    if not selected:
        print(f"No cases for category: {category}")
        return 2

    total_rules = 0
    total_passed = 0
    by_category: dict[str, list[tuple[int, int]]] = {}

    for case in selected:
        text = case.render()
        results = [(rule, rule.check(text)) for rule in case.rules]
        passed = sum(1 for _, ok in results if ok)
        total = len(results)
        total_rules += total
        total_passed += passed
        by_category.setdefault(case.category, []).append((passed, total))

        print(f"\n=== {case.id} [{case.category}] ===")
        print(f"guide: {case.description}")
        print(f"score: {passed}/{total}")
        for rule, ok in results:
            mark = "PASS" if ok else "FAIL"
            print(f"  {mark:4} {rule.name}: {rule.guide}")
        if show_text:
            print("\n--- rendered text ---")
            print(text)

    print("\n=== category summary ===")
    failed = False
    for cat, scores in sorted(by_category.items()):
        p = sum(a for a, _ in scores)
        t = sum(b for _, b in scores)
        ratio = p / t if t else 0.0
        print(f"{cat:18} {p:2}/{t:<2}  {ratio:.3f}")
        if p != t:
            failed = True

    ratio = total_passed / total_rules if total_rules else 0.0
    print(f"\nTOTAL {total_passed}/{total_rules}  {ratio:.3f}")
    if failed:
        print("FAIL: one or more output quality rules failed")
        return 1
    print("PASS: all output quality rules passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic Slack output quality eval")
    parser.add_argument("--category", choices=sorted({c.category for c in CASES}))
    parser.add_argument("--show-text", action="store_true")
    args = parser.parse_args()
    return run(args.category, args.show_text)


if __name__ == "__main__":
    raise SystemExit(main())
