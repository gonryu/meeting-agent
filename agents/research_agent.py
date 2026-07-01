"""에이전트형 리서치 엔진 — Claude tool-use 다중홉 + critic 3종 (온디맨드 v1).
설계: docs/superpowers/specs/2026-06-29-agentic-research-engine-design.md"""
import json
import logging
import os
import re
import time
from dataclasses import dataclass

import anthropic

from tools import drive, gmail, trello, ontology, slack_read
from agents.research_types import CompanyResearch, NewsItem, SourceDoc, Attendee

log = logging.getLogger(__name__)
_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_MODEL = "claude-sonnet-5"        # 에이전트 실행/합성 — most agentic Sonnet(완주·자체검증), Opus 근접·저가
_HAIKU = "claude-haiku-4-5"       # critic(URL그라운딩·동일성) — 기계적이라 저가 유지
_KEY_SOURCES = {"gmail_search", "drive_search"}   # 최소 들러야 할 핵심 소스


def agentic_enabled() -> bool:
    return os.getenv("AGENTIC_RESEARCH", "false").lower() == "true"


@dataclass
class ToolContext:
    user_id: str
    creds: object
    slack_client: object = None
    folder_id: str = ""


def _ontology_brief(user_id: str, company_name: str) -> str:
    """게이팅된 사용자(온톨로지 토큰)면 회사 사내 맥락(관계·문서목록)을 결정론적으로 확보.
    에이전트가 회사를 직접 조회하든 말든 보장 주입. 미게이팅/실패/빈결과 시 빈 문자열."""
    try:
        from agents import before
        if not before._ontology_enabled(user_id):
            return ""
        ctx = ontology.company_context(user_id, company_name, recent=True)
        if not ctx:
            return ""
        rels = ctx.get("relations") or []
        docs = ctx.get("documents") or []
        lines = []
        if rels:
            lines.append("관계: " + ", ".join(
                f"{r.get('relation')}={r.get('title')}" for r in rels[:10] if r.get("title")))
        if docs:
            lines.append("사내 문서(관련되면 ontology_doc_fetch(document_id)로 본문 확인):")
            for d in docs[:8]:
                if d.get("title"):
                    lines.append(f"  - document_id={d.get('id')} | {d.get('title')}")
        return "\n".join(lines).strip()
    except Exception as e:
        log.warning(f"온톨로지 브리프 실패({company_name}): {e}")
        return ""


def run_agentic_research(*, company_name: str, user_id: str, creds, slack_client=None,
                         meeting_context: str = "") -> "CompanyResearch | None":
    """에이전트 리서치. 성공 시 CompanyResearch, 실패/미완 시 None(호출부 폴백)."""
    folder_id = os.getenv("DRIVE_RESEARCH_FOLDER_ID", "")
    ctx = ToolContext(user_id=user_id, creds=creds, slack_client=slack_client, folder_id=folder_id)
    onto_ctx = _ontology_brief(user_id, company_name)
    if onto_ctx:
        log.info(f"[AGENTIC] {company_name} 온톨로지 맥락 주입 ({len(onto_ctx)}자)")
    try:
        return _agent_loop(company_name, meeting_context, ctx, ontology_context=onto_ctx)
    except Exception as e:
        log.exception(f"에이전트 리서치 실패, 폴백 ({company_name}): {e}")
        return None


_SUBMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_line": {"type": "string"},
        "company_identity_confirmed": {"type": "string",
            "description": "이 회사가 누구인지 확정(동명 타사 배제 근거). 예: 'komsa=한국해양교통안전공단, 독일 KOMSA AG 아님'"},
        "deal_context": {"type": "string"},
        "news": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "summary": {"type": "string"},
            "url": {"type": "string"}, "source": {"type": "string"}}}},
        "connections": {"type": "array", "items": {"type": "string"}},
        "source_docs": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "url": {"type": "string"}, "why": {"type": "string"}}}},
        "attendees": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "role": {"type": "string"},
            "contact": {"type": "string"}, "note": {"type": "string"}}}},
        "talking_points": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary_line", "company_identity_confirmed"],
}


def _tool_specs() -> list[dict]:
    s = lambda **p: {"type": "object", "properties": p}
    return [
        {"name": "gmail_search", "description": "회사·인물명으로 메일 검색(헤더·snippet)",
         "input_schema": s(query={"type": "string"})},
        {"name": "gmail_read_thread", "description": "스레드 본문 읽기(거래 흐름·수치)",
         "input_schema": s(thread_id={"type": "string"})},
        {"name": "drive_search", "description": "영업/제안 공유폴더+본인+공유받은 문서 검색",
         "input_schema": s(query={"type": "string"})},
        {"name": "drive_read", "description": "파일 본문 추출(PDF·hwpx·docx·xlsx)",
         "input_schema": s(file_id={"type": "string"}, mime_type={"type": "string"}, name={"type": "string"})},
        {"name": "slack_channel_history", "description": "biz 채널 최근 논의(자유서술)",
         "input_schema": s(channel={"type": "string"})},
        {"name": "trello_lookup", "description": "업체 파이프라인 카드(체크리스트·코멘트)",
         "input_schema": s(company={"type": "string"})},
        {"name": "web_search", "description": "외부 최근 동향 웹 검색",
         "input_schema": s(query={"type": "string"})},
        {"name": "ontology_lookup", "description": "사내 온톨로지 엔티티·문서(회사명으로 조회)",
         "input_schema": s(name={"type": "string"})},
        {"name": "ontology_doc_fetch", "description": "사내 온톨로지 문서 본문 조회(주입된 document_id)",
         "input_schema": s(document_id={"type": "string"})},
        {"name": "submit_research", "description": "리서치 완료 — 구조화 결과 제출",
         "input_schema": _SUBMIT_SCHEMA},
    ]


def _dispatch(name: str, args: dict, ctx: ToolContext) -> str:
    """도구 실행 + 계측(소요시간·출력크기 로깅). 어떤 도구가 시간을 먹는지 진단용."""
    t0 = time.monotonic()
    try:
        out = _dispatch_call(name, args, ctx)
    except Exception as e:
        log.warning(f"[AGENTIC] tool {name} 실패 {int((time.monotonic()-t0)*1000)}ms: {e}")
        return f"(도구 {name} 실패: {str(e)[:120]})"
    log.info(f"[AGENTIC] tool {name}({str(args)[:60]}) {int((time.monotonic()-t0)*1000)}ms → {len(out or '')}자")
    return out


def _dispatch_call(name: str, args: dict, ctx: ToolContext) -> str:
    if True:
        if name == "gmail_search":
            return json.dumps(gmail.search_recent_emails(ctx.creds, args.get("query", ""), args.get("query", "")), ensure_ascii=False, default=str)
        if name == "gmail_read_thread":
            return json.dumps(gmail.read_thread(ctx.creds, args.get("thread_id", "")), ensure_ascii=False, default=str)
        if name == "drive_search":
            return json.dumps(drive.search_files(ctx.creds, args.get("query", ""), folder_id=ctx.folder_id), ensure_ascii=False, default=str)
        if name == "drive_read":
            return drive.read_file_text(ctx.creds, args.get("file_id", ""), args.get("mime_type", ""), args.get("name", ""))
        if name == "slack_channel_history":
            return json.dumps(slack_read.channel_history(ctx.slack_client, args.get("channel", ""), requesting_user_id=ctx.user_id), ensure_ascii=False, default=str)
        if name == "trello_lookup":
            return json.dumps(trello.get_card_context(ctx.user_id, args.get("company", ""), limit_comments=3) or {}, ensure_ascii=False, default=str)
        if name == "web_search":
            from agents import before
            query = args.get("query", "")
            # before._search()는 검색 결과 블록(URL 포함)을 버리고 모델 프로즈만 반환한다.
            # 프로즈에 URL을 안 적으면 news[].url이 비어 _url_grounding_keep(Haiku)이
            # "출처 없음"으로 판정해 전체를 기각 → 업체동향 항상 공백(회귀 #62/#64류).
            # 검색 프롬프트에 URL 표기를 명시적으로 요구해 프로즈 안에 URL이 남게 한다.
            prompt = (f"{query}\n\n답변의 각 사실/항목마다 실제 출처 URL을 괄호로 표기하라 "
                      f"(예: '...설명... (https://example.com/...)'). 출처 URL이 없는 항목은 쓰지 마라.")
            return before._search(prompt)
        if name == "ontology_lookup":
            return json.dumps(ontology.company_context(ctx.user_id, args.get("name", ""), recent=True) or {}, ensure_ascii=False, default=str)
        if name == "ontology_doc_fetch":
            return json.dumps(ontology.document_fetch(ctx.user_id, args.get("document_id", "")) or {}, ensure_ascii=False, default=str)
        return f"unknown tool: {name}"


_MAX_ROUNDS = int(os.getenv("AGENTIC_MAX_ROUNDS", "6"))
_TIMEOUT_S = int(os.getenv("AGENTIC_TIMEOUT_S", "120"))

_SYSTEM = """당신은 파라메타(parametacorp) 사업개발 리서치 에이전트다. 목표: '제대로'(풍부+정확).
파라메타 사업분야: 블록체인(loopchain), 디지털자산·STO·RWA, DID/MyID, 결제·금융 인프라,
공공·국가 블록체인(K-BTF), 보안·인증(CSAP)·AI보안, 핀테크, 규제 대응.

원칙:
1. 다중홉: 한 도구 결과(파일명·회사명·thread_id)를 다음 검색 쿼리에 적극 사용하라.
   예) 제목→gmail_search→스레드의 견적서 파일명→그 이름으로 drive_search→drive_read.
2. 여러 소스를 교차로 확인하라. Gmail만 보고 끝내지 말 것 — Drive(견적/제안/deck)·Trello·
   (내부/biz 미팅이면) slack_channel_history·web을 관련되면 반드시 들러라.
3. 동명 타사 주의: 회사 동일성을 확정하라(예: komsa=한국해양교통안전공단 vs 독일 KOMSA AG).
4. talking_points는 수집이 아니라 조합 — 전체 맥락에서 미팅 논의 포인트를 도출하라.
5. **효율 우선 — 과탐색 금지**: 미팅 준비에 *충분한* 핵심 정보를 모았으면(보통 2~3라운드) **즉시 submit_research**하라. 모든 각도를 다 검색하려 하지 마라. web_search는 정말 필요한 1~2개만. 완벽보다 미팅에 쓸 핵심(동향·연결점·이전맥락·논의포인트)이 채워졌는지로 판단하라.
6. deal_context·talking_points의 모든 수치·금액·날짜는 네가 실제로 읽은 출처(Gmail/Drive/web)에 있는 값만 사용하라. 추정·창작 금지. 출처 없는 숫자는 쓰지 마라.
7. 섹션은 역할이 다르다 — **같은 사실을 여러 섹션에 반복하지 마라**(가장 적합한 한 곳에만):
   · news(동향) = 상대 회사의 *외부* 최근 활동(우리와 무관한 자체 움직임).
   · deal_context(거래 맥락) = *우리(파라메타)와의* 진행 이력/현황. **가독성 필수 — 배경/현재단계/다음액션처럼 논리적 전환마다 줄바꿈(\\n)으로 문장을 나눠라. 하나의 긴 문단으로 몰아쓰지 마라.** 3~5개 문장, 문장마다 개행.
   · connections(연결점) = 우리 서비스↔상대 니즈 매핑. 항목당 한 줄, 짧게.
   · talking_points = 오늘 미팅에서 *할 말·물을 것*(액션). **각 1~2문장으로 짧게, 5개 이내.** 한 항목에 여러 포인트를 넣지 말 것(문단·하위불릿 금지).
8. source_docs.url은 gmail_search/drive_search 결과에 포함된 "url" 필드를 그대로 옮겨 써라(직접 만들어내지 마라). url이 없는 항목은 url을 빈 문자열로 두라.
9. 모든 필드는 순수 텍스트만 담아라. `<tag>`, `<parameter ...>`, `<invoke ...>` 같은 XML/함수호출 문법을 필드 값 안에 절대 쓰지 마라 — summarize한 프로즈만 써라."""


def _initial_prompt(company_name: str, meeting_context: str, ontology_context: str = "",
                    channels: list = None) -> str:
    ctx = f"\n\n미팅 맥락:\n{meeting_context}" if meeting_context else ""
    chans = channels if channels is not None else slack_read.biz_channel_list()
    chan_hint = ""
    if chans:
        listed = ", ".join(f"{c['id']}({c['name']})" if c["name"] else c["id"] for c in chans)
        chan_hint = (f"\n\n내부/biz 미팅이면 slack_channel_history로 관련 채널을 확인하라. "
                     f"사용 가능 채널: {listed}")
    onto = (f"\n\n[사내 온톨로지 맥락 — 반드시 검토. 관련 문서는 ontology_doc_fetch로 본문 확인]\n{ontology_context}"
            if ontology_context else "")
    return f"'{company_name}'에 대해 파라메타 미팅 사전 리서치를 수행하라.{ctx}{chan_hint}{onto}"


def _coverage_gap(called: set) -> bool:
    """핵심 소스(gmail/drive)를 안 들렀으면 커버리지 부족(조기종료 의심)."""
    return not _KEY_SOURCES.issubset(called)


def _url_grounding_keep(r: CompanyResearch) -> set:
    """Haiku 기계적 패스: news 각 항목이 출처(url/source)에 근거하는지 → 유지 인덱스 집합."""
    items = [f"{i}. {n.title} | url={n.url or ''} src={n.source or ''}" for i, n in enumerate(r.news)]
    if not items:
        return set()
    prompt = ("아래 뉴스 항목 중 **출처(url 또는 src)가 실재하는** 항목의 번호만 JSON으로.\n"
              '형식: {"keep":[0,2]}\n\n' + "\n".join(items))
    try:
        resp = _claude.messages.create(model=_HAIKU, max_tokens=256,
                                       messages=[{"role": "user", "content": prompt}])
        raw = resp.content[0].text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return {int(i) for i in json.loads(raw).get("keep", [])}
    except Exception as e:
        log.warning(f"url grounding 실패, 전체 유지: {e}")
        return set(range(len(r.news)))


def _apply_url_grounding(r: CompanyResearch) -> CompanyResearch:
    """critic이 전체를 기각하면(keep 빈 집합인데 원본은 비어있지 않음) 과탐지로 간주해 원본을
    유지한다 — 회귀 #62/#64와 같은 '업체동향 항상 정보 없음' 증상 방지(완전 침묵보다 낫다)."""
    if not r.news:
        return r
    keep = _url_grounding_keep(r)
    if not keep:
        log.warning(f"[AGENTIC] url grounding이 뉴스 {len(r.news)}건 전부 기각 — 과탐지 의심, 원본 유지 ({r.company_name})")
        return r
    r.news = [n for i, n in enumerate(r.news) if i in keep]
    return r


def _collect_domains(r) -> list:
    """참석자 이메일·출처 URL에서 도메인 수집(동명타사 교차검증용)."""
    doms = set()
    for a in r.attendees:
        m = re.search(r"@([\w.-]+)", a.contact or "")
        if m:
            doms.add(m.group(1).lower())
    for d in list(r.source_docs) + list(r.news):
        url = getattr(d, "url", "") or ""
        m = re.search(r"https?://([^/\s]+)", url)
        if m:
            doms.add(m.group(1).lower())
    return sorted(doms)


def _identity_consistent(company: str, identity_claim: str, domains: list) -> bool:
    """관찰된 도메인이 주장한 회사 동일성과 모순되지 않는지(Haiku). 실패 시 True(관대)."""
    try:
        prompt = (f"회사: {company}\n동일성 주장: {identity_claim}\n관찰된 도메인: {', '.join(domains)}\n"
                  "오직 도메인이 **명백히 다른 나라·다른 업종의 동명(同名) 타사**임을 가리킬 때만 false. "
                  "다음은 모두 true(일치)로 본다: 회사명 변형(예: 신한증권=신한투자증권, 미래에셋=미래에셋증권), "
                  "자회사·계열사, 내부 프로젝트/협업명, 도메인 정보 부족·무관. 의심스러우면 true. "
                  '코드펜스 없이 JSON만: {"consistent": true}')
        resp = _claude.messages.create(model=_HAIKU, max_tokens=128,
                                       messages=[{"role": "user", "content": prompt}])
        raw = resp.content[0].text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return bool(json.loads(raw).get("consistent", True))
    except Exception as e:
        log.warning(f"동일성 검증 실패, 통과: {e}")
        return True


def _run_critics(r, ctx, called, identity_claim: str = ""):
    """①URL 그라운딩(Haiku) 적용. ②동명타사=submit의 company_identity_confirmed를
    관찰 도메인으로 교차검증(Haiku, 모순 시 caveat). ③커버리지 부족은 로그 관측."""
    r = _apply_url_grounding(r)
    if _coverage_gap(called):
        log.info(f"[AGENTIC] 커버리지 부족(들른 소스={called}) — {r.company_name}")
    domains = _collect_domains(r)
    if domains and identity_claim and not _identity_consistent(r.company_name, identity_claim, domains):
        log.warning(f"[AGENTIC] 회사 동일성 의심 — {r.company_name}: 도메인={domains} vs 주장='{identity_claim}'")
        caveat = "⚠️ 회사 동일성 확인 필요"
        r.summary_line = f"{caveat} — {r.summary_line}" if r.summary_line else caveat
    return r


def _agent_loop(company_name: str, meeting_context: str, ctx: "ToolContext",
                ontology_context: str = ""):
    tools = _tool_specs()
    channels = (slack_read.biz_channels_resolved(ctx.slack_client) if ctx.slack_client
                else slack_read.biz_channel_list())
    messages = [{"role": "user",
                 "content": _initial_prompt(company_name, meeting_context, ontology_context, channels)}]
    called: set = set()
    nudged = False
    start = time.monotonic()
    for _round in range(_MAX_ROUNDS):
        elapsed = int(time.monotonic() - start)
        if elapsed > _TIMEOUT_S:
            log.warning(f"[AGENTIC] 타임아웃({_TIMEOUT_S}s, R{_round+1}) → 폴백 ({company_name}) 누적도구={sorted(called)}")
            return None
        # 마지막 라운드 또는 예산 70% 도달 시 submit 강제 — 타임아웃 폴백 대신 반드시 결과 산출
        force_submit = (_round >= _MAX_ROUNDS - 1) or (elapsed > _TIMEOUT_S * 0.7)
        tool_choice = ({"type": "tool", "name": "submit_research"} if force_submit
                       else {"type": "auto"})
        if force_submit:
            log.info(f"[AGENTIC] {company_name} R{_round+1} submit 강제(force, elapsed={elapsed}s)")
        t_llm = time.monotonic()
        resp = _claude.messages.create(model=_MODEL, max_tokens=4096, system=_SYSTEM,
                                       tools=tools, tool_choice=tool_choice, messages=messages)
        log.info(f"[AGENTIC] {company_name} R{_round+1} (elapsed={elapsed}s) "
                 f"LLM {int((time.monotonic()-t_llm)*1000)}ms stop={getattr(resp, 'stop_reason', '?')}")
        messages.append({"role": "assistant", "content": resp.content})
        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            log.info(f"[AGENTIC] {company_name} R{_round+1} tool_use 없음(end_turn) → submit 없이 폴백")
            break
        log.info(f"[AGENTIC] {company_name} R{_round+1} 도구={[tu.name for tu in tool_uses]}")
        results = []
        submit_input = None
        for tu in tool_uses:
            called.add(tu.name)
            if tu.name == "submit_research":
                if _coverage_gap(called) and not nudged and not force_submit:
                    nudged = True
                    results.append({"type": "tool_result", "tool_use_id": tu.id,
                        "content": "아직 Gmail/Drive를 확인하지 않았다. 관련 메일·문서를 먼저 검색·교차확인한 뒤 다시 submit_research를 호출하라."})
                else:
                    submit_input = tu.input
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "접수됨"})
            else:
                out = _dispatch(tu.name, tu.input, ctx)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": (out or "")[:8000]})
        messages.append({"role": "user", "content": results})
        if submit_input is not None:
            log.info(f"[AGENTIC] {company_name} submit 수락 "
                     f"(R{_round+1}, elapsed={int(time.monotonic()-start)}s, 누적도구={sorted(called)})")
            research = _to_company_research(submit_input, company_name)
            return _run_critics(research, ctx, called, submit_input.get("company_identity_confirmed", ""))
        elif nudged and "submit_research" in {tu.name for tu in tool_uses}:
            log.info(f"[AGENTIC] {company_name} R{_round+1} submit 시도→커버리지 nudge 반려")
    return None


def _as_list(v) -> list:
    """리스트면 그대로, 문자열이면 [문자열], 그 외 빈 리스트. 모델이 배열 대신 문자열을
    반환했을 때 list('블록체인')→['블','록','체','인'] 문자폭발 방지."""
    if isinstance(v, list):
        return v
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def _as_str_list(v) -> list:
    return [_sanitize_field_text(str(x).strip()) for x in _as_list(v) if str(x).strip()]


# submit_research 스키마 필드명 + 클래식 tool-call XML 태그. 모델이 자신의 이전 턴
# tool_use 블록(대화 히스토리에 남은 넛지 반려분 등)을 자유 텍스트 필드 안에 그대로
# 흘렸을 때(예: "...</deal_context><parameter name=\"news\">[...]") 방어적으로 잘라낸다.
_LEAK_TAG_RE = re.compile(
    r"</?(?:summary_line|company_identity_confirmed|deal_context|news|connections|"
    r"source_docs|attendees|talking_points|invoke|parameter|function_calls)\b[^>]*>",
    re.IGNORECASE,
)


def _sanitize_field_text(text: str) -> str:
    """자유 텍스트 필드에서 tool-call류 XML 잔여물을 발견하면 그 지점에서 잘라낸다."""
    if not text:
        return text
    m = _LEAK_TAG_RE.search(text)
    if m:
        text = text[:m.start()].rstrip()
    return text


def _to_company_research(d: dict, company_name: str) -> CompanyResearch:
    return CompanyResearch(
        company_name=company_name,
        summary_line=_sanitize_field_text(str(d.get("summary_line", ""))),
        deal_context=_sanitize_field_text(str(d.get("deal_context", ""))),
        news=[NewsItem(title=_sanitize_field_text(n.get("title", "")),
                       summary=_sanitize_field_text(n.get("summary", "")),
                       url=n.get("url") or None, source=n.get("source", ""))
              for n in _as_list(d.get("news")) if isinstance(n, dict)],
        connections=_as_str_list(d.get("connections")),
        source_docs=[SourceDoc(title=_sanitize_field_text(s.get("title", "")), url=s.get("url", ""),
                               why=_sanitize_field_text(s.get("why", "")))
                     for s in _as_list(d.get("source_docs")) if isinstance(s, dict)],
        attendees=[Attendee(name=a.get("name", ""), role=a.get("role", ""),
                            contact=a.get("contact", ""),
                            note=_sanitize_field_text(a.get("note", "")))
                   for a in _as_list(d.get("attendees")) if isinstance(a, dict)],
        talking_points=_as_str_list(d.get("talking_points")),
    )
