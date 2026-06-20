"""업체 뉴스 관련성 판정 — web_search 결과를 사후 판정(생성기 불신).

negative fast-cut(LLM 없음) → Haiku high/mid/low 판정 → high·mid만 보존.
best-effort: 판정 LLM 실패 시 fast-cut 결과만 통과(절대 '정보 없음' 강제 생성 안 함).
정의: prompts/templates/news_relevance.md (핫리로드).
"""
import json
import logging
import os
import re
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_MODEL = "claude-haiku-4-5"
_TEMPLATE = Path(__file__).parent.parent / "prompts" / "templates" / "news_relevance.md"
_NO_INFO = "- 파라메타 사업 맥락의 최근 공개 정보 없음"

# 가격/시세 노이즈 정규식 (LLM 없이 즉시 컷)
_PRICE_RE = re.compile(
    r"시세|김프|급등|급락|폭락|호재|매수세|매도세|청산|"
    r"\[(?:마감|개장|일일|주간)?시황\]|"
    r"\d+\s*%"
)


def _load_relevance_def() -> str:
    try:
        return _TEMPLATE.read_text(encoding="utf-8")
    except Exception as e:
        log.warning(f"news_relevance.md 로드 실패: {e}")
        return ""


def _load_negatives() -> list[str]:
    """'## Negative' 섹션의 불릿을 negative 문자열 목록으로 파싱(쉼표 분리)."""
    negs, in_neg = [], False
    for line in _load_relevance_def().splitlines():
        s = line.strip()
        if s.startswith("## "):
            in_neg = "negative" in s.lower()
            continue
        if in_neg and s.startswith("- "):
            for token in s[2:].split(","):
                token = token.strip()
                if token:
                    negs.append(token)
    return negs


def _matches_negative(line: str, negatives: list[str]) -> bool:
    lowered = line.lower()
    for neg in negatives:
        n = neg.strip().lower()
        if not n:
            continue
        if n.isascii():
            if re.search(rf"\b{re.escape(n)}\b", lowered):
                return True
        elif n in line:
            return True
    return False


def _negative_fast_cut(news_text: str, negatives: list[str] = None) -> str:
    """negative/시세 매칭 불릿 줄 제거. 비불릿·'정보 없음' 줄은 보존."""
    if negatives is None:
        negatives = _load_negatives()
    kept = []
    for line in news_text.splitlines():
        s = line.strip()
        if not s:
            continue
        if "정보 없음" in s or not s.startswith("- "):
            kept.append(line)
            continue
        if _PRICE_RE.search(s) or _matches_negative(s, negatives):
            continue
        kept.append(line)
    return "\n".join(kept)
