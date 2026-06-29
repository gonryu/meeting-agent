"""에이전트형 리서치 엔진 — Claude tool-use 다중홉 + critic 3종 (온디맨드 v1).
설계: docs/superpowers/specs/2026-06-29-agentic-research-engine-design.md"""
import json
import logging
import os
from dataclasses import dataclass

import anthropic

from tools import drive, gmail, trello, ontology, slack_read
from agents.research_types import CompanyResearch, NewsItem, SourceDoc, Attendee

log = logging.getLogger(__name__)
_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_MODEL = "claude-sonnet-4-5"
_HAIKU = "claude-haiku-4-5"
_KEY_SOURCES = {"gmail_search", "drive_search"}   # 최소 들러야 할 핵심 소스


def agentic_enabled() -> bool:
    return os.getenv("AGENTIC_RESEARCH", "false").lower() == "true"


@dataclass
class ToolContext:
    user_id: str
    creds: object
    slack_client: object = None
    folder_id: str = ""


def run_agentic_research(*, company_name: str, user_id: str, creds, slack_client=None,
                         meeting_context: str = "") -> "CompanyResearch | None":
    """에이전트 리서치. 성공 시 CompanyResearch, 실패/미완 시 None(호출부 폴백)."""
    folder_id = os.getenv("DRIVE_RESEARCH_FOLDER_ID", "")
    ctx = ToolContext(user_id=user_id, creds=creds, slack_client=slack_client, folder_id=folder_id)
    try:
        return _agent_loop(company_name, meeting_context, ctx)
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
        {"name": "ontology_lookup", "description": "사내 온톨로지 엔티티·문서",
         "input_schema": s(name={"type": "string"})},
        {"name": "submit_research", "description": "리서치 완료 — 구조화 결과 제출",
         "input_schema": _SUBMIT_SCHEMA},
    ]


def _dispatch(name: str, args: dict, ctx: ToolContext) -> str:
    try:
        if name == "gmail_search":
            return json.dumps(gmail.search_recent_emails(ctx.creds, args.get("query", ""), args.get("query", "")), ensure_ascii=False, default=str)
        if name == "gmail_read_thread":
            return json.dumps(gmail.read_thread(ctx.creds, args.get("thread_id", "")), ensure_ascii=False, default=str)
        if name == "drive_search":
            return json.dumps(drive.search_files(ctx.creds, args.get("query", ""), folder_id=ctx.folder_id), ensure_ascii=False, default=str)
        if name == "drive_read":
            return drive.read_file_text(ctx.creds, args.get("file_id", ""), args.get("mime_type", ""), args.get("name", ""))
        if name == "slack_channel_history":
            return json.dumps(slack_read.channel_history(ctx.slack_client, args.get("channel", "")), ensure_ascii=False, default=str)
        if name == "trello_lookup":
            return json.dumps(trello.get_card_context(ctx.user_id, args.get("company", ""), limit_comments=3) or {}, ensure_ascii=False, default=str)
        if name == "web_search":
            from agents import before
            return before._search(args.get("query", ""))
        if name == "ontology_lookup":
            return json.dumps(ontology.company_context(ctx.user_id, args.get("name", ""), recent=True) or {}, ensure_ascii=False, default=str)
        return f"unknown tool: {name}"
    except Exception as e:
        log.warning(f"도구 {name} 실패: {e}")
        return f"(도구 {name} 실패: {str(e)[:120]})"


_MAX_ROUNDS = int(os.getenv("AGENTIC_MAX_ROUNDS", "12"))

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
5. 충분히 모았으면 submit_research를 호출하라. 모든 주장에 가능한 한 출처를 남겨라."""


def _initial_prompt(company_name: str, meeting_context: str) -> str:
    ctx = f"\n\n미팅 맥락:\n{meeting_context}" if meeting_context else ""
    return f"'{company_name}'에 대해 파라메타 미팅 사전 리서치를 수행하라.{ctx}"


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
    keep = _url_grounding_keep(r)
    r.news = [n for i, n in enumerate(r.news) if i in keep]
    return r


def _run_critics(r: CompanyResearch, ctx, called: set) -> CompanyResearch:
    """①URL 그라운딩(Haiku) 적용. ②동명타사=capable 모델이 submit의 company_identity_confirmed로
    책임(스키마 required). ③커버리지 부족은 v1에선 로그 관측."""
    r = _apply_url_grounding(r)
    if _coverage_gap(called):
        log.info(f"[AGENTIC] 커버리지 부족(들른 소스={called}) — {r.company_name}")
    return r


def _agent_loop(company_name: str, meeting_context: str, ctx: "ToolContext"):
    tools = _tool_specs()
    messages = [{"role": "user", "content": _initial_prompt(company_name, meeting_context)}]
    called: set = set()
    for _round in range(_MAX_ROUNDS):
        resp = _claude.messages.create(model=_MODEL, max_tokens=4096, system=_SYSTEM,
                                       tools=tools, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if not tool_uses:
            break
        results = []
        submit_input = None
        for tu in tool_uses:
            called.add(tu.name)
            if tu.name == "submit_research":
                submit_input = tu.input
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": "접수됨"})
            else:
                out = _dispatch(tu.name, tu.input, ctx)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": (out or "")[:8000]})
        messages.append({"role": "user", "content": results})
        if submit_input is not None:
            research = _to_company_research(submit_input, company_name)
            return _run_critics(research, ctx, called)
    return None


def _to_company_research(d: dict, company_name: str) -> CompanyResearch:
    return CompanyResearch(
        company_name=company_name,
        summary_line=d.get("summary_line", ""),
        deal_context=d.get("deal_context", ""),
        news=[NewsItem(title=n.get("title", ""), summary=n.get("summary", ""),
                       url=n.get("url") or None, source=n.get("source", ""))
              for n in (d.get("news") or [])],
        connections=list(d.get("connections") or []),
        source_docs=[SourceDoc(title=s.get("title", ""), url=s.get("url", ""), why=s.get("why", ""))
                     for s in (d.get("source_docs") or [])],
        attendees=[Attendee(name=a.get("name", ""), role=a.get("role", ""),
                            contact=a.get("contact", ""), note=a.get("note", ""))
                   for a in (d.get("attendees") or [])],
        talking_points=list(d.get("talking_points") or []),
    )
