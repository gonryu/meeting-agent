"""Gmail API 래퍼 — 이전 이메일 맥락 수집 및 발송"""
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from datetime import datetime, timedelta
import base64
import re

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def _service(creds: Credentials):
    return build("gmail", "v1", credentials=creds)


def _decode_body(payload: dict) -> str:
    """이메일 본문 디코딩"""
    body = ""
    if payload.get("body", {}).get("data"):
        body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="ignore")
    elif payload.get("parts"):
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="ignore")
                break
    body = re.sub(r"<[^>]+>", "", body)
    return body.strip()[:500]


def parse_address_header(header_value: str) -> list[dict]:
    """'이름 <email>' 또는 'email' 형식의 헤더에서 [{name, email}] 추출"""
    results = []
    for part in header_value.split(","):
        part = part.strip()
        match = re.match(r'(.+?)\s*<([^>]+)>', part)
        if match:
            results.append({"name": match.group(1).strip(), "email": match.group(2).strip()})
        elif "@" in part:
            results.append({"name": "", "email": part.strip()})
    return results


def markdown_to_html(text: str) -> str:
    """마크다운 텍스트를 간단한 HTML로 변환 (이메일 발송용)"""
    lines = text.split("\n")
    html_lines = []
    in_list = False
    for line in lines:
        # 헤딩
        if line.startswith("### "):
            if in_list:
                html_lines.append("</ul>"); in_list = False
            html_lines.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("## "):
            if in_list:
                html_lines.append("</ul>"); in_list = False
            html_lines.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("# "):
            if in_list:
                html_lines.append("</ul>"); in_list = False
            html_lines.append(f"<h1>{line[2:]}</h1>")
        # 목록
        elif line.startswith("- ") or line.startswith("* "):
            if not in_list:
                html_lines.append("<ul>"); in_list = True
            item = line[2:]
            item = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", item)
            html_lines.append(f"<li>{item}</li>")
        # 구분선
        elif line.strip() in ("---", "***", "___"):
            if in_list:
                html_lines.append("</ul>"); in_list = False
            html_lines.append("<hr>")
        # 빈 줄
        elif not line.strip():
            if in_list:
                html_lines.append("</ul>"); in_list = False
            html_lines.append("")
        else:
            if in_list:
                html_lines.append("</ul>"); in_list = False
            line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
            html_lines.append(f"<p>{line}</p>")
    if in_list:
        html_lines.append("</ul>")
    body = "\n".join(html_lines)
    return f"""<html><body style="font-family:Arial,sans-serif;line-height:1.6;max-width:700px;margin:auto;padding:20px">
{body}
</body></html>"""


def send_email(creds: Credentials, to: list[str], subject: str, body_html: str) -> bool:
    """Gmail API로 HTML 이메일 발송
    Returns: 성공 여부
    """
    import logging
    log = logging.getLogger(__name__)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["To"] = ", ".join(to)
    msg.attach(MIMEText(body_html, "html", "utf-8"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        _service(creds).users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
        return True
    except Exception as e:
        log.error(f"Gmail 발송 실패: {e}")
        return False


def search_recent_emails(creds: Credentials, person_name: str,
                         company_name: str, days: int = 90) -> list[dict]:
    """
    상대방 이름 + 업체명으로 최근 이메일 검색
    Returns: [{"date", "subject", "snippet", "from", "to", "cc"}]
    """
    after = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
    if person_name and person_name != company_name:
        query = f'"{person_name}" "{company_name}" after:{after}'
    else:
        query = f'"{company_name}" after:{after}'

    svc = _service(creds)
    result = svc.users().messages().list(
        userId="me", q=query, maxResults=10
    ).execute()

    messages = result.get("messages", [])
    emails = []

    for msg in messages[:5]:
        detail = svc.users().messages().get(
            userId="me", id=msg["id"], format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}
        date_str = headers.get("Date", "")
        subject = headers.get("Subject", "(제목 없음)")
        body = _decode_body(detail.get("payload", {}))

        emails.append({
            "date": date_str,
            "subject": subject,
            "snippet": body or detail.get("snippet", ""),
            "from": headers.get("From", ""),
            "to": headers.get("To", ""),
            "cc": headers.get("Cc", ""),
        })

    return emails
