"""온톨로지 딥 리서치 합성 — 출처기반 브리핑(R2) + grounding critic(R3).

news_relevance.py 패턴: 자체 anthropic 클라이언트 + 템플릿 핫리로드.
합성=Sonnet(고품질), critic=Haiku(검증은 가벼움). best-effort: 실패 시 None/폴백.
"""
import logging
import os
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
