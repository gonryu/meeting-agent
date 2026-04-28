"""Briefing Meeting Classifier — 브리핑 전 회의 유형 분류기

harness-100 #84 meeting-strategist의 agenda-architect 패턴을 회의 유형 분류로 적용.
업체 리서치를 시작하기 전, 회의가 내부(자사) 회의인지 외부 회의인지 판별하여
내부 회의는 리서치를 건너뛰도록 한다 (QA 이슈 2.1: 자사 업체 리서치 낭비 차단).

흐름:
  classify_meeting(title, attendees, company_hint, description)
    → JSON {meeting_type, confidence, internal_companies_detected,
            external_companies_detected, research_recommended, rationale}

실패 시: 호출부가 None을 받아 안전하게 기존 경로(리서치 진행)로 폴백.
환경변수 BRIEFING_CLASSIFIER_ENABLED=false 로 비활성화 가능.
"""
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent.parent / "prompts" / "templates" / "briefing"

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_HAIKU = "claude-haiku-4-5"

_SYSTEM_PROMPT = """\
당신은 회의 분류 다중 에이전트 시스템의 일부입니다. 다음 원칙을 반드시 준수하세요:

1. **사실 기반**: 제공된 메타데이터에 실제로 존재하는 정보만 활용합니다. 추측·창작 금지.
2. **JSON 출력**: 코드펜스·설명 없이 순수 JSON 객체만 출력합니다.
3. **보수적 판정**: 외부 참석자가 1명이라도 분명히 보이면 외부 회의로 분류합니다.
4. **내부 별칭 우선**: 자사·계열사 별칭에 매칭되는 회사명은 내부로 처리합니다.
"""

# 내부(자사·계열사) 회사·서비스 별칭 — 대소문자 무시 부분 일치
# 자사 회사명과 자사 서비스명을 모두 포함. 한글·영문 변형 모두 포함.
INTERNAL_COMPANY_ALIASES = [
    # 회사명
    "parametacorp",
    "parameta",
    "파라메타",
    "iconloop",
    "아이콘루프",
    # 서비스명
    "supercycl",
    "슈퍼사이클",
    "수퍼사이클",
    "파라스타",
    "parasta",
    "테마틱볼트",
    "브루프",
    "broof",
    "myid",
    "마이아이디",
]

# 외부 회의 마커 — 제목/설명에 이 단어가 포함되면 LLM 호출 없이 외부로 분류
# (자사 서비스명이 함께 등장해도 마커가 있으면 외부 미팅으로 간주.
#  예: "미팅대상업체 supercycl 제안" → external — supercycl이 우리 서비스 이름과 같더라도
#  이 자리에서는 미팅 상대 업체명이므로 리서치 진행.)
EXTERNAL_MEETING_MARKERS = [
    "미팅대상업체",
    "미팅 대상업체",
    "미팅 대상 업체",
]


def is_enabled() -> bool:
    """환경변수로 분류기 활성/비활성 토글"""
    return os.getenv("BRIEFING_CLASSIFIER_ENABLED", "true").lower() != "false"


def _load_template(name: str) -> str:
    return (_TEMPLATES_DIR / f"{name}.md").read_text(encoding="utf-8")


def _render(template: str, **vars) -> str:
    out = template
    for k, v in vars.items():
        out = out.replace("{{" + k + "}}", str(v) if v is not None else "")
    return out


def _call_llm(prompt: str, *, model: str = _HAIKU, max_tokens: int = 1024) -> str:
    msg = _claude.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _parse_json(text: str) -> dict:
    """LLM 응답에서 JSON을 안전하게 추출. 코드펜스가 섞여 와도 처리."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    return json.loads(text)


def _matches_internal_alias(name: str) -> bool:
    """주어진 문자열이 내부 별칭에 부분 일치하는지(대소문자 무시) 검사"""
    if not name:
        return False
    lo = name.lower()
    return any(alias.lower() in lo for alias in INTERNAL_COMPANY_ALIASES)


def _detect_external_marker(title: str, description: str) -> str | None:
    """제목/설명에 외부 회의 마커가 포함되어 있으면 매칭된 마커 문자열을 반환.

    "미팅대상업체"처럼 이 미팅이 외부 업체와의 미팅임을 명시하는 표현이 있으면
    자사 서비스명이 함께 등장해도 외부로 분류해야 한다.
    """
    if not title and not description:
        return None
    text = f"{title or ''} {description or ''}".lower()
    for marker in EXTERNAL_MEETING_MARKERS:
        if marker.lower() in text:
            return marker
    return None


def _heuristic_classify(*, attendees: list[dict], company_hint: str,
                         internal_domains: set[str]) -> dict | None:
    """LLM 호출 실패 시 사용할 단순 휴리스틱 폴백.

    - 모든 참석자 도메인이 내부 + 업체 힌트가 없거나 내부 별칭이면 internal.
    - 그 외 명확한 외부인이 있으면 external.
    - 애매하면 None을 반환하여 호출부가 안전 경로(리서치 진행)로 가도록 함.
    """
    if not attendees:
        # 참석자 정보가 없으면 힌트만으로 결정
        if company_hint and _matches_internal_alias(company_hint):
            return {
                "meeting_type": "internal",
                "confidence": 0.6,
                "internal_companies_detected": [company_hint],
                "external_companies_detected": [],
                "research_recommended": False,
                "rationale": "참석자 정보 없음, 업체 힌트가 내부 별칭에 매칭.",
            }
        return None

    external_domains: list[str] = []
    internal_count = 0
    for a in attendees:
        email = (a.get("email") or "").strip()
        if not email or "@" not in email:
            continue
        domain = email.split("@")[-1].lower()
        if domain in internal_domains:
            internal_count += 1
        else:
            external_domains.append(domain)

    hint_is_internal = _matches_internal_alias(company_hint) if company_hint else False

    if not external_domains and internal_count > 0 and (not company_hint or hint_is_internal):
        return {
            "meeting_type": "internal",
            "confidence": 0.9,
            "internal_companies_detected": [company_hint] if hint_is_internal else [],
            "external_companies_detected": [],
            "research_recommended": False,
            "rationale": "참석자 전원이 내부 도메인이며 외부 업체 단서 없음.",
        }
    if external_domains:
        return {
            "meeting_type": "external" if internal_count == 0 else "mixed",
            "confidence": 0.85,
            "internal_companies_detected": [company_hint] if hint_is_internal else [],
            "external_companies_detected": [company_hint] if (company_hint and not hint_is_internal) else [],
            "research_recommended": True,
            "rationale": f"외부 도메인 참석자 감지: {', '.join(sorted(set(external_domains))[:3])}.",
        }
    return None


def classify_meeting(*, title: str, attendees: list[dict],
                     company_hint: str = "",
                     description: str = "") -> dict | None:
    """회의 메타데이터를 받아 내부/외부 분류 결과를 반환.

    Args:
        title: 회의 제목
        attendees: [{"email": "...", "name": "...", ...}, ...]
        company_hint: extendedProperties.private.company 또는 사전 추론된 업체명
        description: 회의 설명/어젠다

    Returns:
        분류 결과 dict, 또는 실패 시 None (호출부는 안전하게 리서치 경로로 폴백).
    """
    internal_domains_env = os.getenv("INTERNAL_DOMAINS", "parametacorp.com,iconloop.com")
    internal_domains = {d.strip().lower() for d in internal_domains_env.split(",") if d.strip()}

    t0 = datetime.now()

    # 0단계: 외부 마커 사전 체크 — "미팅대상업체" 등이 있으면 LLM 호출 없이 외부 분류
    # (자사 서비스명이 같이 등장해도 마커가 있으면 외부로 간주)
    marker = _detect_external_marker(title, description)
    if marker:
        result = {
            "meeting_type": "external",
            "confidence": 0.95,
            "internal_companies_detected": [],
            "external_companies_detected": [company_hint] if company_hint else [],
            "research_recommended": True,
            "rationale": f"외부 회의 마커 '{marker}' 감지 (제목/설명).",
        }
        elapsed = (datetime.now() - t0).total_seconds()
        log.info(
            "Briefing Classifier 마커 우회 ({:.2f}s) — '{}' 감지로 외부 분류".format(elapsed, marker)
        )
        return result

    # 1단계: LLM 분류 시도
    try:
        template = _load_template("meeting_classifier")
        # 참석자에서 분류에 필요한 필드만 추려 토큰 절약
        compact_attendees = [
            {
                "email": a.get("email", ""),
                "name": a.get("displayName") or a.get("name") or "",
                "domain": (a.get("email") or "").split("@")[-1].lower() if "@" in (a.get("email") or "") else "",
            }
            for a in attendees
        ]
        prompt = _render(
            template,
            title=title or "(제목 없음)",
            description=description or "(없음)",
            attendees=json.dumps(compact_attendees, ensure_ascii=False, indent=2),
            company_hint=company_hint or "",
            internal_domains=", ".join(sorted(internal_domains)),
            internal_aliases=", ".join(INTERNAL_COMPANY_ALIASES),
            external_markers=", ".join(EXTERNAL_MEETING_MARKERS),
        )
        raw = _call_llm(prompt, model=_HAIKU, max_tokens=1024)
        result = _parse_json(raw)

        # 사후 보정: 업체 힌트가 내부 별칭에 매칭되면 강제 내부 처리
        if company_hint and _matches_internal_alias(company_hint):
            if company_hint not in result.get("internal_companies_detected", []):
                result.setdefault("internal_companies_detected", []).append(company_hint)
            # 외부 참석자가 없으면 internal로 강제, 있으면 mixed 유지
            if result.get("meeting_type") == "external":
                external_present = any(
                    "@" in (a.get("email") or "")
                    and (a.get("email") or "").split("@")[-1].lower() not in internal_domains
                    for a in attendees
                )
                if not external_present:
                    result["meeting_type"] = "internal"
                    result["research_recommended"] = False
                    result["rationale"] = (result.get("rationale", "") +
                                           " (사후 보정: 내부 별칭 매칭, 외부 도메인 없음)").strip()

        elapsed = (datetime.now() - t0).total_seconds()
        log.info(
            "Briefing Classifier 완료 ({:.2f}s) — type={}, conf={:.2f}, "
            "research={}, rationale={}".format(
                elapsed,
                result.get("meeting_type"),
                float(result.get("confidence", 0.0)),
                result.get("research_recommended"),
                (result.get("rationale", "") or "")[:120],
            )
        )
        return result
    except Exception as e:
        log.warning(f"Briefing Classifier LLM 실패, 휴리스틱 폴백 시도: {e}")

    # 2단계: 휴리스틱 폴백
    fallback = _heuristic_classify(
        attendees=attendees, company_hint=company_hint,
        internal_domains=internal_domains,
    )
    if fallback is not None:
        log.info(
            "Briefing Classifier 휴리스틱 폴백 — type={}, research={}, rationale={}".format(
                fallback["meeting_type"],
                fallback["research_recommended"],
                fallback["rationale"],
            )
        )
    else:
        log.info("Briefing Classifier: 분류 결과 없음 (안전 경로로 리서치 진행)")
    return fallback
