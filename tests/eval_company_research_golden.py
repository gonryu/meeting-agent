"""Golden company research render eval.

This verifies representative company categories against rendered Slack output:
normal prospect, media company, and no-news company.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.slack_tools import build_company_research_block  # noqa: E402


@dataclass
class Case:
    id: str
    render: Callable[[], str]
    must_contain: list[str]
    must_not_contain: list[str]


def _text(blocks: list[dict]) -> str:
    return "\n".join(
        block.get("text", {}).get("text", "")
        for block in blocks
        if block.get("type") == "section"
    )


CASES = [
    Case(
        "kisa-linked-news",
        lambda: _text(build_company_research_block(
            "KISA",
            [],
            [],
            ["loopchain ↔ 공공 보안 인프라"],
            news_items=[
                {"title": "N2SF 도입", "summary": "공공 보안체계 확산", "url": "https://kisa.or.kr/n"},
            ],
        )),
        ["*🏢 KISA 리서치 결과*", "<https://kisa.or.kr/n|N2SF 도입> — 공공 보안체계 확산"],
        ["최근 동향 정보 없음", "• **", "last_searched"],
    ),
    Case(
        "tokenpost-media",
        lambda: _text(build_company_research_block(
            "토큰포스트",
            [],
            [],
            ["언론사로 분류되어 파라메타 서비스 연결점은 해당하지 않습니다."],
            news_items=[],
        )),
        ["*🏢 토큰포스트 리서치 결과*", "최근 동향 정보 없음", "언론사로 분류되어"],
        ["협력 가능성", "• **"],
    ),
    Case(
        "empty-company",
        lambda: _text(build_company_research_block(
            "무명업체",
            [],
            [],
            [],
            news_items=[],
        )),
        ["최근 동향 정보 없음", "분석 정보 없음"],
        ["last_searched", "• **"],
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Company research golden render eval")
    parser.add_argument("--show-text", action="store_true")
    args = parser.parse_args()

    passed = 0
    for case in CASES:
        text = case.render()
        failures = []
        for token in case.must_contain:
            if token not in text:
                failures.append(f"missing:{token}")
        for token in case.must_not_contain:
            if token in text:
                failures.append(f"forbidden:{token}")
        ok = not failures
        passed += int(ok)
        print(f"{'PASS' if ok else 'FAIL'} {case.id}: {failures}")
        if args.show_text:
            print(text)

    total = len(CASES)
    print(f"\ncompany_research_golden: {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
