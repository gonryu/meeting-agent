"""news_relevance 골든셋 eval 하네스 (radar tests/eval_news_classify.py 패턴).

사용:
    .venv/bin/python tests/eval_news_relevance.py                 # oracle (sanity)
    .venv/bin/python tests/eval_news_relevance.py --mode stub     # 규칙 baseline (무비용)
    .venv/bin/python tests/eval_news_relevance.py --mode haiku    # 실 호출 (요금 발생)
    .venv/bin/python tests/eval_news_relevance.py --mode haiku --max-cases 5
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_GOLDEN = Path(__file__).parent / "golden" / "news_relevance.jsonl"
_LABELS = ["high", "mid", "low", "exclude"]
_DEFAULT_THRESHOLD = {"oracle": 1.0, "stub": 0.0, "haiku": 0.55, "sonnet": 0.55}
_HAIKU = "claude-haiku-4-5"
_SONNET = "claude-sonnet-4-5"


def load_golden() -> list[dict]:
    return [json.loads(l) for l in _GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]


def classify_oracle(row: dict) -> str:
    return row["expected"]["relevance"]


def classify_stub(row: dict) -> str:
    """규칙 baseline: 시세/시황 → low, 그 외 → mid (저급 baseline)."""
    import agents.news_relevance as nr
    line = f"- {row['title']} {row['description']}"
    if nr._negative_fast_cut(line).strip() == "":
        return "low"
    return "mid"


def _classify_llm(row: dict, model: str) -> str:
    import agents.news_relevance as nr
    bullet = [f"- {row['title']} {row['description']}"]
    try:
        verdict = nr._judge_with_llm(row["company"], bullet, model=model)
        return verdict.get(0, "mid")
    except Exception as e:
        print(f"  ! {row['id']} 판정 실패: {e}")
        return "mid"


def classify_haiku(row: dict) -> str:
    return _classify_llm(row, _HAIKU)


def classify_sonnet(row: dict) -> str:
    return _classify_llm(row, _SONNET)


def precision_recall_f1(matrix: dict) -> dict:
    out = {}
    for label in _LABELS:
        tp = matrix.get((label, label), 0)
        fp = sum(matrix.get((e, label), 0) for e in _LABELS if e != label)
        fn = sum(matrix.get((label, a), 0) for a in _LABELS if a != label)
        p = tp / (tp + fp) if (tp + fp) else 0.0
        r = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * p * r / (p + r) if (p + r) else 0.0
        out[label] = {"precision": p, "recall": r, "f1": f1, "support": tp + fn}
    return out


def fmt_matrix(matrix: dict) -> str:
    header = "expected\\actual  " + "  ".join(f"{a:>8}" for a in _LABELS)
    lines = [header]
    for e in _LABELS:
        row = "  ".join(f"{matrix.get((e, a), 0):>8}" for a in _LABELS)
        lines.append(f"{e:>14}  {row}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="news_relevance 골든셋 eval")
    parser.add_argument("--mode", choices=["oracle", "stub", "haiku", "sonnet"], default="oracle")
    parser.add_argument("--max-cases", type=int, default=0, help="실행할 케이스 수 제한(0=전체)")
    parser.add_argument("--threshold", type=float, default=None, help="평균 F1 임계(미지정 시 mode 기본)")
    args = parser.parse_args()

    rows = load_golden()
    if args.max_cases:
        rows = rows[: args.max_cases]
    classifier = {"oracle": classify_oracle, "stub": classify_stub,
                  "haiku": classify_haiku, "sonnet": classify_sonnet}[args.mode]

    matrix: dict = {}
    correct = 0
    for row in rows:
        actual = classifier(row)
        expected = row["expected"]["relevance"]
        matrix[(expected, actual)] = matrix.get((expected, actual), 0) + 1
        if actual == expected:
            correct += 1

    metrics = precision_recall_f1(matrix)
    macro_f1 = sum(m["f1"] for m in metrics.values()) / len(_LABELS)
    acc = correct / len(rows) if rows else 0.0

    print(f"\n=== news_relevance eval (mode={args.mode}, n={len(rows)}) ===")
    print(f"accuracy: {acc:.3f}  macro-F1: {macro_f1:.3f}\n")
    for label in _LABELS:
        m = metrics[label]
        print(f"  {label:>8}: P={m['precision']:.2f} R={m['recall']:.2f} "
              f"F1={m['f1']:.2f} (n={m['support']})")
    print("\n" + fmt_matrix(matrix))

    threshold = args.threshold if args.threshold is not None else _DEFAULT_THRESHOLD[args.mode]
    if macro_f1 < threshold:
        print(f"\nFAIL: macro-F1 {macro_f1:.3f} < threshold {threshold}")
        return 1
    print(f"\nPASS: macro-F1 {macro_f1:.3f} >= threshold {threshold}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
