"""온톨로지 합성 grounding eval — 브리핑 주장이 출처에 근거하는지 (LLM-as-judge).

사용:
    .venv/bin/python tests/eval_ontology_grounding.py            # oracle (sanity)
    .venv/bin/python tests/eval_ontology_grounding.py --mode sonnet   # 실 호출(요금)
"""
import argparse, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
_GOLDEN = Path(__file__).parent / "golden" / "ontology_grounding.jsonl"
_SONNET = "claude-sonnet-5"


def load_golden() -> list[dict]:
    return [json.loads(l) for l in _GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]


def judge_oracle(row: dict) -> bool:
    return row["expected_grounded"]


def judge_sonnet(row: dict) -> bool:
    import anthropic
    c = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    prompt = (f"출처:\n{chr(10).join(row['sources'])}\n\n브리핑:\n{row['brief']}\n\n"
              "브리핑의 모든 사실이 출처에 근거하면 GROUNDED, 출처에 없는 주장(환각)이 "
              "하나라도 있으면 HALLUCINATED만 출력.")
    r = c.messages.create(model=_SONNET, max_tokens=10,
                          messages=[{"role": "user", "content": prompt}])
    return "GROUNDED" in r.content[0].text.upper()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["oracle", "sonnet"], default="oracle")
    args = ap.parse_args()
    judge = {"oracle": judge_oracle, "sonnet": judge_sonnet}[args.mode]
    rows = load_golden()
    correct = sum(1 for r in rows if judge(r) == r["expected_grounded"])
    acc = correct / len(rows) if rows else 0.0
    print(f"\n=== ontology grounding eval (mode={args.mode}, n={len(rows)}) ===")
    print(f"accuracy: {acc:.3f}")
    threshold = 1.0 if args.mode == "oracle" else 0.5
    if acc < threshold:
        print(f"FAIL: {acc:.3f} < {threshold}"); return 1
    print(f"PASS: {acc:.3f} >= {threshold}"); return 0


if __name__ == "__main__":
    sys.exit(main())
