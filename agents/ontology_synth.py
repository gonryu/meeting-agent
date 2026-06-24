"""온톨로지 딥 리서치 합성 — 출처기반 브리핑(R2) + grounding critic(R3).

news_relevance.py 패턴: 자체 anthropic 클라이언트 + 템플릿 핫리로드.
합성=Sonnet(고품질), critic=Haiku(검증은 가벼움). best-effort: 실패 시 None/폴백.
"""
import logging
import os
import re
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_SYNTH_MODEL = "claude-sonnet-4-5"
_CRITIC_MODEL = "claude-haiku-4-5"
_TPL_DIR = Path(__file__).parent.parent / "prompts" / "templates"


def _load(name: str) -> str:
    try:
        return (_TPL_DIR / name).read_text(encoding="utf-8")
    except Exception as e:
        log.warning(f"{name} 로드 실패: {e}")
        return ""


def _fmt_relations(relations: list) -> str:
    return "\n".join(f"- {r.get('relation')}: {r.get('title')}" for r in (relations or [])) or "(없음)"


def _fmt_sources(docs: list) -> str:
    out = []
    for d in (docs or []):
        if not d.get("summary"):
            continue
        ym = f" ({d['ym']})" if d.get("ym") else ""
        out.append(f"### {d.get('title','')}{ym}\n{d.get('summary','')}")
    return "\n\n".join(out) or "(없음)"


def synthesize_company_brief(company: str, sources: dict) -> str | None:
    """출처기반 합성(R2) → grounding critic(R3) → 교정 마크다운. 출처 없으면 None."""
    if not sources or not (sources.get("docs") or sources.get("relations")):
        return None
    src_text = _fmt_sources(sources.get("docs"))
    if src_text == "(없음)":
        return None  # 본문 있는 문서가 하나도 없으면 합성 불가
    rel_text = _fmt_relations(sources.get("relations"))
    # R2 합성
    prompt = (_load("ontology_brief.md")
              .replace("{{company}}", company)
              .replace("{{relations}}", rel_text)
              .replace("{{sources}}", src_text))
    try:
        resp = _claude.messages.create(model=_SYNTH_MODEL, max_tokens=1500,
                                       messages=[{"role": "user", "content": prompt}])
        brief = resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"온톨로지 합성 실패({company}): {e}")
        return None
    if not brief:
        return None
    # R3 grounding critic (best-effort)
    try:
        check = (_load("ontology_grounding_check.md")
                 .replace("{{sources}}", src_text)
                 .replace("{{brief}}", brief))
        cresp = _claude.messages.create(model=_CRITIC_MODEL, max_tokens=1500,
                                        messages=[{"role": "user", "content": check}])
        corrected = cresp.content[0].text.strip()
        return corrected or brief
    except Exception as e:
        log.warning(f"grounding critic 실패({company}), 합성 결과 통과: {e}")
        return brief


# 온톨로지 스니펫 앞의 출처 신뢰도 마커(> ⚠️ UNCERTAIN — confidence: 0.55 등)는
# LLM을 "내용 불확실"로 오해시켜 메타-코멘트를 유발 → 합성 전 제거.
_PROVENANCE_RE = re.compile(r"^\s*>.*confidence.*$", re.IGNORECASE | re.MULTILINE)
# 합성 결과가 요약 대신 메타-코멘트(미래 약속)로 도망간 경우 감지 → 폐기.
_META_KEYWORDS = ("스니펫", "완성 후", "정리하겠", "향후", "확보하면", "불완전", "제공하겠")


def _clean_snippet(text: str) -> str:
    """스니펫에서 출처 신뢰도 마커·선두 인용부호 제거."""
    t = _PROVENANCE_RE.sub("", text or "")
    t = re.sub(r"^\s*>\s?", "", t, flags=re.MULTILINE)  # 남은 인용 '> ' 제거
    return t.strip()


def _fmt_snippets(docs: list) -> str:
    out = []
    for d in (docs or []):
        ym = f" ({d['ym']})" if d.get("ym") else ""
        out.append(f"- {d.get('title','')}{ym}: {_clean_snippet(d.get('snippet',''))}")
    return "\n".join(out)


def synthesize_recent_situation(company: str, recent: dict) -> dict | None:
    """브리핑 라이트 합성 — Haiku로 스니펫→최근상황 2~3문장 + 문서 링크.
    docs 없으면 None. critic 없음(표면 작음). best-effort → 실패 시 None.
    Returns: {summary: str, docs: [{title, uri}]}"""
    docs = (recent or {}).get("docs") or []
    if not docs:
        return None
    prompt = (_load("ontology_recent.md")
              .replace("{{company}}", company)
              .replace("{{snippets}}", _fmt_snippets(docs)))
    try:
        resp = _claude.messages.create(model=_CRITIC_MODEL, max_tokens=600,
                                       messages=[{"role": "user", "content": prompt}])
        summary = resp.content[0].text.strip()
    except Exception as e:
        log.warning(f"브리핑 온톨로지 라이트 합성 실패({company}): {e}")
        return None
    if not summary:
        return None
    # 메타-코멘트(요약 대신 "스니펫 불완전/완성 후 제공" 류)면 폐기 → 호출부가 폴백/생략
    if any(kw in summary for kw in _META_KEYWORDS):
        log.warning(f"브리핑 온톨로지 합성이 메타-코멘트 반환({company}), 폐기")
        return None
    links = [{"title": d.get("title", ""), "uri": d.get("uri", "")}
             for d in docs[:3] if d.get("uri")]
    return {"summary": summary, "docs": links}
