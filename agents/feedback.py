"""Feedback 에이전트 — 사용자 피드백 수집 및 관리자 다이제스트 발송"""
import json
import logging
import os

from store import user_store
from agents.before import generate_text

log = logging.getLogger(__name__)

_FEEDBACK_CHANNEL = os.getenv("FEEDBACK_CHANNEL", "")

_CATEGORY_LABELS = {
    "feature_request": "✨ 기능 요청",
    "improvement": "💡 개선 요청",
    "bug_report": "🐛 버그 리포트",
}

_CLASSIFY_PROMPT = """사용자가 Slack DM으로 보낸 메시지를 분석해서 피드백 유형을 분류하고 핵심 내용을 요약해줘.

메시지: "{text}"

분류 기준:
- feature_request: 새 기능 추가 요청 (예: "~기능 추가해줘", "~할 수 있게 해줘", "~도 지원해줘")
- improvement: 기존 기능 개선 요청 (예: "~이렇게 바꿔줘", "~이 불편해", "~개선해줘", "~좀 더 ~했으면")
- bug_report: 오류/버그 신고 (예: "~안 돼", "~가 이상해", "에러가 나", "버그 같아", "~가 안 먹어")

JSON으로만 반환 (설명 없이):
{{"category": "feature_request|improvement|bug_report", "summary": "핵심 내용 한 줄 요약 (30자 이내)"}}"""


def handle_feedback(slack_client, user_id: str, text: str,
                    channel: str = None, thread_ts: str = None) -> None:
    """사용자 피드백을 분류·저장하고 접수 확인 메시지 전송"""
    try:
        result = generate_text(_CLASSIFY_PROMPT.format(text=text.replace('"', "'")))
        cleaned = result.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        parsed = json.loads(cleaned)
        category = parsed.get("category", "feature_request")
        summary = parsed.get("summary", text[:50])
    except Exception as e:
        log.warning(f"피드백 분류 실패, 기본값 사용: {e}")
        category = "feature_request"
        summary = text[:50]

    if category not in _CATEGORY_LABELS:
        category = "feature_request"

    feedback_id = user_store.save_feedback(
        user_id=user_id,
        category=category,
        content=summary,
        original=text,
    )

    label = _CATEGORY_LABELS[category]
    slack_client.chat_postMessage(
        channel=channel or user_id,
        thread_ts=thread_ts,
        text=f"📝 피드백이 접수되었습니다! (#{feedback_id})\n"
             f"• 유형: {label}\n"
             f"• 내용: {summary}\n\n"
             f"관리자에게 매일 아침 전달됩니다. 감사합니다!",
    )
    log.info(f"피드백 저장 완료: id={feedback_id} user={user_id} category={category}")


def send_feedback_digest(slack_client) -> None:
    """미전송 피드백을 모아 관리자 채널/DM으로 다이제스트 발송"""
    items = user_store.get_pending_feedback()
    if not items:
        log.info("전송할 피드백 없음")
        return

    channel = _FEEDBACK_CHANNEL
    if not channel:
        log.warning("FEEDBACK_CHANNEL 환경변수 미설정 — 피드백 다이제스트 발송 건너뜀")
        return

    # 카테고리별 그룹핑
    grouped: dict[str, list[dict]] = {}
    for item in items:
        grouped.setdefault(item["category"], []).append(item)

    lines = [f"📋 *피드백 다이제스트* ({len(items)}건)\n"]

    for cat in ("bug_report", "feature_request", "improvement"):
        cat_items = grouped.get(cat, [])
        if not cat_items:
            continue
        label = _CATEGORY_LABELS.get(cat, cat)
        lines.append(f"*{label}* ({len(cat_items)}건)")
        for it in cat_items:
            user_tag = f"<@{it['user_id']}>"
            date_str = it["created_at"][:10]
            lines.append(f"  • {it['content']} — {user_tag} ({date_str})")
            if it["original"] != it["content"]:
                original_preview = it["original"][:80]
                if len(it["original"]) > 80:
                    original_preview += "…"
                lines.append(f"    _{original_preview}_")
        lines.append("")

    try:
        slack_client.chat_postMessage(
            channel=channel,
            text="\n".join(lines),
        )
        user_store.mark_feedback_notified([it["id"] for it in items])
        log.info(f"피드백 다이제스트 발송 완료: {len(items)}건 → {channel}")
    except Exception as e:
        log.exception(f"피드백 다이제스트 발송 실패: {e}")
