"""Todo (할일) 에이전트 — 개인 Todo 추가·조회·완료/취소/삭제·수정.

설계 원칙:
- DB(`store/user_store.py::todos` + `todo_history`)가 단일 진실.
- Drive `MeetingAgent/Todos/오픈루프.md`(라이브) + `이력.md`(append-only)는 미러.
- 카테고리: work(기본) | personal | ai
- 마감일 색상/이모지 (FR-T6):
    🔴 긴급  due <= today
    🟠 임박  today < due <= today+2
    🟡 주의  today+2 < due <= today+7
    ⚪ 일반  그 외 또는 due 없음
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from store import user_store
from tools import drive as _drive
from agents.before import generate_text, _post

log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "prompts", "templates")
_PARSE_TEMPLATE_PATH = os.path.join(_TEMPLATES_DIR, "todo_parse.md")

_VALID_CATEGORIES = {"work", "personal", "ai"}
_CATEGORY_LABELS = {
    "work": "업무 할 일",
    "personal": "개인 할 일",
    "ai": "AI 논의 항목",
}
_CATEGORY_HEADERS = {
    "work": "💼 *업무 할 일*",
    "personal": "🏠 *개인 할 일*",
    "ai": "🤖 *AI 논의 항목*",
}


# ── LLM 파싱 ─────────────────────────────────────────────────

def _load_parse_template() -> str:
    with open(_PARSE_TEMPLATE_PATH, encoding="utf-8") as f:
        return f.read()


def _today_kst_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _weekday_kr(dt: datetime) -> str:
    names = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
    return names[dt.weekday()]


def _parse_todo_text(raw_text: str) -> dict:
    """LLM으로 자연어 Todo 본문/카테고리/마감일 파싱."""
    today = _today_kst_str()
    weekday = _weekday_kr(datetime.now(KST))
    template = _load_parse_template()
    prompt = (template
              .replace("{{today}}", today)
              .replace("{{weekday}}", weekday)
              .replace("{{text}}", raw_text.replace('"', "'")))

    llm_failed = False
    try:
        result = generate_text(prompt)
        cleaned = result.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        parsed = json.loads(cleaned)
    except Exception as e:
        log.warning(f"Todo 파싱 실패, 폴백 사용: {e}")
        llm_failed = True
        parsed = {
            "task": raw_text.strip(),
            "category": "work",
            "due_date": None,
            "original_text": raw_text,
            "_is_past_completion": False,
        }

    # 검증·정규화
    # LLM 성공 시 빈 task는 의도적으로 빈 값을 유지(호출자가 거부)
    task_raw = parsed.get("task")
    if task_raw is None and llm_failed:
        task = raw_text.strip()
    else:
        task = (task_raw or "").strip()
    category = (parsed.get("category") or "work").strip().lower()
    if category not in _VALID_CATEGORIES:
        category = "work"
    due_date = parsed.get("due_date")
    if due_date and not isinstance(due_date, str):
        due_date = None
    if due_date:
        # 형식 검증
        try:
            datetime.strptime(due_date, "%Y-%m-%d")
        except ValueError:
            due_date = None

    return {
        "task": task,
        "category": category,
        "due_date": due_date,
        "original_text": parsed.get("original_text") or raw_text,
        "_is_past_completion": bool(parsed.get("_is_past_completion", False)),
    }


# ── 마감 색상/이모지 (FR-T6) ────────────────────────────────

def _format_due(due_date_str: str | None, today: datetime | None = None) -> tuple[str, str]:
    """FR-T6: (emoji, label) 반환. due 없으면 (⚪, "")."""
    if not due_date_str:
        return ("⚪", "")
    today = today or datetime.now(KST)
    try:
        due = datetime.strptime(due_date_str, "%Y-%m-%d").replace(tzinfo=KST)
    except ValueError:
        return ("⚪", due_date_str)

    today_d = today.date()
    due_d = due.date()
    delta_days = (due_d - today_d).days

    if delta_days <= 0:
        if delta_days < 0:
            label = f"due {due_date_str} ({-delta_days}일 지남)"
        else:
            label = f"due {due_date_str} (오늘)"
        return ("🔴", label)
    if delta_days <= 2:
        return ("🟠", f"due {due_date_str} (D-{delta_days})")
    if delta_days <= 7:
        return ("🟡", f"due {due_date_str} (D-{delta_days})")
    return ("⚪", f"due {due_date_str} (D-{delta_days})")


# ── Drive 미러 헬퍼 ─────────────────────────────────────────

def _safe_drive_upsert(user_id: str, force: bool = False) -> None:
    """Drive 오픈루프.md 동기화. 실패해도 메인 흐름에는 영향 없음."""
    try:
        creds = user_store.get_credentials(user_id)
        user = user_store.get_user(user_id)
        contacts_folder_id = user.get("contacts_folder_id") if user else None
        if not contacts_folder_id:
            log.info(f"contacts_folder_id 없음 — Drive 동기화 건너뜀 (user={user_id})")
            return
        _drive.upsert_todo_openloop(user_id, creds, contacts_folder_id, force=force)
    except Exception as e:
        log.warning(f"Drive 오픈루프 동기화 실패 (user={user_id}): {e}")


def _safe_drive_history(user_id: str, line: str) -> None:
    """Drive 이력.md append. 실패해도 메인 흐름은 계속."""
    try:
        creds = user_store.get_credentials(user_id)
        user = user_store.get_user(user_id)
        contacts_folder_id = user.get("contacts_folder_id") if user else None
        if not contacts_folder_id:
            return
        today = _today_kst_str()
        _drive.append_todo_history(user_id, creds, contacts_folder_id, line, today)
    except Exception as e:
        log.warning(f"Drive 이력 append 실패 (user={user_id}): {e}")


# ── 추가 (FR-T1) ────────────────────────────────────────────

def _split_multi_todos(raw_text: str) -> list[str]:
    """멀티라인/멀티 항목 입력을 개별 todo 라인으로 분할.

    - 빈 줄로 구분된 블록을 라인 단위로 평탄화
    - 각 라인의 불릿 마커("- ", "• ", "* ", "▸ ", "▪ ", "1. ", "1) " 등) 제거
    - 빈 라인은 스킵
    - 단일 라인이면 [raw_text] 한 개 반환
    """
    if not raw_text or "\n" not in raw_text:
        return [raw_text.strip()] if raw_text.strip() else []

    lines: list[str] = []
    for line in raw_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # 불릿/번호 마커 제거
        for marker in ("- ", "• ", "* ", "+ ", "▸ ", "▪ ", "● ", "▶ ", "→ "):
            if line.startswith(marker):
                line = line[len(marker):].strip()
                break
        # 번호 마커 ("1. ", "1) ", "(1) ")
        import re as _re
        m = _re.match(r"^[\(]?\d+[\.\)\]]\s+", line)
        if m:
            line = line[m.end():].strip()
        if line:
            lines.append(line)

    if len(lines) <= 1:
        return [raw_text.strip()] if raw_text.strip() else []
    return lines


def handle_add(slack_client, user_id: str, raw_text: str,
               channel: str | None = None, thread_ts: str | None = None,
               source: str | None = None) -> int | None:
    """Todo 추가. 멀티라인이면 여러 개 추가. LLM 파싱 → DB 저장 → 히스토리 → Drive → Slack ack."""
    items = _split_multi_todos(raw_text)
    if not items:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="⚠️ 할 일 본문이 비어있어요. 예: `할일 추가 내일까지 AIA 제안서 이슈 작성`")
        return None

    if len(items) > 1:
        log.info(f"멀티 Todo 추가: user={user_id} count={len(items)}")
        return _handle_add_multi(slack_client, user_id, items,
                                  channel=channel, thread_ts=thread_ts, source=source)

    parsed = _parse_todo_text(items[0])
    task = parsed["task"]
    if not task.strip():
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="⚠️ 할 일 본문이 비어있어요. 예: `할일 추가 내일까지 AIA 제안서 이슈 작성`")
        return None

    category = parsed["category"]
    due_date = parsed["due_date"]

    src_str = source
    if not src_str and channel and thread_ts:
        src_str = f"slack:{channel}:{thread_ts}"
    elif not src_str and channel:
        src_str = f"slack:{channel}"

    todo_id = user_store.add_todo(
        user_id=user_id, task=task, category=category,
        due_date=due_date, source=src_str,
    )
    user_store.log_todo_history(
        todo_id, user_id, "created",
        payload={"task": task, "category": category, "due_date": due_date},
    )

    # Drive 미러 (오픈루프 + 이력)
    _safe_drive_upsert(user_id)
    now_hm = datetime.now(KST).strftime("%H:%M")
    due_part = f", due {due_date}" if due_date else ""
    _safe_drive_history(user_id, f"- {now_hm} created: {task} ({category}{due_part})")

    # Slack 응답 — 부주의한 추가를 되돌릴 수 있도록 [🗑️ 삭제] 버튼
    emoji, label = _format_due(due_date)
    cat_label = _CATEGORY_LABELS.get(category, category)
    text = (
        f"📝 할 일 추가됨 (#{todo_id}) · *{task}*\n"
        f"• 분류: {cat_label}\n"
        f"• {emoji} {label or '마감 미지정'}"
    )
    if parsed.get("_is_past_completion"):
        text += "\n_💡 이미 완료된 항목인가요? `@paramee {제목} 완료` 로 처리할 수 있어요._"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {"type": "actions", "elements": [
            {"type": "button",
             "text": {"type": "plain_text", "text": "🗑️ 삭제"},
             "style": "danger",
             "action_id": "todo_delete_btn",
             "value": str(todo_id)},
        ]},
    ]
    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text=text, blocks=blocks)

    log.info(f"Todo 추가: id={todo_id} user={user_id} task={task!r} category={category} due={due_date}")
    return todo_id


def _handle_add_multi(slack_client, user_id: str, items: list[str],
                      channel: str | None = None, thread_ts: str | None = None,
                      source: str | None = None) -> int | None:
    """여러 항목을 한 번에 추가 — 한 메시지로 N건 응답."""
    src_str = source
    if not src_str and channel and thread_ts:
        src_str = f"slack:{channel}:{thread_ts}"
    elif not src_str and channel:
        src_str = f"slack:{channel}"

    added: list[dict] = []
    failed: list[str] = []

    for raw in items:
        try:
            parsed = _parse_todo_text(raw)
            task = parsed["task"].strip()
            if not task:
                failed.append(raw)
                continue
            todo_id = user_store.add_todo(
                user_id=user_id, task=task, category=parsed["category"],
                due_date=parsed["due_date"], source=src_str,
            )
            user_store.log_todo_history(
                todo_id, user_id, "created",
                payload={"task": task, "category": parsed["category"],
                         "due_date": parsed["due_date"]},
            )
            now_hm = datetime.now(KST).strftime("%H:%M")
            due_part = f", due {parsed['due_date']}" if parsed["due_date"] else ""
            _safe_drive_history(user_id,
                                f"- {now_hm} created: {task} ({parsed['category']}{due_part})")
            added.append({
                "id": todo_id, "task": task,
                "category": parsed["category"], "due_date": parsed["due_date"],
            })
        except Exception as e:
            log.exception(f"Todo 추가 실패: {raw!r} — {e}")
            failed.append(raw)

    # Drive 오픈루프 1회만 갱신 (배치 후)
    _safe_drive_upsert(user_id, force=True)

    # 응답 메시지
    if not added:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ {len(items)}건 모두 추가 실패. 본문이 너무 짧거나 빈 항목이 포함되어 있을 수 있어요.")
        return None

    lines = [f"📝 *{len(added)}건* 할 일 추가됨"]
    if failed:
        lines.append(f"_(추가 실패 {len(failed)}건은 무시됨)_")
    lines.append("")
    for item in added:
        emoji, label = _format_due(item["due_date"])
        cat_label = _CATEGORY_LABELS.get(item["category"], item["category"])
        due_str = f" — {label}" if label else ""
        lines.append(f"{emoji} *#{item['id']}* {item['task']}  _({cat_label}{due_str})_")
    text = "\n".join(lines)

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text=text, blocks=blocks)

    log.info(f"멀티 Todo 추가 완료: user={user_id} added={len(added)} failed={len(failed)}")
    return added[0]["id"] if added else None


# ── 조회 (FR-T2) ────────────────────────────────────────────

def _build_active_blocks(active: list[dict],
                          recent_done: list[dict],
                          today: datetime,
                          history_link: str | None = None) -> list[dict]:
    """`/할일` 응답용 Block Kit 블록 (카테고리 섹션 + 항목 + 버튼)."""
    blocks: list[dict] = []
    blocks.append({
        "type": "header",
        "text": {"type": "plain_text", "text": "📋 내 할 일"},
    })
    summary = (
        f"활성 *{len(active)}* · 최근 완료 *{len(recent_done)}* · "
        f"기준일 {today.strftime('%Y-%m-%d')}"
    )
    blocks.append({"type": "context",
                   "elements": [{"type": "mrkdwn", "text": summary}]})

    if not active:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_활성 할 일이 없어요. `할 일 추가 …` 으로 시작하세요._"},
        })

    # 카테고리 그룹
    grouped: dict[str, list[dict]] = {"work": [], "personal": [], "ai": []}
    for t in active:
        cat = t.get("category") or "work"
        grouped.setdefault(cat, []).append(t)

    # 표시용 번호: 정렬된 순서대로 1..N 부여
    counter = 0
    for cat in ("work", "personal", "ai"):
        items = grouped.get(cat, [])
        if not items:
            continue
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": _CATEGORY_HEADERS[cat]},
        })
        for t in items:
            counter += 1
            emoji, label = _format_due(t.get("due_date"), today)
            line = f"{emoji} *{counter}.* {t.get('task','')}"
            if label:
                line += f"  ·  _{label}_"
            todo_id = str(t.get("id"))
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": line},
            })
            blocks.append({
                "type": "actions",
                "elements": [
                    {"type": "button",
                     "text": {"type": "plain_text", "text": "✅ 완료"},
                     "style": "primary",
                     "action_id": "todo_complete_btn",
                     "value": todo_id},
                    {"type": "button",
                     "text": {"type": "plain_text", "text": "🚫 취소"},
                     "action_id": "todo_cancel_btn",
                     "value": todo_id},
                    {"type": "button",
                     "text": {"type": "plain_text", "text": "🗑️ 삭제"},
                     "style": "danger",
                     "action_id": "todo_delete_btn",
                     "value": todo_id},
                ],
            })

    if recent_done:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "✅ *최근 완료 5건*"},
        })
        for t in recent_done:
            closed = (t.get("closed_at") or "")[:10]
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn",
                              "text": f"~{t.get('task','')}~  ·  완료 {closed}"}],
            })

    if history_link:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"📂 <{history_link}|Drive 이력 보기>"}],
        })
    return blocks


def handle_list(slack_client, user_id: str,
                channel: str | None = None, thread_ts: str | None = None) -> None:
    """현재 활성 Todo + 최근 완료 5건을 Block Kit 으로 출력. silent fail 차단."""
    log.info(f"Todo 목록 조회 요청: user={user_id}")
    try:
        active = user_store.list_active_todos(user_id)
        recent = user_store.list_recent_completed(user_id, 5)
    except Exception as e:
        log.exception(f"Todo 목록 DB 조회 실패: user={user_id}")
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 할 일 목록을 불러오지 못했어요. 잠시 후 다시 시도해주세요.\n_(에러: {e})_")
        return

    log.info(f"Todo 목록 결과: user={user_id} active={len(active)} recent={len(recent)}")
    today = datetime.now(KST)

    try:
        blocks = _build_active_blocks(active, recent, today)
    except Exception as e:
        log.exception(f"Todo 블록 빌드 실패: user={user_id}")
        # 폴백: 단순 텍스트로라도 응답
        text_lines = [f"📋 내 할 일 — 활성 *{len(active)}* / 최근 완료 *{len(recent)}*"]
        if not active:
            text_lines.append("_활성 할 일이 없어요. `할 일 추가 …` 으로 시작하세요._")
        else:
            for t in active[:20]:
                due = t.get("due_date") or "—"
                text_lines.append(f"• #{t.get('id')} {t.get('task','')} (마감 {due})")
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="\n".join(text_lines))
        return

    fallback = f"📋 내 할 일 — 활성 {len(active)}건"
    try:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=fallback, blocks=blocks)
    except Exception as e:
        log.exception(f"Todo 목록 게시 실패: user={user_id}")
        # 텍스트만이라도
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=fallback + "  (블록 렌더링 실패)")
        return

    # 조회 시점에도 Drive 미러 동기화 보장 (force=False — 디바운스 적용)
    try:
        _safe_drive_upsert(user_id)
    except Exception as e:
        log.warning(f"Drive 미러 갱신 실패 (무시): user={user_id} — {e}")


# ── 완료/취소/삭제 (FR-T3) ──────────────────────────────────

def _resolve_todo_target(user_id: str, target: int | str) -> dict | None:
    """ID(int) 또는 텍스트(str)로 활성 Todo 1건 식별. 모호하면 None."""
    if isinstance(target, int) or (isinstance(target, str) and target.isdigit()):
        todo_id = int(target)
        return user_store.get_todo(user_id, todo_id)
    matches = user_store.find_todo_by_text(user_id, str(target).strip())
    if len(matches) == 1:
        return matches[0]
    return None  # 0건 또는 다중매칭 — 호출측에서 처리


def _close_with_event(slack_client, user_id: str, target: int | str,
                      *, status: str, event: str, success_emoji: str,
                      success_label: str, note: str | None = None,
                      channel: str | None = None,
                      thread_ts: str | None = None) -> bool:
    """완료/취소/삭제 공통 처리."""
    todo = _resolve_todo_target(user_id, target)
    if not todo:
        # 다중 매칭 또는 미존재
        if isinstance(target, str) and not target.isdigit():
            matches = user_store.find_todo_by_text(user_id, target.strip())
            if len(matches) > 1:
                preview = "\n".join(
                    f"• #{m['id']} {m['task']}" for m in matches[:5]
                )
                _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                      text=f"⚠️ '{target}' 와 일치하는 활성 할 일이 여러 개예요.\n{preview}\n\n번호로 지정해주세요.")
                return False
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 해당 할 일을 찾지 못했어요. (대상: {target!r})")
        return False

    if todo["status"] != "open" and event != "deleted":
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"ℹ️ 이미 *{todo['status']}* 상태인 할 일입니다 (#{todo['id']}).")
        return False

    user_store.close_todo(todo["id"], status=status, note=note)
    payload = {"task": todo["task"], "category": todo.get("category"),
               "due_date": todo.get("due_date")}
    if note:
        payload["reason"] = note
    user_store.log_todo_history(todo["id"], user_id, event, payload=payload)

    _safe_drive_upsert(user_id, force=True)
    now_hm = datetime.now(KST).strftime("%H:%M")
    cat = todo.get("category") or "work"
    line = f"- {now_hm} {event}: {todo['task']} ({cat})"
    if note:
        line += f" — 사유: {note}"
    _safe_drive_history(user_id, line)

    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text=f"{success_emoji} *#{todo['id']} {todo['task']}* — {success_label}")
    log.info(f"Todo {event}: id={todo['id']} user={user_id}")
    return True


def handle_complete(slack_client, user_id: str, target: int | str,
                    channel: str | None = None,
                    thread_ts: str | None = None) -> bool:
    return _close_with_event(slack_client, user_id, target,
                              status="done", event="completed",
                              success_emoji="✅", success_label="완료 처리했습니다.",
                              channel=channel, thread_ts=thread_ts)


def handle_cancel(slack_client, user_id: str, target: int | str,
                  reason: str | None = None,
                  channel: str | None = None,
                  thread_ts: str | None = None) -> bool:
    return _close_with_event(slack_client, user_id, target,
                              status="cancelled", event="cancelled",
                              success_emoji="🚫", success_label="취소 처리했습니다.",
                              note=reason,
                              channel=channel, thread_ts=thread_ts)


def handle_delete(slack_client, user_id: str, target: int | str,
                  channel: str | None = None,
                  thread_ts: str | None = None) -> bool:
    return _close_with_event(slack_client, user_id, target,
                              status="deleted", event="deleted",
                              success_emoji="🗑️", success_label="삭제했습니다.",
                              channel=channel, thread_ts=thread_ts)


# ── 수정 (FR-T4) ────────────────────────────────────────────

def handle_update(slack_client, user_id: str, target_text: str | int,
                  field: str, new_value: str,
                  channel: str | None = None,
                  thread_ts: str | None = None) -> bool:
    """field: task | due_date | category"""
    if field not in ("task", "due_date", "category"):
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 수정 가능 필드: task / due_date / category")
        return False

    todo = _resolve_todo_target(user_id, target_text)
    if not todo:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ '{target_text}' 와 일치하는 할 일을 단일 식별하지 못했습니다.")
        return False

    if field == "category" and new_value not in _VALID_CATEGORIES:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ category는 work | personal | ai 중 하나여야 합니다.")
        return False
    if field == "due_date" and new_value:
        try:
            datetime.strptime(new_value, "%Y-%m-%d")
        except ValueError:
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text=f"⚠️ due_date 형식은 YYYY-MM-DD 이어야 합니다.")
            return False

    old_val = todo.get(field)
    user_store.update_todo(todo["id"], **{field: new_value or None})
    user_store.log_todo_history(
        todo["id"], user_id, "updated",
        payload={"field": field, "old": old_val, "new": new_value},
    )

    _safe_drive_upsert(user_id, force=True)
    now_hm = datetime.now(KST).strftime("%H:%M")
    _safe_drive_history(
        user_id,
        f"- {now_hm} updated: {todo['task']} ({field}: {old_val} → {new_value})"
    )

    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text=f"✏️ #{todo['id']} *{todo['task']}* — {field} 갱신 완료 ({old_val} → {new_value})")
    return True


# ── 브리핑 통합 (FR-T5) ─────────────────────────────────────

def build_todo_block(user_id: str, today_date: datetime | None = None) -> list[dict]:
    """브리핑 단계 2에서 호출. 활성 Todo 요약 블록 (최대 12줄, 초과 시 +N more)."""
    today = today_date or datetime.now(KST)
    active = user_store.list_active_todos(user_id)
    if not active:
        return []

    lines: list[str] = [f"📋 *오늘의 Todo* — 활성 {len(active)}건"]
    MAX_LINES = 12
    counter = 0
    for t in active:
        if counter >= MAX_LINES:
            break
        counter += 1
        emoji, label = _format_due(t.get("due_date"), today)
        cat = t.get("category") or "work"
        cat_marker = {"work": "💼", "personal": "🏠", "ai": "🤖"}.get(cat, "•")
        line = f"{emoji} {cat_marker} {t.get('task','')}"
        if label:
            line += f" _({label})_"
        lines.append(line)

    overflow = len(active) - counter
    if overflow > 0:
        lines.append(f"_…외 {overflow}건. `/할일` 로 전체 보기_")

    return [{
        "type": "section",
        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
    }]


# ── 버튼 라우팅 ──────────────────────────────────────────────

def _disable_clicked_action_block(slack_client, body: dict, status_label: str) -> None:
    """버튼 클릭 후 원본 메시지의 해당 actions 블록을 상태 표시 텍스트로 교체.

    동일 todo의 좀비 버튼 클릭 방지 — '완료해놓고 삭제 누르는' 케이스 차단.
    같은 메시지 내 다른 todo의 actions 블록은 그대로 유지.
    """
    channel = (body.get("channel") or {}).get("id") or (body.get("container") or {}).get("channel_id")
    msg_ts = (body.get("message") or {}).get("ts") or (body.get("container") or {}).get("message_ts")
    if not (channel and msg_ts):
        return
    clicked_value = (body.get("actions") or [{}])[0].get("value", "")
    if not clicked_value:
        return

    original_blocks = (body.get("message") or {}).get("blocks") or []
    if not original_blocks:
        return

    new_blocks: list[dict] = []
    for blk in original_blocks:
        if blk.get("type") == "actions":
            elements = blk.get("elements", []) or []
            # 이 actions 블록이 클릭된 버튼(=todo_id)에 속하면 상태 텍스트로 교체
            if any((el.get("value") or "") == clicked_value for el in elements):
                new_blocks.append({
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"_✓ {status_label}_"}],
                })
                continue
        new_blocks.append(blk)

    try:
        slack_client.chat_update(
            channel=channel,
            ts=msg_ts,
            blocks=new_blocks,
            text=(body.get("message") or {}).get("text") or "할 일 처리됨",
        )
    except Exception as e:
        log.warning(f"chat_update 실패 (무시): {e}")


def handle_complete_button(slack_client, body: dict) -> None:
    user_id = body["user"]["id"]
    todo_id = body.get("actions", [{}])[0].get("value", "")
    if not todo_id:
        return
    msg_channel = body.get("channel", {}).get("id") or body.get("container", {}).get("channel_id")
    msg_thread_ts = (body.get("message") or {}).get("thread_ts")
    handle_complete(slack_client, user_id=user_id, target=int(todo_id),
                    channel=msg_channel, thread_ts=msg_thread_ts)
    _disable_clicked_action_block(slack_client, body, "완료됨")


def handle_cancel_button(slack_client, body: dict) -> None:
    user_id = body["user"]["id"]
    todo_id = body.get("actions", [{}])[0].get("value", "")
    if not todo_id:
        return
    msg_channel = body.get("channel", {}).get("id") or body.get("container", {}).get("channel_id")
    msg_thread_ts = (body.get("message") or {}).get("thread_ts")
    handle_cancel(slack_client, user_id=user_id, target=int(todo_id),
                  channel=msg_channel, thread_ts=msg_thread_ts)
    _disable_clicked_action_block(slack_client, body, "취소됨")


def handle_delete_button(slack_client, body: dict) -> None:
    user_id = body["user"]["id"]
    todo_id = body.get("actions", [{}])[0].get("value", "")
    if not todo_id:
        return
    msg_channel = body.get("channel", {}).get("id") or body.get("container", {}).get("channel_id")
    msg_thread_ts = (body.get("message") or {}).get("thread_ts")
    handle_delete(slack_client, user_id=user_id, target=int(todo_id),
                  channel=msg_channel, thread_ts=msg_thread_ts)
    _disable_clicked_action_block(slack_client, body, "삭제됨")


# ── 자연어 의도 파서 (완료/취소/삭제/수정) ─────────────────

_COMPLETE_KEYWORDS = ("완료", "끝냄", "끝났어", "다 했어", "처리됨", "done", "complete")
_CANCEL_KEYWORDS = ("취소", "그만", "포기", "cancel")
_DELETE_KEYWORDS = ("삭제", "지워", "delete", "remove")


def parse_close_command(text: str) -> tuple[str | None, str | None]:
    """자연어 메시지에서 (action, target_text) 추출.
    action ∈ {complete, cancel, delete} 또는 None.
    target_text는 마지막 키워드 앞부분의 텍스트.
    """
    t = (text or "").strip()
    if not t:
        return None, None
    lowered = t.lower()
    matched_kw: str | None = None
    matched_action: str | None = None
    # 우선순위: delete > cancel > complete (삭제·취소가 더 명시적)
    for kw in _DELETE_KEYWORDS:
        if kw in lowered:
            matched_kw = kw
            matched_action = "delete"
            break
    if not matched_kw:
        for kw in _CANCEL_KEYWORDS:
            if kw in lowered:
                matched_kw = kw
                matched_action = "cancel"
                break
    if not matched_kw:
        for kw in _COMPLETE_KEYWORDS:
            if kw in lowered:
                matched_kw = kw
                matched_action = "complete"
                break
    if not matched_action:
        return None, None
    # target = 키워드 앞부분
    idx = lowered.rfind(matched_kw)
    target = t[:idx].strip().rstrip(",").rstrip()
    return matched_action, target or None
