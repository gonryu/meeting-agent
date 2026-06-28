"""Deterministic public-news RSS fallback for company research.

Claude web_search can return empty/no-URL output even when public news exists.
This module uses Google News RSS search as a no-key fallback and returns the
same markdown bullet shape consumed by `parse_trend_bullets`.
"""
from __future__ import annotations

import html
import logging
import re
from urllib.parse import quote_plus
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from agents import company_profile

log = logging.getLogger(__name__)


def _clean_title(title: str, source: str = "") -> str:
    title = html.unescape((title or "").strip())
    source = html.unescape((source or "").strip())
    suffix = f" - {source}"
    if source and title.endswith(suffix):
        title = title[: -len(suffix)].strip()
    # Google News RSS often preserves publisher section labels in the title.
    # They add noise in Slack and can look broken after markdown/link handling.
    title = re.sub(r"^\[[^\]]{1,30}\]\s*", "", title).strip()
    title = re.sub(r"^[^]\n]{1,30}\]\s*", "", title).strip()
    return title


def _fetch_rss(query: str, timeout: float = 8.0) -> bytes:
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=ko&gl=KR&ceid=KR:ko"
    )
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    resp = urlopen(req, timeout=timeout)
    try:
        return resp.read()
    finally:
        close = getattr(resp, "close", None)
        if callable(close):
            close()


def search_company_news(company_name: str, max_items: int = 5) -> str:
    """Search Google News RSS and return markdown bullets with URLs."""
    queries = company_profile.search_queries(company_name, max_queries=6)
    if not queries:
        return ""

    bullets: list[str] = []
    seen: set[str] = set()
    for query in queries:
        try:
            data = _fetch_rss(query)
            root = ET.fromstring(data)
        except Exception as e:
            log.warning(f"Google News RSS 검색 실패 ({company_name}, {query}): {e}")
            continue

        for item in root.findall(".//item"):
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            source_el = item.find("source")
            source = source_el.text if source_el is not None and source_el.text else ""
            title = _clean_title(title, source)
            link = html.unescape(link.strip())
            if not title or not link.startswith("http"):
                continue
            key = (title.lower(), link)
            if key in seen:
                continue
            seen.add(key)
            summary = f"Google News RSS{f' / {source}' if source else ''}"
            bullets.append(f"- **[{title}]**: {summary} ({link})")
            if len(bullets) >= max_items:
                return "\n".join(bullets)
    return "\n".join(bullets)
