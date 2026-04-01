"""Slack 메시지 빌더 및 표준 질의 문구"""
import re
from datetime import datetime

# LLM이 생성하는 도입/마무리 문구 패턴 — 업체동향에서 제외
_PREAMBLE_KEYWORDS = (
    "검색해 드리겠습니다",
    "알려드리겠습니다",
    "정리해 드리겠습니다",
    "살펴보겠습니다",
    "다음과 같습니다",
    "다음은",
    "검색 결과",
    "이상입니다",
    "없음으로 답변",
    "검색을 진행하겠습니다",
)

def _is_preamble(text: str) -> bool:
    return any(kw in text for kw in _PREAMBLE_KEYWORDS)


_URL_RE = re.compile(r'https?://[^\s\)\]>|]+')


def _slack_linkify(text: str) -> str:
    """뉴스 항목 텍스트에서 URL을 링크로 변환. URL 자체는 노출하지 않고 텍스트에 링크를 검.

    처리 순서:
    1. [제목](URL) 또는 [제목] (URL) → <URL|제목>
    2. 텍스트 (URL) 형태 → <URL|텍스트>
    3. 남은 bare URL → <URL|링크>  (이미 변환된 <...> 내부는 건드리지 않음)
    """
    # 1. [제목](URL) 또는 [제목] (URL)
    text = re.sub(
        r'\[([^\]]+)\]\s*\((https?://[^\s\)]+)\)',
        lambda m: f"<{m.group(2)}|{m.group(1)}>",
        text,
    )
    # 2. 텍스트 (URL) — [제목] 형태의 괄호 제거
    def _fmt_link(url: str, label: str) -> str:
        label = label.strip()
        # [제목] 또는 [제목] 설명 → 제목만 추출
        m = re.match(r'^\[([^\]]+)\](.*)', label)
        if m:
            title = m.group(1).strip()
            extra = m.group(2).strip()
            label = f"{title}  {extra}".strip() if extra else title
        return f"<{url}|{label}>"

    text = re.sub(
        r'([^<\s>][^<>]*?)\s*\((https?://[^\s\)]+)\)',
        lambda m: _fmt_link(m.group(2), m.group(1)),
        text,
    )
    # 3. bare URL — <...> 토큰은 건드리지 않도록 분리 후 처리
    parts = re.split(r'(<[^>]+>)', text)
    result = []
    for part in parts:
        if part.startswith('<') and part.endswith('>'):
            result.append(part)
        else:
            result.append(_URL_RE.sub(lambda m: f"<{m.group(0)}|링크>", part))
    return ''.join(result)


def format_time(iso_str: str) -> str:
    """ISO 시간 → 한국어 표시 (예: 오후 3:00). 오늘이 아닌 경우 날짜 포함 (예: 3/31 오후 3:00).
    날짜만 있으면 '종일' 반환."""
    if not iso_str:
        return ""
    # 종일 이벤트: "2026-03-24" 형태 (T 없음)
    if "T" not in iso_str:
        try:
            d = datetime.strptime(iso_str, "%Y-%m-%d")
            return f"{d.month}/{d.day} 종일"
        except Exception:
            return iso_str
    try:
        from zoneinfo import ZoneInfo
        KST = ZoneInfo("Asia/Seoul")
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(KST)
        today = datetime.now(KST).date()
        hour = dt.hour
        minute = dt.minute
        period = "오전" if hour < 12 else "오후"
        h = hour if hour <= 12 else hour - 12
        time_part = f"{period} {h}:{minute:02d}"
        if dt.date() != today:
            weekdays = ["월", "화", "수", "목", "금", "토", "일"]
            dow = weekdays[dt.weekday()]
            return f"{dt.month}/{dt.day}({dow}) {time_part}"
        return time_part
    except Exception:
        return iso_str


def build_briefing_message(
    meeting: dict,
    company_name: str,
    company_news: list[str],
    persons: list[dict],
    service_connections: list[str],
    previous_context: dict,
    parascope_content: list[str] | None = None,
) -> list[dict]:
    """브리핑 Slack Block Kit 메시지 생성"""
    time_str = format_time(meeting.get("start_time", ""))
    meet_link = meeting.get("meet_link", "")
    platform = "Google Meet" if meet_link else "미팅"
    link_text = f"<{meet_link}|{platform}>" if meet_link else platform

    meeting_title = meeting.get("summary", company_name)
    location = meeting.get("location", "")
    location_str = f" · 📍{location}" if location else ""
    lines = [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"*📋 {meeting_title} ({company_name}) — {time_str} ({link_text}){location_str}*",
    ]

    # ParaScope 브리핑 (있을 때만)
    if parascope_content:
        lines.append("🔭  *ParaScope 브리핑*")
        for item in parascope_content:
            lines.append(f"{_slack_linkify(item)}")
        lines.append("")

    # 업체 동향 (도입 문구 제외, URL 링크화)
    lines.append("🏢  *업체 동향*")
    filtered_news = [n for n in company_news if not _is_preamble(n)]
    if filtered_news:
        for news in filtered_news[:3]:
            lines.append(f"• {_slack_linkify(news)}")
    else:
        lines.append("• 최근 동향 정보 없음")

    lines.append("")

    # 담당자 (있을 때만)
    if persons:
        lines.append("👤  *담당자*")
        for p in persons:
            name = p.get("name", "")
            role = p.get("role", "")
            link = p.get("linkedin", "")
            memo = p.get("memo", "")
            person_line = f"• {name}"
            if role:
                person_line += f" / {role}"
            if link:
                person_line += f" (<{link}|LinkedIn>)"
            lines.append(person_line)
            if memo:
                lines.append(f"  └ 메모: {memo}")
        lines.append("")

    # 서비스 연결점
    lines.append("🔗  *파라메타 서비스 연결점*")
    if service_connections:
        for conn in service_connections[:3]:
            lines.append(f"• {conn}")
    else:
        lines.append("• 분석 정보 없음")

    lines.append("")

    # 이전 미팅 맥락 (회의록)
    lines.append("📌  *이전 미팅 맥락*")
    trello_items = previous_context.get("trello", [])
    emails = previous_context.get("emails", [])
    minutes = previous_context.get("minutes", [])

    if trello_items:
        lines.append(f"• Trello 미완료: {' / '.join(trello_items[:3])}")
    for m in minutes:
        name = m.get("name", "").replace("_내부용.md", "").replace("_", " ")
        modified = m.get("modifiedTime", "")[:10]
        file_id = m.get("id", "")
        link = f"https://drive.google.com/file/d/{file_id}/view" if file_id else ""
        link_text = f" <{link}|열기>" if link else ""
        lines.append(f"• 회의록: {name} ({modified}){link_text}")
    if not trello_items and not minutes:
        lines.append("• 이전 미팅 기록 없음")

    lines.append("")

    # 이메일 맥락 (이전 미팅 맥락 다음)
    lines.append("📧  *이메일 맥락*")
    if emails:
        for email in emails[:3]:
            snippet = email.get("snippet", "")[:60]
            date = email.get("date", "")
            subject = email.get("subject", "")
            lines.append(f"• {date}  {subject or snippet}")
    else:
        lines.append("• 이메일 기록 없음")

    lines.append("")

    # 어젠다
    agenda = meeting.get("description", "").strip()
    if agenda:
        lines.append("📝  *어젠다*")
        for line in agenda.splitlines():
            if line.strip():
                lines.append(f"• {line.strip()}")
    else:
        lines.append("📝  *어젠다 등록하려면 이 스레드에 답장하세요*")
        lines.append("_(입력 내용은 Calendar 이벤트에도 자동 반영됩니다)_")

    return [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}]


# ── 표준 질의 문구 ──────────────────────────────────────────

def ask_is_external_meeting(event_name: str, time_str: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"📅 *{event_name}* ({time_str}) 은 외부 미팅인가요?\n_(1시간 내 미응답 시 외부 미팅으로 간주하여 자동 브리핑 진행합니다)_"},
        },
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ 네"}, "value": f"external|{event_name}", "action_id": "confirm_external"},
                {"type": "button", "text": {"type": "plain_text", "text": "⏭️ 아니요"}, "value": f"internal|{event_name}", "action_id": "confirm_internal"},
            ],
        },
    ]


def ask_company_name(event_name: str) -> str:
    return f"📋 *{event_name}* 미팅을 브리핑하려는데, 상대 업체명이 확인되지 않아요.\n어느 업체와의 미팅인가요? (예: 카카오, 네이버)"


def ask_email(person_name: str) -> str:
    return f"👤 *{person_name}* 님의 이메일 주소를 찾지 못했어요.\n직접 입력해주시면 캘린더 초대와 Contacts에 저장할게요."


def ask_save_contacts(person_name: str, summary: str) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"💾 *{person_name}* 님에 대한 새 정보가 있어요.\n{summary}\nContacts에 저장할까요?"},
        },
        {
            "type": "actions",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ 저장"}, "value": f"save|{person_name}", "action_id": "save_contact"},
                {"type": "button", "text": {"type": "plain_text", "text": "❌ 건너뜀"}, "value": f"skip|{person_name}", "action_id": "skip_contact"},
            ],
        },
    ]
