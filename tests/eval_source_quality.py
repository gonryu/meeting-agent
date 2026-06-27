"""Company research source quality eval.

Deterministic checks for source hygiene before/after assisted public-source
collection. This does not judge business relevance; it verifies citation shape.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


_URL_RE = re.compile(r"https?://[^\s)>\]]+")


@dataclass
class Case:
    id: str
    text: str
    should_pass: bool
    note: str


CASES = [
    Case(
        "good-two-sources",
        "- **[N2SF 도입]**: 공공 보안체계 확산 (https://kisa.or.kr/n2sf)\n"
        "- **[BCMD 모집]**: 블록체인 교육생 모집 (https://kisa.or.kr/bcmd)",
        True,
        "Every bullet has a URL.",
    ),
    Case(
        "missing-url",
        "- **[N2SF 도입]**: 공공 보안체계 확산\n"
        "- **[BCMD 모집]**: 블록체인 교육생 모집 (https://kisa.or.kr/bcmd)",
        False,
        "One news bullet has no source URL.",
    ),
    Case(
        "same-domain-ok",
        "- A (https://kisa.or.kr/a)\n- B (https://kisa.or.kr/b)",
        True,
        "Same official domain may be acceptable when every bullet is sourced.",
    ),
    Case(
        "no-bullets",
        "파라메타 사업 맥락의 최근 공개 정보 없음",
        True,
        "Explicit no-info state is acceptable.",
    ),
]


def check_source_quality(text: str) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    bullets = [line.strip() for line in text.splitlines() if line.strip().startswith("- ")]
    if not bullets:
        return True, []
    missing = [line for line in bullets if not _URL_RE.search(line)]
    if missing:
        reasons.append(f"missing_url:{len(missing)}")
    broken = [u for u in _URL_RE.findall(text) if u.endswith((".", ","))]
    if broken:
        reasons.append(f"broken_url:{len(broken)}")
    return not reasons, reasons


def main() -> int:
    parser = argparse.ArgumentParser(description="Source quality eval")
    parser.add_argument("--show-failures", action="store_true")
    args = parser.parse_args()

    passed = 0
    for case in CASES:
        ok, reasons = check_source_quality(case.text)
        correct = ok == case.should_pass
        passed += int(correct)
        mark = "PASS" if correct else "FAIL"
        print(f"{mark} {case.id}: expected={case.should_pass} actual={ok} reasons={reasons} — {case.note}")
        if args.show_failures and not correct:
            print(case.text)

    total = len(CASES)
    print(f"\nsource_quality: {passed}/{total}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
