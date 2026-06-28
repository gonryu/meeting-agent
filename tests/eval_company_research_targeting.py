"""Company research targeting eval.

Deterministic checks for the pre-search targeting layer: alias expansion,
domain-keyword guidance, URL requirement, and internal-company bypass.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents import company_profile  # noqa: E402
from agents import research_orchestrator as ro  # noqa: E402


@dataclass
class Case:
    id: str
    company: str
    must_contain: list[str]
    must_not_contain: list[str]
    internal: bool = False


CASES = [
    Case(
        "dunamu-upbit",
        "두나무",
        ["업비트", "Upbit", "Dunamu", "실명계좌", "특금법", "추천 검색 질의",
         "업비트 실명계좌", "URL 없는 항목은 제외"],
        [],
    ),
    Case(
        "upbit-dunamu",
        "업비트",
        ["두나무", "Upbit", "Dunamu", "실명계좌", "FIU", "추천 검색 질의",
         "두나무 실명계좌", "URL 없는 항목은 제외"],
        [],
    ),
    Case(
        "danal-paycoin",
        "다날",
        ["페이코인", "Paycoin", "스테이블코인", "온체인 KYC", "ERC-1101",
         "추천 검색 질의", "페이코인 스테이블코인"],
        [],
    ),
    Case(
        "payco-nhn",
        "페이코",
        ["NHN페이코", "PAYCO", "간편결제", "전자금융", "추천 검색 질의"],
        [],
    ),
    Case(
        "samsung-research",
        "삼성 리서치",
        ["Samsung Research", "AI 보안", "블록체인", "추천 검색 질의"],
        [],
    ),
    Case(
        "samsung-securities",
        "삼성증권",
        ["STO", "토큰증권", "비수탁 지갑", "추천 검색 질의"],
        [],
    ),
    Case(
        "komsa-public-agency",
        "komsa",
        ["KOMSA", "선박검사", "전자증서", "DID", "추천 검색 질의"],
        [],
    ),
    Case(
        "internal-parameta",
        "파라메타",
        ["자사/내부 조직"],
        ["동일 실체/검색 별칭"],
        internal=True,
    ),
]


def _prompt_for(company: str) -> str:
    template = ro._load_template("company", "trend_signals.md")
    return ro._render(
        template,
        company_name=company,
        today="2026-06-28",
        search_context=company_profile.trend_search_context(company),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Company research targeting eval")
    parser.add_argument("--show-prompts", action="store_true")
    args = parser.parse_args()

    passed = 0
    total = 0
    for case in CASES:
        text = _prompt_for(case.company)
        failures: list[str] = []
        for token in case.must_contain:
            total += 1
            if token not in text:
                failures.append(f"missing:{token}")
        for token in case.must_not_contain:
            total += 1
            if token in text:
                failures.append(f"forbidden:{token}")
        total += 1
        if company_profile.is_internal_company(case.company) != case.internal:
            failures.append("internal_classification")
        case_checks = len(case.must_contain) + len(case.must_not_contain) + 1
        passed += case_checks - len(failures)
        print(f"{'PASS' if not failures else 'FAIL'} {case.id}: {failures}")
        if args.show_prompts:
            print(text)

    print(f"\ncompany_research_targeting: {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
