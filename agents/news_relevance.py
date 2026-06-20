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


def _judge_with_llm(company_name: str, bullets: list[str]) -> dict:
    """남은 불릿을 Haiku로 high/mid/low/exclude 판정. Returns: {index: relevance}."""
    relevance_def = _load_relevance_def()
    numbered = "\n".join(
        f"{i}. {b.lstrip('-').strip()}" for i, b in enumerate(bullets)
    )
    prompt = f"""{relevance_def}

---
대상 업체: {company_name}

아래 뉴스 후보를 위 기준으로 각각 판정하라:
- high: {company_name}와 직접 연결된 파라메타 사업영역 사업 신호
- mid: 파라메타 사업영역의 관련 동향(직접 액션은 약함)
- low: 키워드만 맞는 단신·시세·마케팅
- exclude: {company_name}와 무관하거나 이름만 같은 다른 회사(동명 타사) 기사

JSON만 출력(코드펜스·설명 없이):
{{"items":[{{"i":0,"relevance":"high"}}]}}

후보:
{numbered}"""
    resp = _claude.messages.create(
        model=_MODEL, max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    raw = raw.removeprefix("```json").removeprefix("```").rstrip("```").strip()
    data = json.loads(raw)
    return {int(it["i"]): str(it.get("relevance", "mid")).lower()
            for it in data.get("items", [])}


def judge_news(company_name: str, news_text: str, today: str = None) -> str:
    """뉴스 텍스트를 관련성 판정해 high·mid만 보존한 마크다운 반환."""
    if not news_text or not news_text.strip():
        return _NO_INFO
    negatives = _load_negatives()
    cut = _negative_fast_cut(news_text, negatives)
    bullets = [
        l for l in cut.splitlines()
        if l.strip().startswith("- ") and "정보 없음" not in l
    ]
    if not bullets:
        return _NO_INFO
    try:
        verdicts = _judge_with_llm(company_name, bullets)
    except Exception as e:
        log.warning(f"뉴스 관련성 판정 실패, fast-cut 결과 통과 ({company_name}): {e}")
        cut_bullets = [l for l in cut.splitlines() if l.strip().startswith("- ")]
        return "\n".join(cut_bullets) if cut_bullets else _NO_INFO
    kept = []
    for i, line in enumerate(bullets):
        rel = verdicts.get(i, "mid")  # 판정 누락 항목은 보존(mid)
        if rel in ("high", "mid"):
            kept.append(f"{line.rstrip()} `[관련도: {rel}]`")
    return "\n".join(kept) if kept else _NO_INFO
