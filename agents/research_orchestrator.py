"""Research Orchestrator — 업체·인물 리서치 다중 에이전트 파이프라인

harness-100 #44 (market-research)·#63 (research-assistant) 패턴을 본 서비스의
브리핑 리서치에 맞춰 Python 프롬프트 체인으로 적용.

업체 리서치 파이프라인:
  1. industry_context (Haiku)        ┐
  2. competitor_landscape (Haiku)    ├─ 병렬 실행
  3. trend_signals (Haiku + web)     ┘
  4. synthesis (Sonnet)              — 최종 마크다운 조립

인물 리서치 파이프라인:
  1. profile_collector (Haiku + web) ┐
  2. email_context_summarizer (Haiku)├─ 병렬 실행
  3. synthesis (Sonnet)              — 최종 마크다운 조립

실패 시 폴백: 호출부에서 기존 단일 호출 경로로 자동 전환.
환경변수 RESEARCH_ORCHESTRATOR_ENABLED=false 로 비활성화 가능.
"""
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import anthropic

log = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent.parent / "prompts" / "templates" / "research"

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_HAIKU = "claude-haiku-4-5"
_SONNET = "claude-sonnet-4-5"

_SYSTEM_PROMPT = """\
당신은 회의 브리핑용 리서치 다중 에이전트 시스템의 일부입니다. 다음 원칙을 반드시 준수하세요:

1. **사실 기반**: 입력된 자료·검색 결과에 실제로 존재하는 정보만 활용합니다. 유추·추론·창작 금지.
2. **출처 보존**: 검색에서 얻은 URL과 출처는 그대로 보존합니다.
3. **JSON 출력 시 형식 엄수**: JSON만 출력하라고 지시받은 경우, 코드펜스·설명 없이 순수 JSON 객체만 출력합니다.
4. **빈 결과 허용**: 정보가 없으면 빈 배열·빈 문자열로 두세요. 채우려고 추측하지 마세요.
5. **불명확 처리**: 확인되지 않은 부분은 명시적으로 빈 값으로 두거나 "공개 정보 없음" 등 정해진 문구를 사용합니다.
"""


# ── 공용 헬퍼 ─────────────────────────────────────────────────


def _load_template(*parts: str) -> str:
    return (_TEMPLATES_DIR.joinpath(*parts)).read_text(encoding="utf-8")


def _render(template: str, **vars) -> str:
    out = template
    for k, v in vars.items():
        out = out.replace("{{" + k + "}}", str(v) if v is not None else "")
    return out


def _call_llm(prompt: str, *, model: str, max_tokens: int = 2048) -> str:
    msg = _claude.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _call_llm_with_search(prompt: str, *, model: str = _HAIKU,
                           max_tokens: int = 2048) -> str:
    """웹 검색 포함 LLM 호출 — Claude web_search 베타 도구 사용.

    before.py의 _search() 동작과 동일한 형태로 호출하여 검색 텍스트만 반환.
    실패 시 일반 호출로 폴백.
    """
    try:
        resp = _claude.beta.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=_SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{"role": "user", "content": prompt}],
            betas=["web-search-2025-03-05"],
        )
        return "\n".join(
            block.text for block in resp.content if hasattr(block, "text")
        ).strip()
    except Exception as e:
        log.warning(f"web_search 호출 실패, 일반 호출로 폴백: {e}")
        return _call_llm(prompt, model=model, max_tokens=max_tokens)


def _parse_json(text: str) -> dict:
    """LLM 응답에서 JSON을 안전하게 추출. 코드펜스가 섞여 와도 처리."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    return json.loads(text)


def is_enabled() -> bool:
    """환경변수로 리서치 오케스트레이터 활성/비활성 토글"""
    return os.getenv("RESEARCH_ORCHESTRATOR_ENABLED", "true").lower() != "false"


# ── 업체 리서치 단계 ──────────────────────────────────────────


def _company_industry(company_name: str, today: str, knowledge_md: str) -> dict:
    template = _load_template("company", "industry_context.md")
    prompt = _render(template, company_name=company_name, today=today,
                     knowledge=knowledge_md or "(없음)")
    raw = _call_llm(prompt, model=_HAIKU, max_tokens=1024)
    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        log.warning(f"industry_context JSON 파싱 실패: {e} / 원문: {raw[:200]}")
        return {"industry": "", "value_proposition": "",
                "regulation_notes": [], "maturity": "unknown"}


def _company_competitors(company_name: str, today: str) -> dict:
    template = _load_template("company", "competitor_landscape.md")
    prompt = _render(template, company_name=company_name, today=today)
    raw = _call_llm(prompt, model=_HAIKU, max_tokens=1024)
    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        log.warning(f"competitor_landscape JSON 파싱 실패: {e} / 원문: {raw[:200]}")
        return {"peers": [], "positioning": "", "differentiators": []}


def _company_trends(company_name: str, today: str) -> str:
    from agents import company_profile
    template = _load_template("company", "trend_signals.md")
    prompt = _render(
        template,
        company_name=company_name,
        today=today,
        search_context=company_profile.trend_search_context(company_name),
    )
    return _call_llm_with_search(prompt, model=_HAIKU, max_tokens=2048)


def _company_synthesis(*, company_name: str, today: str,
                        industry: dict, competitor: dict,
                        trend_md: str, gmail_context: str) -> str:
    template = _load_template("company", "synthesis.md")
    prompt = _render(
        template,
        company_name=company_name,
        today=today,
        industry_json=json.dumps(industry, ensure_ascii=False, indent=2),
        competitor_json=json.dumps(competitor, ensure_ascii=False, indent=2),
        trend_md=trend_md or "최근 공개된 정보 없음",
        gmail_context=gmail_context or "(없음)",
    )
    return _call_llm(prompt, model=_SONNET, max_tokens=3072)


def run_company_research(*, company_name: str, knowledge_md: str = "",
                          gmail_context: str = "", user_id: str = "",
                          creds=None, slack_client=None, allow_agent: bool = False) -> "CompanyResearch":
    """업체 리서치 다단계 파이프라인. Returns: CompanyResearch 구조화 객체.

    호출부(before.py.research_company)는 research_types.render_company_news_block()로
    `## 최근 동향` 본문 문자열을 만들어 기존 news_text 자리에 삽입한다(전환기 단계1).
    Raises: 단계 실패 시. 호출부가 캐치하여 기존 단일 호출 경로로 폴백.
    """
    from agents import company_profile
    from agents.research_types import CompanyResearch, parse_trend_bullets
    # 에이전트 플래그 ON + 자격 있으면 위임, 실패/미완 시 레거시 폴백
    try:
        from agents import research_agent
        # v1: 에이전트는 온디맨드(allow_agent=True)만 — 브리핑은 레거시 유지(비용·rich 미렌더)
        if research_agent.agentic_enabled() and allow_agent and user_id and creds is not None:
            agent_out = research_agent.run_agentic_research(
                company_name=company_name, user_id=user_id, creds=creds,
                slack_client=slack_client, meeting_context=gmail_context)
            if agent_out is not None:
                return agent_out
            log.info(f"에이전트 미완 → 레거시 폴백 ({company_name})")
    except Exception as e:
        log.warning(f"에이전트 경로 오류, 레거시 폴백 ({company_name}): {e}")
    company_name = company_profile.normalize_company_name(company_name)
    log.info(f"Research Orchestrator (company) 시작: {company_name}")
    t0 = datetime.now()
    today = datetime.now().strftime("%Y-%m-%d")
    assisted = ""
    try:
        from agents import research_assist
        assisted = research_assist.assisted_knowledge(company_name)
        if assisted:
            knowledge_md = "\n\n".join(part for part in (knowledge_md, assisted) if part)
            log.info(f"  assisted public sources 주입: {company_name} ({len(assisted):,}자)")
    except Exception as e:
        log.warning(f"assisted public sources 주입 실패, 기본 리서치 계속 ({company_name}): {e}")

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_industry = pool.submit(_company_industry, company_name, today, knowledge_md)
        f_compet = pool.submit(_company_competitors, company_name, today)
        f_trend = pool.submit(_company_trends, company_name, today)
        industry = f_industry.result()
        competitor = f_compet.result()
        trend_md = f_trend.result()

    t1 = datetime.now()
    log.info(
        f"  [1-3/4] 병렬 단계 완료 ({(t1-t0).total_seconds():.1f}s, "
        f"industry={bool(industry.get('industry'))}, "
        f"peers={len(competitor.get('peers', []))}, "
        f"trend_chars={len(trend_md)})"
    )

    # 동향 = '파라메타 사업분야 렌즈로 본 이 회사의 최근 활동 + 미팅 포인트'.
    # 단계3: trend_md를 NewsItem으로 파싱(URL 포함) → judge로 도메인 필터(재작성 없이
    # keep/drop만 → URL 구조적 보존). _trend_relevance(재작성 판정, URL 유실)를 대체.
    # KISA 보안체계·AI보안=유지, K-브랜드/IP·인사·시세=제외(trend_judge.md).
    from agents import news_relevance
    raw_news_items = [n for n in parse_trend_bullets("\n".join((trend_md, assisted))) if n.url]
    if trend_md.strip() and not raw_news_items:
        log.info(f"  trend_signals URL 포함 항목 없음 — 뉴스 제외 ({company_name})")
    if not raw_news_items:
        try:
            from agents import news_rss
            rss_md = news_rss.search_company_news(company_name)
            raw_news_items = [n for n in parse_trend_bullets(rss_md) if n.url]
            if raw_news_items:
                log.info(f"  Google News RSS fallback 후보 확보 ({company_name}): {len(raw_news_items)}건")
        except Exception as e:
            log.warning(f"Google News RSS fallback 실패 ({company_name}): {e}")
    news_items = news_relevance.judge(raw_news_items, company_name)
    if raw_news_items and not news_items:
        log.info(f"  judge가 URL 후보를 모두 제외 — 상위 후보 보존 ({company_name}): {len(raw_news_items)}건")
        for item in raw_news_items[:3]:
            item.relevance = item.relevance or "mid"
        news_items = raw_news_items[:3]
    # synthesis 개요는 판정 통과 동향을 컨텍스트로(시세/무관 항목 제외, URL 불필요)
    judged_trend_md = "\n".join(
        f"- **[{n.title}]**: {n.summary}".rstrip() for n in news_items
    ) or "최근 공개된 정보 없음"

    overview_md = _company_synthesis(
        company_name=company_name, today=today,
        industry=industry, competitor=competitor,
        trend_md=judged_trend_md, gmail_context=gmail_context,
    )
    # 개요(Sonnet 합성)는 표시 전용 마크다운으로 보존 — 재파싱하지 않는다.
    research = CompanyResearch(
        company_name=company_name,
        overview=overview_md,
        news=news_items,
        searched_at=today,
    )
    t2 = datetime.now()
    log.info(
        f"  [4/4] synthesis 완료 ({(t2-t1).total_seconds():.1f}s) "
        f"— 총 {(t2-t0).total_seconds():.1f}s, "
        f"overview={len(overview_md):,}자, news={len(research.news)}건"
    )
    return research


# ── 인물 리서치 단계 ──────────────────────────────────────────


def _person_profile(person_name: str, company_name: str) -> str:
    template = _load_template("person", "profile_collector.md")
    prompt = _render(template, person_name=person_name, company_name=company_name)
    return _call_llm_with_search(prompt, model=_HAIKU, max_tokens=2048)


def _person_email_context(person_name: str, company_name: str,
                           gmail_context: str) -> dict:
    template = _load_template("person", "email_context_summarizer.md")
    prompt = _render(
        template,
        person_name=person_name,
        company_name=company_name,
        email_lines=gmail_context or "(없음)",
    )
    raw = _call_llm(prompt, model=_HAIKU, max_tokens=1024)
    try:
        return _parse_json(raw)
    except json.JSONDecodeError as e:
        log.warning(f"email_context_summarizer JSON 파싱 실패: {e} / 원문: {raw[:200]}")
        return {"recent_topics": [], "open_threads": [], "tone": ""}


def _person_synthesis(*, person_name: str, company_name: str, today: str,
                       profile_md: str, email_context: dict) -> str:
    template = _load_template("person", "synthesis.md")
    prompt = _render(
        template,
        person_name=person_name,
        company_name=company_name,
        today=today,
        profile_md=profile_md or "공개 정보 없음",
        email_context_json=json.dumps(email_context, ensure_ascii=False, indent=2),
    )
    return _call_llm(prompt, model=_SONNET, max_tokens=2048)


def run_person_research(*, person_name: str, company_name: str,
                         gmail_context: str = "") -> str:
    """인물 리서치 다단계 파이프라인. Returns: `## 공개 정보` 섹션 본문 마크다운.

    호출부(before.py.research_person)는 이 결과를 기존 `info_text` 자리에 삽입한다.
    Raises: 단계 실패 시. 호출부가 캐치하여 기존 단일 호출 경로로 폴백.
    """
    log.info(f"Research Orchestrator (person) 시작: {person_name} ({company_name})")
    t0 = datetime.now()
    today = datetime.now().strftime("%Y-%m-%d")

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_profile = pool.submit(_person_profile, person_name, company_name)
        f_email = pool.submit(
            _person_email_context, person_name, company_name, gmail_context,
        )
        profile_md = f_profile.result()
        email_ctx = f_email.result()

    t1 = datetime.now()
    log.info(
        f"  [1-2/3] 병렬 단계 완료 ({(t1-t0).total_seconds():.1f}s, "
        f"profile_chars={len(profile_md)}, "
        f"topics={len(email_ctx.get('recent_topics', []))})"
    )

    final_md = _person_synthesis(
        person_name=person_name, company_name=company_name, today=today,
        profile_md=profile_md, email_context=email_ctx,
    )
    t2 = datetime.now()
    log.info(
        f"  [3/3] synthesis 완료 ({(t2-t1).total_seconds():.1f}s) "
        f"— 총 {(t2-t0).total_seconds():.1f}s, {len(final_md):,}자"
    )
    return final_md
