"""업체 뉴스 관련성 판정 — web_search 결과를 사후 판정(생성기 불신).

negative fast-cut(LLM 없음) → Sonnet high/mid/low 판정 → high·mid만 보존.
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
# 관련성 판정은 등급(high/mid/low/exclude) 판단이라 Sonnet 사용(회의록·제안서와 동일 티어).
# fast-cut이 명백한 노이즈를 먼저 제거하므로 LLM엔 애매한 경계 케이스만 도달.
_MODEL = "claude-sonnet-5"
_TEMPLATE = Path(__file__).parent.parent / "prompts" / "templates" / "news_relevance.md"
_NO_INFO = "- 파라메타 사업 맥락의 최근 공개 정보 없음"

# 가격/시세 노이즈 정규식 (LLM 없이 즉시 컷)
# precision-first: 고신뢰 패턴만 컷. 모호한 bare 토큰(급등/청산/고래/호재 등)·
# 퍼센트(\d+%)는 정상 기사를 영구 소실시킬 수 있어 제거 — LLM 판정에 위임.
_PRICE_RE = re.compile(
    r"시세|김프|김치프리미엄|"
    r"\[(?:마감|개장|일일|주간)?시황\]"
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


def _judge_with_llm(company_name: str, bullets: list[str], today: str = None,
                    model: str = None) -> dict:
    """남은 불릿을 LLM으로 high/mid/low/exclude 판정. Returns: {index: relevance}.

    model 미지정 시 모듈 기본(_MODEL). eval 하네스가 Haiku/Sonnet 비교에 사용."""
    relevance_def = _load_relevance_def()
    from datetime import datetime
    today = today or datetime.now().strftime("%Y-%m-%d")
    numbered = "\n".join(
        f"{i}. {b.lstrip('-').strip()}" for i, b in enumerate(bullets)
    )
    prompt = f"""{relevance_def}

---
대상 업체: {company_name}
오늘({today}) 기준

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
        model=model or _MODEL, max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw)
    return {int(it["i"]): str(it.get("relevance", "mid")).lower()
            for it in data.get("items", [])}


_TREND_JUDGE_TEMPLATE = (Path(__file__).parent.parent / "prompts" / "templates"
                         / "research" / "company" / "trend_judge.md")


def _judge_domain_keep(company_name: str, bullets: list[str]) -> set[int]:
    """도메인 렌즈로 유지할 항목의 0-based 인덱스 집합 반환(재작성 없음).

    실패 시 예외 → 호출부(judge)가 best-effort 폴백. trend_judge.md(핫리로드) 기준."""
    try:
        criteria = _TREND_JUDGE_TEMPLATE.read_text(encoding="utf-8")
    except Exception as e:
        log.warning(f"trend_judge.md 로드 실패: {e}")
        raise
    numbered = "\n".join(f"{i}. {b}" for i, b in enumerate(bullets))
    prompt = (criteria.replace("{{company}}", company_name)
                      .replace("{{numbered}}", numbered))
    # 도메인 렌즈는 등급이 아니라 keep/drop이라 Haiku로 충분(strict 등급 회귀 방지 — #64/#65).
    resp = _claude.messages.create(
        model="claude-haiku-4-5", max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw)
    return {int(i) for i in data.get("keep", [])}


def judge(items: list, company_name: str) -> list:
    """동향 NewsItem 리스트를 파라메타 사업 렌즈로 필터링(유지 항목만 반환).

    항목을 재작성하지 않고 keep/drop + relevance 필드만 설정 → url/title/summary가
    구조적으로 보존된다(마크다운 왕복·LLM 재작성 경로 없음). 오케스트레이터·단일경로 공통
    진입점(스트랭글러 단계3). best-effort: fast-cut(시세/광고) 후 LLM 실패 시 생존분 통과.
    """
    if not items:
        return []
    negatives = _load_negatives()

    def _is_neg(it) -> bool:
        text = f"- {it.title} {it.summary}"
        return bool(_PRICE_RE.search(text) or _matches_negative(text, negatives))

    survivors = [it for it in items if not _is_neg(it)]
    if not survivors:
        return []
    bullets = [f"{it.title}: {it.summary}".strip().rstrip(":").strip()
               for it in survivors]
    try:
        keep = _judge_domain_keep(company_name, bullets)
    except Exception as e:
        log.warning(f"judge 도메인 판정 실패, fast-cut 생존분 통과 ({company_name}): {e}")
        return survivors
    kept = []
    for i, it in enumerate(survivors):
        if i in keep:
            it.relevance = it.relevance or "mid"
            kept.append(it)
    return kept


def judge_news(company_name: str, news_text: str, today: str = None,
               add_tags: bool = True) -> str:
    """뉴스 텍스트를 관련성 판정해 high·mid만 보존한 마크다운 반환.

    add_tags=False면 보존된 불릿에 `[관련도: x]` 접미사를 붙이지 않는다
    (오케스트레이터의 raw 동향 불릿 판정 등 구조화 산출물에 태그 오염을 막기 위함).
    """
    if not news_text or not news_text.strip():
        return _NO_INFO
    negatives = _load_negatives()
    cut = _negative_fast_cut(news_text, negatives)
    cut_lines = cut.splitlines()
    bullets = [
        l for l in cut_lines
        if l.strip().startswith("- ") and "정보 없음" not in l
    ]
    if not bullets:
        return _NO_INFO
    try:
        verdicts = _judge_with_llm(company_name, bullets, today=today)
    except Exception as e:
        log.warning(f"뉴스 관련성 판정 실패, fast-cut 결과 통과 ({company_name}): {e}")
        # 비불릿 줄(헤더 등)은 임의 삭제하지 않고 보존, 불릿은 fast-cut 결과 그대로 통과
        kept = [l for l in cut_lines
                if l.strip() and (not l.strip().startswith("- ")
                                  or "정보 없음" not in l)]
        # 보존 가치가 있는 불릿이 하나도 없으면 정보 없음
        if not any(l.strip().startswith("- ") and "정보 없음" not in l for l in kept):
            return _NO_INFO
        return "\n".join(kept)
    kept = []
    bullet_idx = 0
    for line in cut_lines:
        s = line.strip()
        if not s:
            continue
        # 비불릿 줄(### 헤더 등)·'정보 없음' 줄은 임의 삭제하지 않고 보존
        if not s.startswith("- ") or "정보 없음" in s:
            kept.append(line.rstrip())
            continue
        rel = verdicts.get(bullet_idx, "mid")  # 판정 누락 항목은 보존(mid)
        bullet_idx += 1
        if rel in ("high", "mid"):
            if add_tags:
                kept.append(f"{line.rstrip()} `[관련도: {rel}]`")
            else:
                kept.append(line.rstrip())
    # 보존된 불릿이 하나도 없으면(헤더만 남으면) 정보 없음
    if not any(l.strip().startswith("- ") and "정보 없음" not in l for l in kept):
        return _NO_INFO
    return "\n".join(kept) if kept else _NO_INFO
