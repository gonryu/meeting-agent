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


def agentic_enabled() -> bool:
    return os.getenv("AGENTIC_RESEARCH", "false").lower() == "true"


@dataclass
class ToolContext:
    user_id: str
    creds: object
    slack_client: object = None
    folder_id: str = ""


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
