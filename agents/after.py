"""After Agent — 회의록 생성 완료 후 사후 처리 자동화

동작 방식:
  - During Agent의 _generate_and_post_minutes() 완료 직후 백그라운드 스레드로 호출
  - 외부 참석자 이메일 조회 → Slack Draft 버튼 발송 → 사용자 승인 후 Gmail 발송
  - 내부용 회의록에서 액션아이템 LLM 추출 → DB 저장 → 담당자 DM 알림
  - 매일 08:00 리마인더: D-day/D-1 미완료 액션아이템 담당자 DM
  - 외부 참석자 People 파일에 last_met + 미팅 이력 갱신
  - '후속 미팅' 패턴 감지 시 일정 생성 제안
"""
import json
import logging
import os
import re
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import anthropic
from google import genai

from store import user_store
from tools import calendar as cal, drive, gmail
from prompts.briefing import extract_action_items_prompt

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# 사람 정보 조회 캐시 (name → {"email": str|None, "slack_uid": str|None})
_person_cache: dict[str, dict] = {}

_gemini = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
_GEMINI_MODEL = "gemini-2.0-flash"
_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_CLAUDE_MODEL = "claude-haiku-4-5"

_INTERNAL_DOMAINS = set(
    os.getenv("INTERNAL_DOMAINS", "parametacorp.com,iconloop.com").split(",")
)

_FOLLOWUP_PATTERNS = [
    "다음 미팅", "후속 미팅", "다시 만나", "follow-up", "follow up",
    "후속 일정", "다음에 만나", "재미팅",
]


# ── LLM ──────────────────────────────────────────────────────

def _generate(prompt: str) -> str:
    """Gemini 우선, 실패 시 Claude 폴백"""
    try:
        resp = _gemini.models.generate_content(model=_GEMINI_MODEL, contents=prompt)
        return resp.text.strip()
    except Exception as e:
        log.warning(f"Gemini _generate 실패, Claude로 폴백: {e}")
        resp = _claude.messages.create(
            model=_CLAUDE_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()


# ── 진입점 ───────────────────────────────────────────────────

def trigger_after_meeting(
    slack_client, *,
    user_id: str,
    event_id: str | None,
    title: str,
    date_str: str,
    attendees_raw: list[dict],
    internal_body: str,
    external_body: str,
    creds,
) -> None:
    """After Agent 진입점. 백그라운드 스레드에서 실행."""
    try:
        log.info(f"After Agent 시작: {title} (event_id={event_id})")

        # 사용자 설정 조회 (contacts_folder_id)
        contacts_folder_id = None
        try:
            user_info = user_store.get_user(user_id)
            contacts_folder_id = user_info.get("contacts_folder_id")
        except Exception:
            pass

        # 사람 조회 캐시 초기화 (미팅별로 새로 조회)
        _person_cache.clear()

        # A. 외부 참석자 이메일 조회
        recipients = _resolve_attendee_emails(
            attendees_raw, event_id, creds,
            slack_client=slack_client,
            contacts_folder_id=contacts_folder_id,
        )
        log.info(f"외부 참석자: {[r['email'] for r in recipients]}")

        # B. 액션아이템 추출
        _extract_and_save_action_items(event_id or title, user_id, internal_body)

        # C. 외부용 Draft Slack 발송
        _send_draft_to_slack(
            slack_client,
            user_id=user_id,
            event_id=event_id or title,
            title=title,
            date_str=date_str,
            external_body=external_body,
            recipients=recipients,
        )

        # D. 담당자 DM 알림
        _notify_action_items(
            slack_client,
            event_id=event_id or title,
            user_id=user_id,
            title=title,
            creds=creds,
            contacts_folder_id=contacts_folder_id,
        )

        # E. Contacts 자동 갱신
        try:
            if contacts_folder_id and recipients:
                _update_contacts(creds, contacts_folder_id, recipients, title, date_str)
        except Exception as e:
            log.warning(f"Contacts 갱신 실패 (무시): {e}")

        # F. 후속 일정 패턴 감지
        _suggest_followup(slack_client, user_id, internal_body, title)

        log.info(f"After Agent 완료: {title}")

    except Exception:
        log.exception(f"After Agent 오류: {title}")


# ── A. 참석자 이메일 조회 ────────────────────────────────────

def _lookup_person(name: str, slack_client, creds, contacts_folder_id: str | None) -> dict:
    """이름으로 사람 정보 조회. 우선순위: Slack → Google 주소록 → Gmail → Contacts 폴더
    Returns: {"email": str|None, "slack_uid": str|None}
    """
    if name in _person_cache:
        return _person_cache[name]

    result = {"email": None, "slack_uid": None}

    # 1. Slack 계정 조회 (display_name / real_name 매칭)
    try:
        resp = slack_client.users_list()
        for member in resp.get("members", []):
            if member.get("deleted") or member.get("is_bot"):
                continue
            profile = member.get("profile", {})
            display = profile.get("display_name", "").strip()
            real = profile.get("real_name", "").strip()
            email_addr = profile.get("email", "")
            if name in (display, real) or (display and display in name) or (real and real in name):
                result["slack_uid"] = member["id"]
                if email_addr:
                    result["email"] = email_addr
                break
    except Exception as e:
        log.debug(f"Slack 조회 실패 ({name}): {e}")

    # 2. Google 주소록 (People API)
    if not result["email"]:
        try:
            result["email"] = gmail.find_email_in_contacts(creds, name)
        except Exception as e:
            log.debug(f"Google 주소록 조회 실패 ({name}): {e}")

    # 3. Gmail 이메일 헤더 검색
    if not result["email"]:
        try:
            result["email"] = gmail.find_email_by_name(creds, name)
        except Exception as e:
            log.debug(f"Gmail 검색 실패 ({name}): {e}")

    # 4. Contacts 폴더 (Drive People/{이름}.md)
    if not result["email"] and contacts_folder_id:
        try:
            content, _ = drive.get_person_info(creds, contacts_folder_id, name)
            if content:
                m = re.search(r"email:\s*([\w.+-]+@[\w.-]+)", content)
                if m:
                    result["email"] = m.group(1)
        except Exception as e:
            log.debug(f"Contacts 폴더 조회 실패 ({name}): {e}")

    _person_cache[name] = result
    return result


def _resolve_attendee_emails(
    attendees_raw: list[dict],
    event_id: str | None,
    creds,
    slack_client=None,
    contacts_folder_id: str | None = None,
) -> list[dict]:
    """외부 참석자 이메일+이름 목록 반환 (내부 도메인 제외)
    우선순위: Calendar API attendees → attendees_raw + 이름 기반 조회
    """
    # 1. Calendar API로 참석자 조회 (event_id 있는 경우)
    base_list: list[dict] = []
    if event_id:
        try:
            base_list = cal.get_event_attendees(creds, event_id)
        except Exception as e:
            log.warning(f"Calendar 참석자 조회 실패, attendees_raw 사용: {e}")

    # Calendar API 결과가 없으면 attendees_raw 사용
    if not base_list:
        for a in attendees_raw:
            email = a.get("email", "")
            domain = email.split("@")[-1] if "@" in email else ""
            if domain and domain not in _INTERNAL_DOMAINS:
                base_list.append({"name": a.get("name", ""), "email": email})

    # 2. 이메일 없는 참석자는 이름으로 추가 조회
    result = []
    for person in base_list:
        name = person.get("name", "").strip()
        email = person.get("email", "")
        if not email and name and slack_client:
            info = _lookup_person(name, slack_client, creds, contacts_folder_id)
            email = info.get("email", "")
        if email:
            result.append({"name": name, "email": email})
    return result


# ── B. 액션아이템 추출 ──────────────────────────────────────

def _extract_and_save_action_items(event_id: str, user_id: str, internal_body: str) -> None:
    """LLM으로 액션아이템 추출 후 DB 저장"""
    try:
        raw = _generate(extract_action_items_prompt(internal_body))
        # JSON 블록 추출
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if not match:
            log.warning("액션아이템 JSON 파싱 실패 (빈 배열 처리)")
            return
        items = json.loads(match.group())
        if not isinstance(items, list):
            return
        if items:
            user_store.save_action_items(event_id, user_id, items)
            log.info(f"액션아이템 {len(items)}개 저장: {event_id}")
    except Exception as e:
        log.warning(f"액션아이템 추출 실패 (무시): {e}")


# ── C. 외부용 Draft Slack 발송 ──────────────────────────────

def _send_draft_to_slack(
    slack_client, *,
    user_id: str,
    event_id: str,
    title: str,
    date_str: str,
    external_body: str,
    recipients: list[dict],
) -> None:
    """외부용 회의록 발송 승인 요청 (Block Kit 버튼)"""
    # pending_drafts에 저장
    draft_id = user_store.save_pending_draft(
        event_id, user_id, title, external_body, recipients
    )

    if recipients:
        recipient_names = ", ".join(
            r["name"] or r["email"] for r in recipients
        )
        recipient_line = f"*발송 대상:* {recipient_names}"
    else:
        recipient_line = "*발송 대상:* 외부 참석자 없음 (이메일 발송 불필요)"

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"📤 *외부용 회의록 발송 준비*\n"
                    f"*미팅:* {title} ({date_str})\n"
                    f"{recipient_line}"
                ),
            },
        },
    ]

    if recipients:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "발송하기 ✉️"},
                    "style": "primary",
                    "action_id": "after_send_minutes",
                    "value": str(draft_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "발송 안 함"},
                    "action_id": "after_cancel_minutes",
                    "value": str(draft_id),
                },
            ],
        })

    slack_client.chat_postMessage(
        channel=user_id,
        text=f"📤 외부용 회의록 발송 준비: {title}",
        blocks=blocks,
    )


def handle_send_draft(slack_client, body: dict) -> None:
    """'발송하기' 버튼 핸들러"""
    user_id = body["user"]["id"]
    draft_id = int(body["actions"][0]["value"])
    draft = user_store.get_pending_draft(draft_id)

    if not draft or draft["status"] not in ("pending", "failed"):
        slack_client.chat_postMessage(
            channel=user_id, text="⚠️ 이미 처리된 발송 요청입니다."
        )
        return

    recipients = json.loads(draft["recipients"] or "[]")
    to_emails = [r["email"] for r in recipients if r.get("email")]

    if not to_emails:
        slack_client.chat_postMessage(
            channel=user_id, text="⚠️ 발송 가능한 이메일 주소가 없습니다."
        )
        return

    try:
        creds = user_store.get_credentials(draft["user_id"])
    except Exception as e:
        slack_client.chat_postMessage(
            channel=user_id, text=f"⚠️ 인증 오류: {e}"
        )
        return

    subject = f"[회의록] {draft['title']}"
    body_html = gmail.markdown_to_html(draft["external_body"])
    success = gmail.send_email(creds, to_emails, subject, body_html)

    if success:
        user_store.update_draft_status(draft_id, "sent")
        recipient_str = ", ".join(to_emails)
        slack_client.chat_postMessage(
            channel=user_id,
            text=f"✅ 외부용 회의록을 발송했습니다.\n수신: {recipient_str}",
        )
        log.info(f"외부용 회의록 발송 완료: {draft['title']} → {to_emails}")
    else:
        user_store.update_draft_status(draft_id, "failed")
        slack_client.chat_postMessage(
            channel=user_id,
            text=f"❌ 이메일 발송에 실패했습니다. 직접 발송해주세요.\n수신 예정: {', '.join(to_emails)}",
        )


def handle_cancel_draft(slack_client, body: dict) -> None:
    """'발송 안 함' 버튼 핸들러"""
    user_id = body["user"]["id"]
    draft_id = int(body["actions"][0]["value"])
    draft = user_store.get_pending_draft(draft_id)

    if draft and draft["status"] == "pending":
        user_store.update_draft_status(draft_id, "cancelled")

    slack_client.chat_postMessage(
        channel=user_id, text="⏭️ 외부용 회의록 발송을 취소했습니다."
    )


# ── D. 담당자 DM 알림 ────────────────────────────────────────

def _notify_action_items(
    slack_client, *, event_id: str, user_id: str, title: str,
    creds=None, contacts_folder_id: str | None = None,
) -> None:
    """액션아이템 담당자별 Slack DM 발송.
    담당자 Slack UID 조회: Slack → Google 주소록 → Gmail → Contacts 폴더 순서
    """
    items = user_store.get_action_items(event_id)
    if not items:
        return

    # 담당자별 그룹핑
    by_assignee: dict[str | None, list[dict]] = {}
    for item in items:
        key = item.get("assignee")
        by_assignee.setdefault(key, []).append(item)

    for assignee, assignee_items in by_assignee.items():
        lines = [f"📋 *{title}* 미팅 후 액션아이템"]
        for it in assignee_items:
            due = f" (기한: {it['due_date']})" if it.get("due_date") else ""
            lines.append(f"• {it['content']}{due}")

        text = "\n".join(lines)

        target_uid = user_id  # 기본: 주최자
        extra_note = ""
        if assignee:
            # Slack → Google 주소록 → Gmail → Contacts 폴더 순으로 조회
            info = _person_cache.get(assignee)
            if not info and creds:
                info = _lookup_person(assignee, slack_client, creds, contacts_folder_id)
            slack_uid = (info or {}).get("slack_uid")
            found_email = (info or {}).get("email")

            if slack_uid:
                # Slack 계정 찾음 → 직접 DM
                target_uid = slack_uid
            elif found_email:
                # Slack 없지만 이메일 발견 → 주최자에게 알리고 이메일로 전달 요청
                extra_note = (
                    f"\n\n_(담당자 *{assignee}*의 Slack 계정을 찾지 못했습니다. "
                    f"이메일({found_email})로 직접 전달해주세요.)_"
                )
            else:
                # 어디서도 찾지 못함
                extra_note = (
                    f"\n\n_(담당자 *{assignee}*의 연락처를 찾지 못했습니다. "
                    f"Slack·Google 주소록·Gmail·Contacts 폴더 모두 확인했습니다.)_"
                )

        if extra_note:
            text += extra_note

        try:
            slack_client.chat_postMessage(channel=target_uid, text=text)
        except Exception as e:
            log.warning(f"담당자 DM 실패 ({assignee}): {e}")


def handle_complete_action_item(slack_client, body: dict) -> None:
    """'완료 ✅' 버튼 핸들러"""
    user_id = body["user"]["id"]
    item_id = int(body["actions"][0]["value"])
    user_store.update_action_item_status(item_id, "done")
    slack_client.chat_postMessage(
        channel=user_id, text="✅ 액션아이템을 완료 처리했습니다."
    )


def action_item_reminder(slack_client) -> None:
    """매일 08:00 KST 실행 — D-day/D-1 미완료 액션아이템 담당자 DM"""
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    for due_date in (today, tomorrow):
        items = user_store.get_open_action_items_by_due(due_date)
        label = "오늘" if due_date == today else "내일"

        # 사용자(주최자)별 그룹핑
        by_user: dict[str, list[dict]] = {}
        for item in items:
            by_user.setdefault(item["user_id"], []).append(item)

        for uid, uid_items in by_user.items():
            lines = [f"⏰ *액션아이템 리마인더* — {label} 기한"]
            for it in uid_items:
                assignee = f"[{it['assignee']}] " if it.get("assignee") else ""
                lines.append(f"• {assignee}{it['content']}")
            try:
                slack_client.chat_postMessage(channel=uid, text="\n".join(lines))
            except Exception as e:
                log.warning(f"리마인더 DM 실패 ({uid}): {e}")


# ── E. Contacts 자동 갱신 ────────────────────────────────────

def _update_contacts(
    creds,
    contacts_folder_id: str,
    recipients: list[dict],
    title: str,
    date_str: str,
) -> None:
    """외부 참석자 People/{이름}.md에 last_met + 미팅 이력 1줄 추가"""
    for person in recipients:
        name = person.get("name", "").strip()
        if not name:
            continue
        try:
            content, file_id = drive.get_person_info(creds, contacts_folder_id, name)
            if not content:
                continue

            # last_met 업데이트
            if "last_met:" in content:
                content = re.sub(
                    r"last_met:\s*.+", f"last_met: {date_str}", content
                )
            else:
                content += f"\nlast_met: {date_str}"

            # 미팅 이력 섹션에 추가
            history_line = f"- {date_str} {title}"
            if "## 미팅 이력" in content:
                content = content.replace(
                    "## 미팅 이력",
                    f"## 미팅 이력\n{history_line}",
                    1,
                )
            else:
                content += f"\n\n## 미팅 이력\n{history_line}"

            drive.save_person_info(creds, contacts_folder_id, name, content, file_id)
            log.info(f"Contacts 갱신: {name}")
        except Exception as e:
            log.warning(f"Contacts 갱신 실패 ({name}): {e}")


# ── F. 후속 일정 제안 ────────────────────────────────────────

def _suggest_followup(
    slack_client, user_id: str, internal_body: str, title: str
) -> None:
    """'다음 미팅' 등 패턴 감지 시 일정 생성 제안 메시지 발송"""
    if not any(p in internal_body for p in _FOLLOWUP_PATTERNS):
        return

    slack_client.chat_postMessage(
        channel=user_id,
        text=f"📅 *{title}* 회의록에서 후속 미팅 언급이 감지되었습니다.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"📅 *{title}* 회의록에서 후속 미팅 언급이 감지되었습니다.\n"
                        f"후속 일정을 잡으시겠습니까?"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "후속 일정 잡기 📅"},
                        "action_id": "suggest_followup_meeting",
                        "value": title,
                    }
                ],
            },
        ],
    )
