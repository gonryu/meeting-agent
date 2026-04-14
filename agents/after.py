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
from tools import calendar as cal, drive, gmail, trello
from prompts.briefing import extract_action_items_prompt

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# 사람 정보 조회 캐시 (name → {"email": str|None, "slack_uid": str|None})
_person_cache: dict[str, dict] = {}
# 회의록 요약 캐시 (event_id → summary text) — Trello 카드 description용
_minutes_summary_cache: dict[str, str] = {}

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

        # D-2. Trello 액션아이템 등록 제안 (이벤트의 업체명 사용)
        try:
            company_names = []
            if event_id and creds:
                try:
                    ev = cal.get_event(creds, event_id)
                    company_raw = (ev.get("extendedProperties", {})
                                    .get("private", {}).get("company", ""))
                    company_names = [c.strip() for c in company_raw.split(",") if c.strip()]
                except Exception as e:
                    log.warning(f"이벤트 업체명 조회 실패: {e}")
            if not company_names:
                company_names = [title]
                log.info(f"업체명 없음 → 미팅 제목을 Trello 카드명으로 사용: {title}")
            if company_names:
                # 회의록 요약 생성 및 캐시 (Trello 카드 description용)
                eid = event_id or title
                try:
                    summary = _generate(_SUMMARIZE_MINUTES_PROMPT.format(
                        minutes=internal_body))
                    _minutes_summary_cache[eid] = summary.strip()
                    log.info(f"회의록 요약 캐시 저장: {eid}")
                except Exception as e:
                    log.warning(f"회의록 요약 생성 실패 (무시): {e}")
                _propose_trello_registration(
                    slack_client,
                    user_id=user_id,
                    event_id=eid,
                    company_names=company_names,
                )
        except Exception as e:
            log.warning(f"Trello 등록 제안 실패 (무시): {e}")

        # E. Contacts 자동 갱신
        try:
            if contacts_folder_id and recipients:
                _update_contacts(creds, contacts_folder_id, recipients, title, date_str)
        except Exception as e:
            log.warning(f"Contacts 갱신 실패 (무시): {e}")

        # F. 후속 일정 패턴 감지
        _suggest_followup(slack_client, user_id, internal_body, title)

        # G. 제안서 키워드 감지 + 제안 (FR-A11)
        try:
            from agents import proposal as proposal_agent
            proposal_agent.detect_and_suggest_proposal(
                slack_client,
                user_id=user_id,
                event_id=event_id,
                title=title,
                date_str=date_str,
                internal_body=internal_body,
                company_names=company_names,
                attendees_raw=attendees_raw,
                creds=creds,
            )
        except Exception as e:
            log.warning(f"제안서 제안 실패 (무시): {e}")

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


_SUMMARIZE_MINUTES_PROMPT = """다음 회의록을 10줄 이내로 요약하세요.
핵심 논의사항, 결정사항, 주요 발언을 간결하게 정리하세요.
불릿 포인트 없이 일반 텍스트로 작성하세요.

회의록:
{minutes}

요약:"""


# ── D-2. Trello 액션아이템 등록 ─────────────────────────────

def _propose_trello_registration(
    slack_client, *,
    user_id: str,
    event_id: str,
    company_names: list[str],
) -> None:
    """액션아이템이 있으면 업체별로 Trello 카드 후보를 보여주고 선택하게 함"""
    items = user_store.get_action_items(event_id)
    if not items:
        log.info(f"액션아이템 없음 — Trello 등록 스킵: {event_id}")
        return

    for company_name in company_names:
        # 유사 카드 후보 검색
        candidates = trello.search_cards(user_id, company_name, limit=5)

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"📌 *Trello에 액션아이템 등록할까요?*\n"
                        f"🏢 *업체:* {company_name} | *액션아이템:* {len(items)}건\n"
                        f"등록할 카드를 선택하세요."
                    ),
                },
            },
        ]

        buttons = []
        if candidates:
            # 후보 카드 목록 표시
            card_list_text = "\n".join(
                f"• *{c['card_name']}* ({c['list_name']})"
                for c in candidates
            )
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"🔍 *유사 카드 후보:*\n{card_list_text}",
                },
            })

            for c in candidates:
                label = c["card_name"]
                if len(label) > 30:
                    label = label[:27] + "..."
                buttons.append({
                    "type": "button",
                    "text": {"type": "plain_text", "text": label},
                    "style": "primary" if c.get("exact_match") else None,
                    "action_id": f"trello_select_card_{c['card_id']}",
                    "value": json.dumps({
                        "event_id": event_id,
                        "company": company_name,
                        "card_id": c["card_id"],
                        "card_name": c["card_name"],
                    }),
                })
                # style=None은 Slack API에서 무시되지 않으므로 제거
                if buttons[-1]["style"] is None:
                    del buttons[-1]["style"]

        # 신규 생성 + 건너뜀 버튼
        buttons.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "➕ 신규 카드 생성"},
            "action_id": "trello_new_card",
            "value": json.dumps({
                "event_id": event_id,
                "company": company_name,
            }),
        })
        buttons.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "건너뜀"},
            "action_id": "trello_skip",
            "value": json.dumps({
                "event_id": event_id,
                "company": company_name,
            }),
        })

        blocks.append({"type": "actions", "elements": buttons})

        slack_client.chat_postMessage(
            channel=user_id,
            text=f"📌 Trello에 액션아이템 등록할까요? ({company_name})",
            blocks=blocks,
        )


def handle_trello_select_card(slack_client, body: dict) -> None:
    """카드 선택 버튼 핸들러 — 기존 Trello 카드에 액션아이템 체크리스트 추가"""
    user_id = body["user"]["id"]
    try:
        payload = json.loads(body["actions"][0]["value"])
        event_id = payload["event_id"]
        company_name = payload["company"]
        card_id = payload["card_id"]
        card_name = payload["card_name"]
    except (KeyError, json.JSONDecodeError) as e:
        log.warning(f"Trello 카드 선택 payload 파싱 실패: {e}")
        slack_client.chat_postMessage(
            channel=user_id, text="❌ Trello 등록 실패: 잘못된 요청"
        )
        return

    _register_to_card(slack_client, user_id=user_id, event_id=event_id,
                      card_id=card_id, card_name=card_name)


def handle_trello_new_card(slack_client, body: dict) -> None:
    """'신규 카드 생성' 버튼 핸들러 — FR-A16: 사용자 확인 후 카드 생성"""
    user_id = body["user"]["id"]
    try:
        payload = json.loads(body["actions"][0]["value"])
        event_id = payload["event_id"]
        company_name = payload["company"]
    except (KeyError, json.JSONDecodeError) as e:
        log.warning(f"Trello 신규 카드 payload 파싱 실패: {e}")
        slack_client.chat_postMessage(
            channel=user_id, text="❌ Trello 등록 실패: 잘못된 요청"
        )
        return

    # FR-A16: 카드 생성 전 확인 메시지 발송
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🆕 *'{company_name}'* 카드를 Trello에 새로 생성할까요?",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ 생성"},
                    "style": "primary",
                    "action_id": "trello_confirm_new_card",
                    "value": json.dumps({
                        "event_id": event_id,
                        "company": company_name,
                    }),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ 취소"},
                    "action_id": "trello_cancel_new_card",
                    "value": json.dumps({
                        "event_id": event_id,
                        "company": company_name,
                    }),
                },
            ],
        },
    ]

    slack_client.chat_postMessage(
        channel=user_id,
        text=f"🆕 '{company_name}' 카드를 Trello에 새로 생성할까요?",
        blocks=blocks,
    )


def handle_trello_confirm_new_card(slack_client, body: dict) -> None:
    """FR-A16: 신규 카드 생성 확인 → 실제 생성 및 등록"""
    user_id = body["user"]["id"]
    try:
        payload = json.loads(body["actions"][0]["value"])
        event_id = payload["event_id"]
        company_name = payload["company"]
    except (KeyError, json.JSONDecodeError) as e:
        log.warning(f"Trello 카드 생성 확인 payload 파싱 실패: {e}")
        slack_client.chat_postMessage(
            channel=user_id, text="❌ Trello 등록 실패: 잘못된 요청"
        )
        return

    result = trello.create_card(user_id, company_name)
    if result is None:
        slack_client.chat_postMessage(
            channel=user_id,
            text=f"❌ Trello 카드 생성 실패: {company_name}",
        )
        return

    _register_to_card(slack_client, user_id=user_id, event_id=event_id,
                      card_id=result["card_id"], card_name=result["card_name"])


def handle_trello_cancel_new_card(slack_client, body: dict) -> None:
    """FR-A16: 신규 카드 생성 취소"""
    user_id = body["user"]["id"]
    try:
        payload = json.loads(body["actions"][0]["value"])
        company_name = payload["company"]
    except (KeyError, json.JSONDecodeError):
        company_name = "알 수 없음"
    slack_client.chat_postMessage(
        channel=user_id,
        text=f"⏭️ *{company_name}* Trello 카드 생성을 건너뛰었습니다.",
    )


def _register_to_card(slack_client, *, user_id: str, event_id: str,
                      card_id: str, card_name: str) -> None:
    """지정된 카드에 액션아이템 체크리스트 + 회의록 요약 코멘트 등록"""
    items = user_store.get_action_items(event_id)
    if not items:
        slack_client.chat_postMessage(
            channel=user_id, text="액션아이템이 없어 Trello 등록을 건너뜁니다."
        )
        return

    checklist_items = []
    for it in items:
        checklist_items.append({
            "assignee": it.get("assignee", ""),
            "content": it.get("content", ""),
            "due_date": it.get("due_date"),
        })

    count = trello.add_checklist_items_by_id(user_id, card_id, checklist_items)

    # 회의록 요약을 카드 코멘트로 추가
    summary = _minutes_summary_cache.pop(event_id, "")
    if summary:
        try:
            trello.add_comment_by_id(user_id, card_id, f"📝 회의록 요약\n{summary}")
        except Exception as e:
            log.warning(f"Trello 카드 코멘트 추가 실패: {e}")

    if count > 0:
        card_info = trello.find_card_by_name(user_id, card_name)
        card_url = card_info["url"] if card_info else ""
        url_text = f"\n<{card_url}|카드 열기>" if card_url else ""
        slack_client.chat_postMessage(
            channel=user_id,
            text=(
                f"📌 *Trello 액션아이템 등록 완료*\n"
                f"*카드:* {card_name}\n"
                f"*등록 항목:* {count}건{url_text}"
            ),
        )
    else:
        slack_client.chat_postMessage(
            channel=user_id,
            text=f"❌ Trello 등록 실패: {card_name} 카드에 항목을 추가하지 못했습니다.",
        )


def handle_trello_register(slack_client, body: dict) -> None:
    """레거시 '등록' 버튼 핸들러 (하위 호환용)"""
    user_id = body["user"]["id"]
    try:
        payload = json.loads(body["actions"][0]["value"])
        event_id = payload["event_id"]
        company_name = payload["company"]
    except (KeyError, json.JSONDecodeError) as e:
        log.warning(f"Trello 등록 payload 파싱 실패: {e}")
        slack_client.chat_postMessage(
            channel=user_id, text="❌ Trello 등록 실패: 잘못된 요청"
        )
        return

    # 기존 방식: 업체명으로 카드 찾거나 생성
    checklist_items = []
    items = user_store.get_action_items(event_id)
    if not items:
        slack_client.chat_postMessage(
            channel=user_id, text="액션아이템이 없어 Trello 등록을 건너뜁니다."
        )
        return

    for it in items:
        checklist_items.append({
            "assignee": it.get("assignee", ""),
            "content": it.get("content", ""),
            "due_date": it.get("due_date"),
        })

    count = trello.add_checklist_items(user_id, company_name, checklist_items)

    summary = _minutes_summary_cache.pop(event_id, "")
    if summary:
        try:
            trello.add_comment(user_id, company_name, f"📝 회의록 요약\n{summary}")
        except Exception as e:
            log.warning(f"Trello 카드 코멘트 추가 실패: {e}")

    if count > 0:
        card_info = trello.find_card_by_name(user_id, company_name)
        card_url = card_info["url"] if card_info else ""
        url_text = f"\n<{card_url}|카드 열기>" if card_url else ""
        slack_client.chat_postMessage(
            channel=user_id,
            text=(
                f"📌 *Trello 액션아이템 등록 완료*\n"
                f"*카드:* {company_name}\n"
                f"*등록 항목:* {count}건{url_text}"
            ),
        )
    else:
        slack_client.chat_postMessage(
            channel=user_id,
            text=f"❌ Trello 등록 실패: {company_name} 카드에 항목을 추가하지 못했습니다.",
        )


def handle_trello_skip(slack_client, body: dict) -> None:
    """'건너뜀' 버튼 핸들러"""
    user_id = body["user"]["id"]
    slack_client.chat_postMessage(
        channel=user_id, text="이번에는 Trello 등록을 건너뛰었습니다."
    )


# ── D-3. Trello 카드 조회 ─────────────────────────────────────

def handle_trello_search(slack_client, *, user_id: str, query: str = "",
                         channel: str = None, thread_ts: str = None) -> None:
    """Trello 카드 조회. query가 있으면 유사 검색, 없으면 전체 목록."""
    token = user_store.get_trello_token(user_id)
    if not token:
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text="Trello 계정이 연결되어 있지 않습니다. `/trello`로 먼저 연결해주세요.",
        )
        return

    if query.strip():
        cards = trello.search_cards(user_id, query, limit=10)
        if not cards:
            slack_client.chat_postMessage(
                channel=channel or user_id, thread_ts=thread_ts,
                text=f"🔍 *'{query}'* 와 유사한 Trello 카드를 찾지 못했습니다.",
            )
            return
        title = f"🔍 *'{query}'* 검색 결과 ({len(cards)}건)"
    else:
        cards = trello.list_all_cards(user_id)
        if not cards:
            slack_client.chat_postMessage(
                channel=channel or user_id, thread_ts=thread_ts,
                text="📋 Trello 보드에 카드가 없습니다.",
            )
            return
        title = f"📋 *Trello 카드 목록* ({len(cards)}건)"

    # 제외할 리스트 필터링 후 카테고리별 그룹핑
    EXCLUDED_LISTS = {"Drop", "대기 (Pending)"}
    cards = [c for c in cards if c.get("list_name") not in EXCLUDED_LISTS]
    if not cards:
        slack_client.chat_postMessage(
            channel=channel or user_id, thread_ts=thread_ts,
            text="📋 표시할 Trello 카드가 없습니다.",
        )
        return

    from collections import OrderedDict
    grouped: OrderedDict[str, list] = OrderedDict()
    for c in cards:
        key = c.get("list_name") or "기타"
        grouped.setdefault(key, []).append(c)

    lines = []
    for list_name, group in grouped.items():
        lines.append(f"\n*📂 {list_name}* ({len(group)}건)")
        for c in group:
            lines.append(f"  • <{c['url']}|{c['card_name']}>")

    slack_client.chat_postMessage(
        channel=channel or user_id,
        thread_ts=thread_ts,
        text=f"{title}\n" + "\n".join(lines),
    )


# ── E. Contacts 자동 갱신 ────────────────────────────────────

def _update_contacts(
    creds,
    contacts_folder_id: str,
    recipients: list[dict],
    title: str,
    date_str: str,
) -> None:
    """외부 참석자 People/{이름}.md에 last_met 업데이트.
    미팅 히스토리 테이블 갱신은 during.py finalize_minutes에서 CM-08로 처리."""
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
