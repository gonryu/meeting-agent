"""Deterministic company research hints.

LLM/web search is weak at resolving service-name/legal-entity aliases
(`두나무` ↔ `업비트`, `다날` ↔ `페이코인`). Keep a small deterministic
profile table for high-value targets and use it only as search guidance.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import re


@dataclass(frozen=True)
class CompanyResearchProfile:
    input_name: str
    canonical_name: str
    aliases: tuple[str, ...] = ()
    domain_terms: tuple[str, ...] = ()
    is_internal: bool = False


_INTERNAL_ALIASES = (
    "parametacorp", "parameta", "파라메타",
    "iconloop", "아이콘루프",
    "supercycl", "슈퍼사이클", "수퍼사이클",
    "파라스타", "parasta",
    "테마틱볼트",
    "브루프", "broof",
    "myid", "마이아이디",
)

_PROFILES = (
    CompanyResearchProfile(
        input_name="두나무",
        canonical_name="두나무",
        aliases=("두나무", "업비트", "Upbit", "Dunamu"),
        domain_terms=(
            "가상자산", "디지털자산", "실명계좌", "KYC", "AML", "특금법",
            "FIU", "거래소", "금융기관 제휴", "케이뱅크", "하나금융",
        ),
    ),
    CompanyResearchProfile(
        input_name="업비트",
        canonical_name="업비트",
        aliases=("업비트", "두나무", "Upbit", "Dunamu"),
        domain_terms=(
            "가상자산", "디지털자산", "실명계좌", "KYC", "AML", "특금법",
            "FIU", "거래소", "금융기관 제휴", "케이뱅크", "하나금융",
        ),
    ),
    CompanyResearchProfile(
        input_name="다날",
        canonical_name="다날",
        aliases=("다날", "다날핀테크", "페이코인", "Paycoin", "Danal", "Danal Fintech"),
        domain_terms=("스테이블코인", "페이코인", "결제", "정산", "온체인 KYC",
                      "ERC-1101", "가상자산", "Danal Fintech"),
    ),
    CompanyResearchProfile(
        input_name="페이코",
        canonical_name="페이코",
        aliases=("페이코", "NHN페이코", "PAYCO", "NHN PAYCO"),
        domain_terms=("간편결제", "전자금융", "핀테크", "결제", "본인확인", "인증", "KYC"),
    ),
    CompanyResearchProfile(
        input_name="삼성 리서치",
        canonical_name="삼성리서치",
        aliases=("삼성리서치", "삼성 리서치", "Samsung Research", "Samsung Research Korea"),
        domain_terms=("AI 보안", "블록체인", "디지털자산", "보안", "핀테크", "인증"),
    ),
    CompanyResearchProfile(
        input_name="삼성증권",
        canonical_name="삼성증권",
        aliases=("삼성증권", "Samsung Securities", "Samsung Securities Korea"),
        domain_terms=("STO", "토큰증권", "비수탁 지갑", "DeFi", "디지털자산", "KYC", "AML", "RFP"),
    ),
    CompanyResearchProfile(
        input_name="KOMSA",
        canonical_name="KOMSA",
        aliases=("KOMSA", "한국해양교통안전공단", "해양교통안전공단"),
        domain_terms=("선박검사", "전자증서", "DID", "VC", "검증체계", "블록체인", "공공과제", "KISA"),
    ),
)


def _norm(text: str) -> str:
    return "".join((text or "").lower().split())


def is_internal_company(company_name: str) -> bool:
    n = _norm(company_name)
    return bool(n) and any(_norm(alias) in n for alias in _INTERNAL_ALIASES)


def normalize_company_name(company_name: str) -> str:
    """Normalize command-ish company text to the canonical research target."""
    raw = (company_name or "").strip()
    if not raw:
        return ""

    direct = _profile_for_norm(_norm(raw))
    if direct:
        return direct.canonical_name
    if is_internal_company(raw):
        return raw

    candidates = [raw]
    for suffix in ("기업정보 리서치", "회사정보 리서치", "업체정보 리서치", "리서치", "알아봐줘", "조사해줘"):
        if raw.endswith(suffix):
            candidates.append(raw[: -len(suffix)].strip(" :,-"))
    compact = _norm(raw)
    if compact.endswith("리서치"):
        candidates.append(raw[:-3].strip(" :,-"))

    for candidate in candidates:
        profile = _profile_for_norm(_norm(candidate))
        if profile:
            return profile.canonical_name
    return candidates[-1] if candidates and candidates[-1] else raw


def _profile_for_norm(norm_name: str) -> CompanyResearchProfile | None:
    for profile in _PROFILES:
        keys = {_norm(profile.canonical_name), *(_norm(a) for a in profile.aliases)}
        if norm_name in keys:
            return profile
    return None


def research_profile(company_name: str) -> CompanyResearchProfile:
    company_name = normalize_company_name(company_name)
    if is_internal_company(company_name):
        return CompanyResearchProfile(
            input_name=company_name,
            canonical_name=company_name,
            aliases=(company_name,),
            is_internal=True,
        )
    n = _norm(company_name)
    profile = _profile_for_norm(n)
    if profile:
        return replace(profile, input_name=company_name)
    return CompanyResearchProfile(
        input_name=company_name,
        canonical_name=company_name,
        aliases=(company_name,),
    )


def trend_search_context(company_name: str) -> str:
    profile = research_profile(company_name)
    if profile.is_internal:
        return "- 자사/내부 조직입니다. 외부 업체 동향 검색 대상이 아닙니다."

    lines: list[str] = []
    aliases = [a for a in profile.aliases if _norm(a) != _norm(company_name)]
    if aliases:
        lines.append("- 동일 실체/검색 별칭: " + ", ".join(aliases))
    if profile.domain_terms:
        lines.append("- 우선 도메인 키워드: " + ", ".join(profile.domain_terms))
    queries = _suggested_queries(profile)
    if queries:
        lines.append("- 추천 검색 질의: " + " / ".join(queries))
    if lines:
        lines.append("- 각 뉴스는 대상 업체 또는 위 별칭 중 하나와 도메인 키워드 중 하나가 함께 확인되는 경우만 포함.")
        lines.append("- URL 없는 항목은 제외.")
    else:
        lines.append("- URL 없는 항목은 제외.")
    return "\n".join(lines)


def _suggested_queries(profile: CompanyResearchProfile) -> list[str]:
    aliases = list(profile.aliases)
    terms = list(profile.domain_terms)
    if not aliases or not terms:
        return []
    queries: list[str] = []
    for alias in aliases[:4]:
        for term in terms[:4]:
            if _norm(alias) == _norm(term):
                continue
            queries.append(f"{alias} {term}")
            if len(queries) >= 12:
                return queries
    return queries


def try_direct_company_research_route(text: str) -> tuple[str, dict] | None:
    """Route simple natural-language company research commands without LLM."""
    raw = (text or "").strip()
    if not raw or "\n" in raw:
        return None
    lowered = raw.lower()
    normalized_raw = normalize_company_name(raw)
    if normalized_raw != raw:
        return ("research_company", {"company": normalized_raw})
    trigger_patterns = (
        r"^(.+?)(?:\s*)리서치$",
        r"^(.+?)(?:\s*)기업정보(?:\s*)리서치$",
        r"^(.+?)(?:\s*)알아봐줘$",
        r"^(.+?)(?:\s*)조사해줘$",
    )
    if not any(token in lowered for token in ("리서치", "알아봐", "조사")):
        return None
    for pattern in trigger_patterns:
        m = re.match(pattern, raw)
        if not m:
            continue
        company = normalize_company_name(m.group(1).strip())
        if company:
            return ("research_company", {"company": company})
    company = normalize_company_name(raw)
    if company != raw:
        return ("research_company", {"company": company})
    return None
