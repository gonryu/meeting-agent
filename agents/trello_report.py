"""Trello 주간 보고서 에이전트

CorpDev_BS 워크스페이스(설정 가능)의 모든 보드를 통합하여 최근 1주간
- 신규 카드
- 코멘트
- 체크리스트 항목 완료
- 다음주 기한(카드 + 체크리스트 항목)

을 수집하여:
1. 상세 본문을 Google Docs로 Drive에 저장
2. 요약을 Slack 관리자 채널로 전송 (Docs 링크 포함)

매주 금요일 21:00 KST 정기 실행 + 관리자 수동 트리거 지원.
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests
import urllib3

from agents import weekly_report_orchestrator
from store import user_store
from tools import drive

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))

# ── 환경변수 ────────────────────────────────────────────────
_WORKSPACE = os.getenv("TRELLO_WORKSPACE", "CorpDev_BS")
_REPORT_CHANNEL = os.getenv("TRELLO_REPORT_CHANNEL") or os.getenv("FEEDBACK_CHANNEL", "")
_REPORT_USER_ID = os.getenv("TRELLO_REPORT_USER_ID", "")
_REPORT_FOLDER_NAME = os.getenv("TRELLO_REPORT_FOLDER_NAME", "Trello 주간 보고서")

# 사내 방화벽 SSL 이슈 대응
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── Trello REST 헬퍼 ────────────────────────────────────────

def _trello_get(path: str, api_key: str, token: str, **params):
    params["key"] = api_key
    params["token"] = token
    url = f"https://api.trello.com/1{path}"
    r = requests.get(url, params=params, verify=False, timeout=30)
    r.raise_for_status()
    return r.json()


def _find_workspace(api_key: str, token: str, name: str) -> dict | None:
    orgs = _trello_get("/members/me/organizations", api_key, token,
                       fields="id,name,displayName")
    target = name.strip().lower()
    for org in orgs:
        if org.get("name", "").lower() == target:
            return org
        if org.get("displayName", "").lower() == target:
            return org
    return None


def _list_boards(api_key: str, token: str, org_id: str) -> list[dict]:
    return _trello_get(
        f"/organizations/{org_id}/boards", api_key, token,
        fields="id,name,url,closed", filter="open",
    )


_ACTION_TYPES = [
    "createCard",
    "commentCard",
    "updateCheckItemStateOnCard",
]


def _fetch_board_actions(api_key: str, token: str, board_id: str,
                         since: datetime) -> list[dict]:
    all_actions: list[dict] = []
    before = None
    since_iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    filter_str = ",".join(_ACTION_TYPES)

    while True:
        params = {"filter": filter_str, "since": since_iso, "limit": 1000}
        if before:
            params["before"] = before
        chunk = _trello_get(f"/boards/{board_id}/actions", api_key, token, **params)
        if not chunk:
            break
        all_actions.extend(chunk)
        if len(chunk) < 1000:
            break
        before = chunk[-1]["id"]
    return all_actions


def _fetch_board_due_items(api_key: str, token: str,
                           board_id: str) -> tuple[list[dict], list[dict]]:
    """미완료 항목 중 due가 설정된 카드 / 체크리스트 항목."""
    try:
        cards = _trello_get(
            f"/boards/{board_id}/cards", api_key, token,
            fields="id,name,shortLink,closed,due,dueComplete",
            checklists="all",
            checklist_fields="name",
            filter="open",
        )
    except Exception as e:
        log.warning(f"[Trello 주간] 카드 조회 실패 board={board_id}: {e}")
        return [], []

    card_dues: list[dict] = []
    item_dues: list[dict] = []
    for card in cards:
        if card.get("closed"):
            continue
        card_url = f"https://trello.com/c/{card.get('shortLink', '')}"
        if card.get("due") and not card.get("dueComplete"):
            card_dues.append({
                "card_id": card["id"],
                "card_name": card["name"],
                "card_url": card_url,
                "due": card["due"],
            })
        for cl in card.get("checklists", []) or []:
            for item in cl.get("checkItems", []) or []:
                if item.get("state") == "complete":
                    continue
                if not item.get("due"):
                    continue
                item_dues.append({
                    "card_id": card["id"],
                    "card_name": card["name"],
                    "card_url": card_url,
                    "checklist_name": cl.get("name", ""),
                    "item_name": item.get("name", ""),
                    "due": item["due"],
                })
    return card_dues, item_dues


# ── 액션 요약 ───────────────────────────────────────────────

def _summarize_action(a: dict) -> dict | None:
    t = a.get("type")
    data = a.get("data", {}) or {}
    card = data.get("card", {}) or {}
    member = a.get("memberCreator", {}) or {}
    card_short = card.get("shortLink") or card.get("id", "")
    base = {
        "type": t,
        "when": a.get("date", ""),
        "actor": member.get("fullName") or member.get("username") or "",
        "card_id": card.get("id", ""),
        "card_name": card.get("name", ""),
        "card_url": f"https://trello.com/c/{card_short}" if card_short else "",
    }
    if t == "createCard":
        base["kind"] = "card_created"
        base["detail"] = (data.get("list") or {}).get("name", "")
        return base
    if t == "commentCard":
        base["kind"] = "comment"
        base["detail"] = (data.get("text") or "").strip()
        return base
    if t == "updateCheckItemStateOnCard":
        item = data.get("checkItem") or {}
        if item.get("state") != "complete":
            return None
        cl = (data.get("checklist") or {}).get("name", "")
        base["kind"] = "checkitem_completed"
        base["detail"] = f"[{cl}] {item.get('name', '')}"
        return base
    return None


# ── 포맷 헬퍼 ───────────────────────────────────────────────

def _fmt_time(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(KST).strftime("%m/%d %H:%M")
    except Exception:
        return ""


def _fmt_date(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone(KST).strftime("%m/%d (%a)")
    except Exception:
        return iso


def _truncate(text: str, limit: int = 220) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _strip_markdown(text: str) -> str:
    """간단한 마크다운 토큰 제거 — Slack 한 줄 미리보기용."""
    import re
    t = text
    # 링크 [텍스트](URL) → 텍스트
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    # 볼드·이탤릭 기호
    t = re.sub(r"[*_#`>\\]", "", t)
    # 여러 공백·개행 정리
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _summarize_long_comments(comments: list[dict],
                             char_threshold: int = 300) -> dict[str, str]:
    """긴 코멘트를 최대 2줄로 요약 (원본 Docs용).

    comments: [{"_uid": str, "detail": str, "card_name": str}, ...]
      _uid: 원본 매핑용 고유 키 (호출부에서 부여)
    Returns: {_uid: "요약 (최대 2줄)"}
    실패 시 빈 딕셔너리.
    """
    targets = [c for c in comments if len(c.get("detail") or "") > char_threshold]
    if not targets:
        return {}
    try:
        from agents.before import generate_text
    except Exception:
        return {}

    import json as _json
    entries = []
    for idx, c in enumerate(targets, start=1):
        entries.append({
            "idx": idx, "uid": c["_uid"],
            "card_name": c.get("card_name", ""),
            "text": _truncate(_strip_markdown(c["detail"]), 1800),
        })
    payload = "\n\n".join(
        f"[{e['idx']}] 카드: {e['card_name']}\n본문: {e['text']}" for e in entries
    )
    prompt = (
        "아래는 Trello 코멘트 전문들입니다. 각 항목을 **한국어 2줄 이내**로 요약해주세요. "
        "핵심 결정사항·합의·다음 단계를 중심으로, 마크다운/이모지 없이 평문으로. "
        "각 줄은 60자 이내로 끊어서 주세요. 줄바꿈은 \\n 으로 표기.\n\n"
        f"{payload}\n\n"
        "JSON 배열로만 반환:\n"
        '[{"idx": 1, "summary": "첫째줄\\n둘째줄"}, ...]'
    )
    try:
        raw = generate_text(prompt).strip()
        cleaned = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
        parsed = _json.loads(cleaned)
    except Exception as e:
        log.warning(f"[Trello 주간] 긴 코멘트 요약 실패, 원문 유지: {e}")
        return {}

    idx_to_uid = {e["idx"]: e["uid"] for e in entries}
    out: dict[str, str] = {}
    if not isinstance(parsed, list):
        return {}
    for item in parsed:
        try:
            idx = int(item.get("idx"))
            summary = (item.get("summary") or "").strip()
        except Exception:
            continue
        uid = idx_to_uid.get(idx)
        if uid and summary:
            # 안전장치 — 2줄로 강제 제한
            lines = [ln.strip() for ln in summary.split("\n") if ln.strip()]
            out[uid] = "\n".join(lines[:2])
    return out


def _summarize_comments_one_liner(by_card: dict[str, list[dict]]) -> dict[str, str]:
    """카드별 코멘트 묶음을 LLM으로 한 줄씩 요약. 실패 시 폴백 빈 딕셔너리.

    by_card: {card_key: [comment_action, ...]}
    Returns: {card_key: "한 줄 요약 (≤40자)"}
    """
    if not by_card:
        return {}
    try:
        from agents.before import generate_text
    except Exception:
        return {}

    # 프롬프트에 넣을 페이로드 구성 (카드별 코멘트 전체 텍스트 concat)
    import json as _json
    entries = []
    for idx, (key, items) in enumerate(by_card.items(), start=1):
        sorted_items = sorted(items, key=lambda x: x["when"])
        combined = " / ".join(_strip_markdown(it["detail"]) for it in sorted_items)
        # 과한 길이 컷 — 토큰 절약
        combined = _truncate(combined, 600)
        entries.append({
            "idx": idx, "key": key,
            "card_name": sorted_items[0]["card_name"],
            "text": combined,
        })

    payload = "\n".join(
        f"{e['idx']}. [{e['card_name']}] {e['text']}" for e in entries
    )
    prompt = (
        "아래는 Trello 카드별로 지난 한 주간 달린 코멘트 모음입니다. "
        "각 번호별로 핵심 진행상황/결정사항을 **한국어 한 줄(40자 이내)**로 요약해주세요. "
        "마크다운/이모지 없이 평문으로, 불필요한 수식어 없이 사실 중심으로.\n\n"
        f"{payload}\n\n"
        "JSON 배열로만 반환:\n"
        '[{"idx": 1, "summary": "..."}, ...]'
    )
    try:
        raw = generate_text(prompt).strip()
        cleaned = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
        parsed = _json.loads(cleaned)
    except Exception as e:
        log.warning(f"[Trello 주간] 코멘트 LLM 요약 실패, 폴백 사용: {e}")
        return {}

    idx_to_key = {e["idx"]: e["key"] for e in entries}
    out: dict[str, str] = {}
    if not isinstance(parsed, list):
        return {}
    for item in parsed:
        try:
            idx = int(item.get("idx"))
            summary = (item.get("summary") or "").strip()
        except Exception:
            continue
        key = idx_to_key.get(idx)
        if key and summary:
            out[key] = summary
    return out


# ── 원본 Markdown 보고서 ────────────────────────────────────

def _build_full_report(
    workspace_name: str, boards: list[dict],
    actions: list[dict],
    upcoming_cards: list[dict], upcoming_items: list[dict],
    since: datetime, until: datetime,
    next_start: datetime, next_end: datetime,
) -> str:
    lines: list[str] = []
    since_kst = since.astimezone(KST)
    until_kst = until.astimezone(KST)
    next_s_kst = next_start.astimezone(KST)
    next_e_kst = next_end.astimezone(KST)

    lines.append(f"# 주간 Trello 업데이트 — {workspace_name}")
    lines.append("")

    new_cards = [a for a in actions if a["kind"] == "card_created"]
    comments = [a for a in actions if a["kind"] == "comment"]
    completed = [a for a in actions if a["kind"] == "checkitem_completed"]
    total_upcoming = len(upcoming_cards) + len(upcoming_items)
    actors = {a["actor"] for a in actions if a.get("actor")}

    lines.append(
        f"- **수집 기간**: {since_kst.strftime('%Y-%m-%d')} ~ "
        f"{until_kst.strftime('%Y-%m-%d')} (KST)"
    )
    lines.append(
        f"- **대상 보드**: {', '.join(b['name'] for b in boards)} "
        f"(총 {len(boards)}개)"
    )
    lines.append(
        f"- **요약**: 신규 카드 {len(new_cards)}건 · 코멘트 {len(comments)}건 · "
        f"완료 항목 {len(completed)}건 · 다음주 기한 {total_upcoming}건 · "
        f"참여 {len(actors)}명"
    )
    lines.append("")

    # 신규 카드
    lines.append(f"## 🆕 신규 카드 ({len(new_cards)})")
    if not new_cards:
        lines.append("_없음_")
    else:
        for a in sorted(new_cards, key=lambda x: x["when"]):
            name = a["card_name"] or "(이름 없음)"
            url = a["card_url"]
            actor = a["actor"]
            detail = a.get("detail", "")
            link = f"[{name}]({url})" if url else name
            meta = f"리스트='{detail}'" if detail else ""
            lines.append(f"- {_fmt_time(a['when'])} · **{link}** — {actor} · {meta}")
    lines.append("")

    # 코멘트
    lines.append(f"## 💬 코멘트 ({len(comments)})")
    if not comments:
        lines.append("_없음_")
        lines.append("")
    else:
        # 긴 코멘트는 LLM으로 2줄 요약 (300자 초과만)
        for i, c in enumerate(comments):
            c["_uid"] = f"c{i}"
        long_summaries = _summarize_long_comments(comments, char_threshold=300)

        by_card = defaultdict(list)
        for a in comments:
            by_card[a["card_id"] or a["card_name"]].append(a)
        card_order = sorted(
            by_card.keys(),
            key=lambda k: max(x["when"] for x in by_card[k]),
            reverse=True,
        )
        for key in card_order:
            items = sorted(by_card[key], key=lambda x: x["when"], reverse=True)
            first = items[0]
            name = first["card_name"] or "(이름 없음)"
            url = first["card_url"]
            link = f"[{name}]({url})" if url else name
            lines.append("")
            lines.append(f"### {link}")
            for a in items:
                lines.append("")
                lines.append(f"**🕐 {_fmt_time(a['when'])} · {a['actor']}**")
                lines.append("")
                uid = a.get("_uid")
                summary = long_summaries.get(uid) if uid else None
                if summary:
                    # 2줄 요약 사용 — 마크다운 깔끔
                    for ln in summary.split("\n"):
                        lines.append(ln)
                    lines.append("_(원문 축약 — 전체 내용은 Trello 카드 참조)_")
                else:
                    # 원문 그대로 — 코멘트 내 개행/목록/굵게 등 마크다운 보존
                    body = a["detail"].rstrip()
                    # 사용자 작성 H3 이상이 카드 섹션(H3)을 덮어쓰지 않게 한 단계 낮춤
                    normalized_lines = []
                    for raw in body.split("\n"):
                        stripped = raw.lstrip()
                        if stripped.startswith("### "):
                            normalized_lines.append(raw.replace("### ", "##### ", 1))
                        elif stripped.startswith("## "):
                            normalized_lines.append(raw.replace("## ", "#### ", 1))
                        elif stripped.startswith("# "):
                            normalized_lines.append(raw.replace("# ", "#### ", 1))
                        else:
                            normalized_lines.append(raw)
                    lines.append("\n".join(normalized_lines))
        lines.append("")

    # 체크리스트 완료
    lines.append(f"## ✅ 체크리스트 항목 완료 ({len(completed)})")
    if not completed:
        lines.append("_없음_")
    else:
        by_card = defaultdict(list)
        for a in completed:
            by_card[a["card_id"] or a["card_name"]].append(a)
        card_order = sorted(
            by_card.keys(),
            key=lambda k: max(x["when"] for x in by_card[k]),
            reverse=True,
        )
        for key in card_order:
            items = sorted(by_card[key], key=lambda x: x["when"])
            first = items[0]
            name = first["card_name"] or "(이름 없음)"
            url = first["card_url"]
            link = f"[{name}]({url})" if url else name
            lines.append(f"- **{link}**")
            for a in items:
                lines.append(f"  - {_fmt_time(a['when'])} · {a['actor']} — {a['detail']}")
    lines.append("")

    # 다음주 기한
    lines.append(
        f"## 📅 다음주 기한 "
        f"({next_s_kst.strftime('%m/%d')} ~ {next_e_kst.strftime('%m/%d')}, "
        f"{total_upcoming})"
    )
    if total_upcoming == 0:
        lines.append("_해당 기간에 기한 설정된 미완료 항목이 없습니다._")
    else:
        by_card: dict[str, dict] = {}
        for c in upcoming_cards:
            by_card.setdefault(c["card_id"], {
                "card_id": c["card_id"], "card_name": c["card_name"],
                "card_url": c["card_url"], "card_due": c["due"], "items": [],
            })["card_due"] = c["due"]
        for it in upcoming_items:
            entry = by_card.setdefault(it["card_id"], {
                "card_id": it["card_id"], "card_name": it["card_name"],
                "card_url": it["card_url"], "card_due": None, "items": [],
            })
            entry["items"].append(it)

        def _min_due(e: dict) -> str:
            dues = []
            if e.get("card_due"):
                dues.append(e["card_due"])
            dues.extend(x["due"] for x in e["items"])
            return min(dues) if dues else ""

        for entry in sorted(by_card.values(), key=_min_due):
            name = entry["card_name"] or "(이름 없음)"
            url = entry["card_url"]
            link = f"[{name}]({url})" if url else name
            header = f"- **{link}**"
            if entry.get("card_due"):
                header += f" · 카드 기한 `{_fmt_date(entry['card_due'])}`"
            lines.append(header)
            for it in sorted(entry["items"], key=lambda x: x["due"]):
                cl = it["checklist_name"]
                prefix = f"[{cl}] " if cl else ""
                lines.append(
                    f"  - `{_fmt_date(it['due'])}` — {prefix}{it['item_name']}"
                )
    lines.append("")
    return "\n".join(lines)


# ── Slack 요약 (mrkdwn) ─────────────────────────────────────

def _build_slack_summary(
    workspace_name: str, boards: list[dict],
    actions: list[dict],
    upcoming_cards: list[dict], upcoming_items: list[dict],
    since: datetime, until: datetime,
    next_start: datetime, next_end: datetime,
    doc_url: str | None,
) -> str:
    since_kst = since.astimezone(KST)
    until_kst = until.astimezone(KST)
    next_s_kst = next_start.astimezone(KST)
    next_e_kst = next_end.astimezone(KST)

    new_cards = [a for a in actions if a["kind"] == "card_created"]
    comments = [a for a in actions if a["kind"] == "comment"]
    completed = [a for a in actions if a["kind"] == "checkitem_completed"]
    total_upcoming = len(upcoming_cards) + len(upcoming_items)
    actors = {a["actor"] for a in actions if a.get("actor")}

    lines: list[str] = []
    lines.append(f"*📊 주간 Trello 업데이트 — {workspace_name}*")
    lines.append(
        f"_{since_kst.strftime('%Y-%m-%d')} ~ {until_kst.strftime('%Y-%m-%d')} · "
        f"신규 {len(new_cards)} · 코멘트 {len(comments)} · "
        f"완료 {len(completed)} · 다음주 기한 {total_upcoming} · "
        f"참여 {len(actors)}명_"
    )
    if doc_url:
        lines.append(f"📄 <{doc_url}|전체 보고서 Google Docs>")
    lines.append("")

    # 신규 카드
    lines.append(f"*🆕 신규 카드 ({len(new_cards)})*")
    if not new_cards:
        lines.append("_없음_")
    else:
        for a in sorted(new_cards, key=lambda x: x["when"]):
            name = a["card_name"] or "(이름 없음)"
            url = a["card_url"]
            link = f"<{url}|{name}>" if url else name
            detail = a.get("detail", "")
            meta = f" · `{detail}`" if detail else ""
            lines.append(f"• {_fmt_time(a['when'])} — {link}{meta} · {a['actor']}")
    lines.append("")

    # 코멘트 — 카드별 한 줄 요약 (LLM)
    lines.append(f"*💬 코멘트 ({len(comments)})*")
    if not comments:
        lines.append("_없음_")
    else:
        by_card = defaultdict(list)
        for a in comments:
            by_card[a["card_id"] or a["card_name"]].append(a)
        card_order = sorted(
            by_card.keys(),
            key=lambda k: max(x["when"] for x in by_card[k]),
            reverse=True,
        )
        summaries = _summarize_comments_one_liner(
            {k: by_card[k] for k in card_order}
        )
        for key in card_order:
            items = sorted(by_card[key], key=lambda x: x["when"], reverse=True)
            first = items[0]
            name = first["card_name"] or "(이름 없음)"
            url = first["card_url"]
            link = f"<{url}|{name}>" if url else name
            count = len(items)
            actors_s = ", ".join(sorted({x["actor"] for x in items if x["actor"]}))
            # LLM 요약 우선, 실패 시 원문 축약 폴백
            summary = summaries.get(key) or _truncate(
                _strip_markdown(first["detail"]), 60
            )
            suffix = f" ({count}건)" if count > 1 else ""
            lines.append(f"• {link}{suffix} — {actors_s}: {summary}")
    lines.append("")

    # 체크리스트 완료
    lines.append(f"*✅ 체크리스트 항목 완료 ({len(completed)})*")
    if not completed:
        lines.append("_없음_")
    else:
        by_card = defaultdict(list)
        for a in completed:
            by_card[a["card_id"] or a["card_name"]].append(a)
        card_order = sorted(
            by_card.keys(),
            key=lambda k: max(x["when"] for x in by_card[k]),
            reverse=True,
        )
        for key in card_order:
            items = sorted(by_card[key], key=lambda x: x["when"])
            first = items[0]
            name = first["card_name"] or "(이름 없음)"
            url = first["card_url"]
            link = f"<{url}|{name}>" if url else name
            # 항목 이름만 뽑아서 나열 (세부 체크리스트명 제외해 짧게)
            titles = []
            for a in items:
                detail = a["detail"]
                # "[체크리스트명] 항목명" → 항목명만
                if detail.startswith("[") and "] " in detail:
                    detail = detail.split("] ", 1)[1]
                titles.append(detail)
            joined = ", ".join(titles)
            lines.append(f"• {link} — {_truncate(joined, 150)}")
    lines.append("")

    # 다음주 기한
    lines.append(
        f"*📅 다음주 기한 ({next_s_kst.strftime('%m/%d')} ~ "
        f"{next_e_kst.strftime('%m/%d')}, {total_upcoming})*"
    )
    if total_upcoming == 0:
        lines.append("_해당 기간에 기한 설정된 미완료 항목이 없습니다._")
    else:
        by_card: dict[str, dict] = {}
        for c in upcoming_cards:
            by_card.setdefault(c["card_id"], {
                "card_id": c["card_id"], "card_name": c["card_name"],
                "card_url": c["card_url"], "card_due": c["due"], "items": [],
            })["card_due"] = c["due"]
        for it in upcoming_items:
            entry = by_card.setdefault(it["card_id"], {
                "card_id": it["card_id"], "card_name": it["card_name"],
                "card_url": it["card_url"], "card_due": None, "items": [],
            })
            entry["items"].append(it)

        def _min_due(e: dict) -> str:
            dues = []
            if e.get("card_due"):
                dues.append(e["card_due"])
            dues.extend(x["due"] for x in e["items"])
            return min(dues) if dues else ""

        for entry in sorted(by_card.values(), key=_min_due):
            name = entry["card_name"] or "(이름 없음)"
            url = entry["card_url"]
            link = f"<{url}|{name}>" if url else name
            parts = []
            if entry.get("card_due"):
                parts.append(f"카드 `{_fmt_date(entry['card_due'])}`")
            for it in sorted(entry["items"], key=lambda x: x["due"]):
                parts.append(f"`{_fmt_date(it['due'])}` {it['item_name']}")
            lines.append(f"• {link} — {', '.join(parts)}")

    return "\n".join(lines)


# ── 오케스트레이터 결과를 Slack용 텍스트로 변환 ────────────────


def _build_enriched_slack_text(
    *, workspace_name: str,
    since: datetime, until: datetime,
    executive_summary_md: str,
    risks: dict,
    doc_url: str | None,
) -> str:
    """오케스트레이터의 5줄 요약 + 핵심 리스크 카드를 Slack mrkdwn으로."""
    since_kst = since.astimezone(KST)
    until_kst = until.astimezone(KST)

    lines: list[str] = []
    lines.append(f"*📊 주간 Trello 업데이트 — {workspace_name}*")
    lines.append(
        f"_{since_kst.strftime('%Y-%m-%d')} ~ {until_kst.strftime('%Y-%m-%d')} (KST)_"
    )
    if doc_url:
        lines.append(f"📄 <{doc_url}|상세 보고서 Google Docs>")
    lines.append("")
    summary = (executive_summary_md or "").strip()
    if summary:
        lines.append(summary)
        lines.append("")

    # 리스크 강조 — delayed/at_risk가 있으면 카드 링크와 함께 노출
    delayed = risks.get("delayed") or []
    at_risk = risks.get("at_risk") or []
    if delayed or at_risk:
        lines.append("*🚨 리스크 카드*")
        for r in delayed[:5]:
            name = r.get("card_name") or "(이름 없음)"
            url = r.get("card_url") or ""
            link = f"<{url}|{name}>" if url else name
            due = r.get("due") or ""
            reason = (r.get("reason") or "").strip()
            suffix = f" — 기한 `{due}`" if due else ""
            if reason:
                suffix += f" · {reason}"
            lines.append(f"• 🚨 {link}{suffix}")
        for r in at_risk[:5]:
            name = r.get("card_name") or "(이름 없음)"
            url = r.get("card_url") or ""
            link = f"<{url}|{name}>" if url else name
            reason = (r.get("reason") or r.get("signal") or "").strip()
            suffix = f" — {reason}" if reason else ""
            lines.append(f"• ⚠️ {link}{suffix}")

    return "\n".join(lines).rstrip()


# ── 수집 + 집계 ─────────────────────────────────────────────

def _collect(api_key: str, token: str, workspace: str, days: int):
    org = _find_workspace(api_key, token, workspace)
    if not org:
        raise RuntimeError(f"Trello 워크스페이스 '{workspace}' 찾기 실패")
    boards = _list_boards(api_key, token, org["id"])

    until = datetime.now(timezone.utc)
    since = until - timedelta(days=days)
    next_start = until
    next_end = until + timedelta(days=days)

    def _in_next_week(iso: str) -> bool:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except Exception:
            return False
        return next_start <= dt <= next_end

    all_actions: list[dict] = []
    up_cards: list[dict] = []
    up_items: list[dict] = []
    for b in boards:
        raw = _fetch_board_actions(api_key, token, b["id"], since)
        refined = [r for r in (_summarize_action(a) for a in raw) if r]
        all_actions.extend(refined)

        card_dues, item_dues = _fetch_board_due_items(api_key, token, b["id"])
        up_cards.extend(c for c in card_dues if _in_next_week(c["due"]))
        up_items.extend(i for i in item_dues if _in_next_week(i["due"]))

    return {
        "workspace_name": org["displayName"],
        "boards": boards,
        "actions": all_actions,
        "upcoming_cards": up_cards,
        "upcoming_items": up_items,
        "since": since,
        "until": until,
        "next_start": next_start,
        "next_end": next_end,
    }


# ── Drive 저장 ──────────────────────────────────────────────

def _ensure_report_folder(creds, contacts_folder_id: str,
                          folder_name: str) -> str:
    """contacts 폴더의 부모 위치에 보고서 폴더 확보."""
    svc = drive._service(creds)
    try:
        parent_resp = svc.files().get(
            fileId=contacts_folder_id, fields="parents"
        ).execute()
        root_id = parent_resp.get("parents", [None])[0]
    except Exception:
        root_id = None
    return drive.create_folder(creds, folder_name, root_id)


def _save_to_drive(user_id: str, title: str, content_md: str) -> tuple[str, str]:
    """Google Docs로 저장. Returns: (doc_id, web_url)"""
    creds = user_store.get_credentials(user_id)
    user = user_store.get_user(user_id)
    contacts_folder_id = user.get("contacts_folder_id")
    if not contacts_folder_id:
        raise RuntimeError(f"사용자 {user_id} Drive 폴더 미등록")
    folder_id = _ensure_report_folder(creds, contacts_folder_id, _REPORT_FOLDER_NAME)
    doc_id = drive.create_draft_doc(creds, title, content_md, folder_id)
    url = f"https://docs.google.com/document/d/{doc_id}/edit"
    return doc_id, url


# ── 사용자 결정 ─────────────────────────────────────────────

def _resolve_report_user() -> str | None:
    """보고서 생성에 사용할 Slack user_id 결정.
    우선순위: TRELLO_REPORT_USER_ID > 첫 번째 토큰 보유자
    """
    if _REPORT_USER_ID:
        return _REPORT_USER_ID
    for row in user_store.all_users():
        uid = row["slack_user_id"]
        if user_store.get_trello_token(uid):
            return uid
    return None


# ── 공개 API ───────────────────────────────────────────────

def generate_report(user_id: str | None = None, days: int = 7,
                    workspace: str | None = None) -> dict:
    """보고서 원본/요약 생성만. Drive 저장·Slack 발송 없음."""
    user_id = user_id or _resolve_report_user()
    if not user_id:
        raise RuntimeError("Trello 보고서 생성 사용자 미지정 (토큰 보유자 없음)")

    api_key = os.getenv("TRELLO_API_KEY", "")
    if not api_key:
        raise RuntimeError("TRELLO_API_KEY 환경변수 미설정")
    token = user_store.get_trello_token(user_id)
    if not token:
        raise RuntimeError(f"Trello 토큰 미등록: {user_id}")

    data = _collect(api_key, token, workspace or _WORKSPACE, days)

    full_md = _build_full_report(
        data["workspace_name"], data["boards"],
        data["actions"], data["upcoming_cards"], data["upcoming_items"],
        data["since"], data["until"], data["next_start"], data["next_end"],
    )
    return {
        "user_id": user_id,
        "full_md": full_md,
        "data": data,
    }


def send_weekly_report(slack_client, user_id: str | None = None,
                       channel: str | None = None,
                       thread_ts: str | None = None,
                       days: int = 7,
                       workspace: str | None = None) -> dict:
    """보고서 생성 → Drive 저장 → Slack 발송. Returns: 결과 요약.

    - channel 지정 시 그 채널로, 미지정 시 `_REPORT_CHANNEL` 폴백
    - thread_ts 지정 시 스레드 답글로 발송
    """
    built = generate_report(user_id=user_id, days=days, workspace=workspace)
    user_id = built["user_id"]
    data = built["data"]

    title_date = data["until"].astimezone(KST).strftime("%Y-%m-%d")
    title = f"{title_date} 주간 Trello 보고서 — {data['workspace_name']}"

    # 오케스트레이터로 본문·Slack 요약 강화 (실패 시 기존 경로로 폴백)
    enriched: dict | None = None
    if weekly_report_orchestrator.is_enabled():
        try:
            enriched = weekly_report_orchestrator.enrich_report(
                data, base_report_md=built["full_md"],
            )
            log.info("[Trello 주간] orchestrator 사용 — 본문·요약 강화")
        except Exception as e:
            log.exception(f"[Trello 주간] Weekly Report Orchestrator 실패 — 폴백: {e}")
            enriched = None

    docs_md = enriched["detailed_md"] if enriched else built["full_md"]

    doc_url = None
    try:
        _, doc_url = _save_to_drive(user_id, title, docs_md)
        log.info(f"[Trello 주간] Google Docs 저장 완료: {doc_url}")
    except Exception as e:
        log.exception(f"[Trello 주간] Google Docs 저장 실패: {e}")

    if enriched:
        # Slack 발송 텍스트 = 5줄 요약 + Docs 링크 + (있으면) 리스크 강조
        slack_text = _build_enriched_slack_text(
            workspace_name=data["workspace_name"],
            since=data["since"], until=data["until"],
            executive_summary_md=enriched["executive_summary_md"],
            risks=enriched.get("risks", {}),
            doc_url=doc_url,
        )
    else:
        slack_text = _build_slack_summary(
            data["workspace_name"], data["boards"],
            data["actions"], data["upcoming_cards"], data["upcoming_items"],
            data["since"], data["until"], data["next_start"], data["next_end"],
            doc_url=doc_url,
        )

    target = channel or _REPORT_CHANNEL
    posted = False
    if not target:
        log.warning(
            "[Trello 주간] TRELLO_REPORT_CHANNEL/FEEDBACK_CHANNEL 미설정 — Slack 발송 건너뜀"
        )
    else:
        try:
            kwargs = {
                "channel": target, "text": slack_text,
                "unfurl_links": False, "unfurl_media": False,
            }
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
            slack_client.chat_postMessage(**kwargs)
            posted = True
            log.info(f"[Trello 주간] Slack 발송 완료 → {target}"
                     f"{' (thread)' if thread_ts else ''}")
        except Exception as e:
            log.exception(f"[Trello 주간] Slack 발송 실패: {e}")

    return {
        "doc_url": doc_url,
        "posted": posted,
        "channel": target,
        "actions": len(data["actions"]),
        "upcoming": len(data["upcoming_cards"]) + len(data["upcoming_items"]),
    }
