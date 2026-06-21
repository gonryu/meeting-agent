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
         "id": d.get("id") or d.get("document_id")}
        for d in docs
    ]
    return {
        "seed": slug,
        "relations": relations[:20],
        "documents": doclist[:10],
        "entity_count": len(ents),
        "document_count": len(docs),
    }
