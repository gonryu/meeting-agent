"""사내 온톨로지(lib-mesh) MCP read 클라이언트 — Streamable-HTTP JSON-RPC.

/mcp/ 직타(트레일링 슬래시 필수 — /mcp는 307이고 리다이렉트 추종 시 Authorization 드롭).
읽기 전용: entity_find / entity_cluster / document_* → 우리 하네스가 합성.
"""
import json
import logging
import os
import re
from datetime import datetime

import httpx

from store import user_store

log = logging.getLogger(__name__)

DEFAULT_URL = os.getenv("ONTOLOGY_MCP_URL", "https://ont.parametacorp.com/mcp/")
_PROTOCOL = "2025-06-18"
_TIMEOUT = float(os.getenv("ONTOLOGY_TIMEOUT", "40"))
_JWT_RE = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")


class OntologyAuthError(Exception):
    """토큰 만료/무효(HTTP 401)."""


def extract_bearer_token(config_text: str) -> str | None:
    """ont 'MCP 설정' 붙여넣기에서 Bearer JWT 추출. JSON이면 Authorization 헤더,
    아니면 원시 텍스트에서 eyJ... 패턴."""
    if not config_text or not config_text.strip():
        return None
    txt = config_text.strip()
    try:
        data = json.loads(txt)
        found = {}

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if str(k).lower() == "authorization" and isinstance(v, str):
                        found["auth"] = v
                    walk(v)
            elif isinstance(o, list):
                for x in o:
                    walk(x)

        walk(data)
        if found.get("auth"):
            m = _JWT_RE.search(found["auth"])
            if m:
                return m.group(0)
    except Exception:
        pass
    m = _JWT_RE.search(txt)
    return m.group(0) if m else None


def _endpoint(url: str = None) -> str:
    u = url or DEFAULT_URL
    return u if u.endswith("/") else u + "/"


def _recent_range(months: int = 6) -> list[str]:
    """['YYYY-MM','YYYY-MM'] — 현재월 기준 과거 N개월(변동층 time_range용)."""
    now = datetime.now()
    y, m = now.year, now.month
    fm, fy = m - months, y
    while fm <= 0:
        fm += 12
        fy -= 1
    return [f"{fy:04d}-{fm:02d}", f"{y:04d}-{m:02d}"]


def _best_slug(find_result) -> str | None:
    """entity_find data → 최선 slug (exact > confidence > importance)."""
    matches = (find_result or {}).get("matches", []) if isinstance(find_result, dict) else []
    if not matches:
        return None
    matches = sorted(
        matches,
        key=lambda mm: (mm.get("match_kind") == "exact", mm.get("confidence", 0), mm.get("importance", 0)),
        reverse=True,
    )
    return matches[0].get("slug")


def _normalize_cluster(cluster, slug) -> dict:
    """entity_cluster data → {seed, relations[], documents[], entity_count, document_count}."""
    data = cluster if isinstance(cluster, dict) else {}
    ents = data.get("entities", []) or []
    docs = data.get("documents", []) or []
    relations = []
    for e in ents:
        via = e.get("via")
        if via and e.get("slug") != slug:
            relations.append({"relation": via, "title": e.get("title") or e.get("slug")})
    doclist = [
        {"title": d.get("title") or d.get("name") or d.get("id"),
         "id": d.get("document_id") or d.get("id"),
         "uri": d.get("source_uri") or d.get("sourceUrl") or "",
         "space": d.get("space_display") or d.get("space") or "",
         "ym": d.get("ym") or "",
         "matched": d.get("matched_via_entities") or []}
        for d in docs
    ]
    return {
        "seed": slug,
        "relations": relations[:20],
        "documents": doclist[:10],
        "entity_count": len(ents),
        "document_count": len(docs),
    }


class OntologyClient:
    """초기화 1회 후 tools/call 재사용. `with` 블록 권장. /mcp/ 직타(리다이렉트 미추종)."""

    def __init__(self, token: str, url: str = None, timeout: float = _TIMEOUT):
        self.url = _endpoint(url)
        self.token = token
        self._http = httpx.Client(timeout=timeout, follow_redirects=False)
        self._sid = None
        self._inited = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def close(self):
        try:
            self._http.close()
        except Exception:
            pass

    def _headers(self) -> dict:
        tok = self.token if self.token.lower().startswith("bearer ") else f"Bearer {self.token}"
        h = {
            "Authorization": tok,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _PROTOCOL,
        }
        if self._sid:
            h["Mcp-Session-Id"] = self._sid
        return h

    def _post(self, payload: dict):
        r = self._http.post(self.url, json=payload, headers=self._headers())
        sid = r.headers.get("mcp-session-id")
        if sid:
            self._sid = sid
        if r.status_code == 401:
            raise OntologyAuthError("ontology 401 unauthorized")
        return r

    @staticmethod
    def _parse(r):
        ct = r.headers.get("content-type", "") or ""
        if "event-stream" in ct:
            for line in r.text.splitlines():
                if line.startswith("data:"):
                    try:
                        m = json.loads(line[5:].strip())
                        if "result" in m or "error" in m:
                            return m
                    except Exception:
                        pass
            return None
        try:
            return r.json()
        except Exception:
            return None

    def _ensure_init(self):
        if self._inited:
            return
        r = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": _PROTOCOL, "capabilities": {},
                                   "clientInfo": {"name": "meeting-agent", "version": "1.0"}}})
        if r.status_code != 200:
            raise RuntimeError(f"ontology initialize 실패: HTTP {r.status_code}")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self._inited = True

    def call_tool(self, name: str, arguments: dict):
        """tools/call → result.content[].text의 `data` 봉투(JSON 파싱) 반환."""
        self._ensure_init()
        r = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": name, "arguments": arguments}})
        msg = self._parse(r)
        if not msg or "result" not in msg:
            raise RuntimeError(f"ontology {name} 실패: "
                               f"{json.dumps(msg, ensure_ascii=False)[:200] if msg else 'no result'}")
        blocks = msg["result"].get("content", []) or []
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        sc = msg["result"].get("structuredContent")
        if sc is not None:
            return sc
        try:
            parsed = json.loads(text)
            return parsed.get("data", parsed) if isinstance(parsed, dict) else parsed
        except Exception:
            return text

    def validate(self) -> bool:
        """등록 시 토큰 유효성: initialize 성공이면 True, 401이면 OntologyAuthError."""
        self._ensure_init()
        return True


def company_context(user_id: str, company_name: str, recent: bool = False) -> dict | None:
    """업체명 → entity_find → entity_cluster → 정규화 dict. 토큰 없으면 None.
    OntologyAuthError는 그대로 올림(호출부가 만료 처리). seed 없으면 빈 구조."""
    token = user_store.get_ontology_token(user_id)
    if not token:
        return None
    with OntologyClient(token) as oc:
        find = oc.call_tool("entity_find", {"name": company_name, "limit": 5})
        slug = _best_slug(find)
        if not slug:
            return {"seed": None, "relations": [], "documents": [], "entity_count": 0, "document_count": 0}
        args = {"seed": slug, "depth": 2, "include_documents": True,
                "limit_entities": 40, "limit_documents": 15}
        if recent:
            args["time_range"] = _recent_range()
        cluster = oc.call_tool("entity_cluster", args)
        return _normalize_cluster(cluster, slug)


def document_fetch(user_id: str, document_id: str, level: str = "summary",
                   max_chars: int = 3000) -> dict | None:
    """문서 요약/본문 가져오기. {title, summary, uri, space}. 토큰 없으면 None."""
    token = user_store.get_ontology_token(user_id)
    if not token:
        return None
    with OntologyClient(token) as oc:
        data = oc.call_tool("document_fetch", {
            "document_id": document_id, "level": level, "max_chars": max_chars})
    if not isinstance(data, dict):
        return {"title": "", "summary": str(data or ""), "uri": "", "space": ""}
    fm = data.get("frontmatter") or {}
    return {
        "title": data.get("title") or fm.get("title") or "",
        "summary": (data.get("body_markdown") or "").strip(),
        "uri": data.get("source_uri") or fm.get("sourceUrl") or "",
        "space": fm.get("space_display") or fm.get("space") or "",
    }


_MEETING_RE = re.compile(r"(회의|미팅|interview|회의록|월간업무보고|간담회|워크숍|workshop)", re.I)
_MEETING_DATE_RE = re.compile(r"\d{4}[-.\s]?\d{1,2}")


def _is_meeting_title(title: str) -> bool:
    """엔티티 제목이 '미팅/회의'성인지 — 키워드 또는 날짜 패턴."""
    t = title or ""
    return bool(_MEETING_RE.search(t) or _MEETING_DATE_RE.search(t))


def person_context(user_id: str, person_name: str) -> dict | None:
    """인물명 → entity_find → entity_cluster → 미팅이력 추출.
    토큰 없으면 None. OntologyAuthError는 호출부로 올림. seed 없으면 빈 구조.
    Returns: {seed, meetings[], sources_count}"""
    token = user_store.get_ontology_token(user_id)
    if not token:
        return None
    with OntologyClient(token) as oc:
        find = oc.call_tool("entity_find", {"name": person_name, "limit": 3})
        slug = _best_slug(find)
        if not slug:
            return {"seed": None, "meetings": [], "sources_count": 0}
        sources = 0
        for m in (find or {}).get("matches", []):
            if m.get("slug") == slug:
                sources = m.get("sources_count", 0)
                break
        cluster = oc.call_tool("entity_cluster", {
            "seed": slug, "depth": 1, "include_documents": False, "limit_entities": 60})
        ents = cluster.get("entities", []) if isinstance(cluster, dict) else []
        meetings = [e.get("title") for e in ents
                    if e.get("via") and _is_meeting_title(e.get("title", ""))]
        return {"seed": slug, "meetings": meetings[:6], "sources_count": sources}


# 문서 우선순위: 제안서·계약·회의록 > 발표 > 주간보고 (낮을수록 우선)
def _doc_priority(title: str) -> int:
    t = (title or "")
    if any(k in t for k in ("제안서", "계약", "회의록", "RFP")):
        return 0
    if any(k in t for k in ("발표", "Proposal", "구성도", "설계")):
        return 1
    return 2


def company_research_sources(user_id: str, company_name: str, max_docs: int = 6) -> dict | None:
    """딥 리서치 입력 — entity_find→cluster→R1 필터→상위문서 document_fetch.
    토큰 없으면 None. Returns: {seed, relations[], docs:[{title,summary,uri,space,ym,id}]}.
    R1: 업체 엔티티에 직접 연결된 문서(matched_via_entities에 seed 포함)만."""
    token = user_store.get_ontology_token(user_id)
    if not token:
        return None
    with OntologyClient(token) as oc:
        find = oc.call_tool("entity_find", {"name": company_name, "limit": 5})
        slug = _best_slug(find)
        if not slug:
            return {"seed": None, "relations": [], "docs": []}
        cluster = oc.call_tool("entity_cluster", {
            "seed": slug, "depth": 2, "include_documents": True,
            "limit_entities": 40, "limit_documents": 30, "time_range": _recent_range(12)})
    norm = _normalize_cluster(cluster, slug)
    # R1: 업체 직접 연결 문서만 (matched에 seed 포함)
    connected = [d for d in norm["documents"] if slug in (d.get("matched") or [])]
    pool = connected or norm["documents"]  # 연결문서 0이면 전체에서라도
    pool = sorted(pool, key=lambda d: (_doc_priority(d.get("title", "")),
                                       -_ym_key(d.get("ym", ""))))[:max_docs]
    docs = []
    for d in pool:
        if not d.get("id"):
            continue
        fetched = None
        try:
            fetched = document_fetch(user_id, d["id"])
        except Exception as fe:
            log.warning(f"document_fetch 실패({d.get('title')}): {fe}")
        docs.append({**d, "summary": (fetched or {}).get("summary", ""),
                     "uri": d.get("uri") or (fetched or {}).get("uri", "")})
    return {"seed": slug, "relations": norm["relations"], "docs": docs}


def _ym_key(ym: str) -> int:
    """'2026-05' → 202605 (정렬용). 빈값 0."""
    try:
        return int((ym or "").replace("-", "")[:6] or 0)
    except Exception:
        return 0
