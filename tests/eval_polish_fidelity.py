"""Korean polish fidelity eval.

Checks whether proposed polished text preserves protected facts. This is the
gate that should run after optional im-not-ai/humanize-korean output.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.korean_polish import validate_fidelity  # noqa: E402


@dataclass
class Case:
    id: str
    original: str
    polished: str
    should_pass: bool
    protected_terms: list[str] = field(default_factory=list)


CASES = [
    Case(
        "safe-style-only",
        "AI 기술을 통해 효율을 높인다.",
        "AI 기술로 효율을 높인다.",
        True,
    ),
    Case(
        "url-change",
        "상세: https://kisa.or.kr/n2sf",
        "상세: https://example.com/n2sf",
        False,
    ),
    Case(
        "number-change",
        "예산은 3000만원이다.",
        "예산은 4000만원이다.",
        False,
    ),
    Case(
        "protected-term-change",
        "MyID는 DID 기반 신원인증 플랫폼이다.",
        "내아이디는 DID 기반 신원인증 플랫폼이다.",
        False,
        ["MyID", "DID"],
    ),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Polish fidelity eval")
    parser.add_argument("--show-failures", action="store_true")
    args = parser.parse_args()

    passed = 0
    for case in CASES:
        ok, reasons = validate_fidelity(case.original, case.polished, case.protected_terms)
        correct = ok == case.should_pass
        passed += int(correct)
        mark = "PASS" if correct else "FAIL"
        print(f"{mark} {case.id}: expected={case.should_pass} actual={ok} reasons={reasons}")
        if args.show_failures and not correct:
            print(f"original: {case.original}\npolished: {case.polished}")

    total = len(CASES)
    print(f"\npolish_fidelity: {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
