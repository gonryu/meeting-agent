"""Gemini 프롬프트 템플릿"""
import json
import os
from datetime import datetime

_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


def _load_template(filename: str) -> str:
    """prompts/templates/ 에서 템플릿 파일 로드"""
    path = os.path.join(_TEMPLATES_DIR, filename)
    with open(path, encoding="utf-8") as f:
        return f.read()


def company_news_prompt(company_name: str) -> str:
    today = datetime.now().strftime("%Y년 %m월 %d일")
    template = _load_template("company_news.md")
    return template.replace("{{today}}", today).replace("{{company_name}}", company_name)


def person_info_prompt(person_name: str, company_name: str) -> str:
    template = _load_template("person_info.md")
    return template.replace("{{person_name}}", person_name).replace("{{company_name}}", company_name)


def service_connection_prompt(company_info: str, knowledge: str) -> str:
    template = _load_template("service_connection.md")
    return template.replace("{{knowledge}}", knowledge).replace("{{company_info}}", company_info)


def briefing_summary_prompt(
    company_name: str,
    company_news: str,
    person_info: str,
    service_connections: str,
    email_context: str,
) -> str:
    template = _load_template("briefing_summary.md")
    return (template
            .replace("{{company_name}}", company_name)
            .replace("{{company_news}}", company_news)
            .replace("{{person_info}}", person_info)
            .replace("{{service_connections}}", service_connections)
            .replace("{{email_context}}", email_context))


def parse_meeting_prompt(user_message: str) -> str:
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    today = now.strftime("%Y-%m-%d")
    now_time = now.strftime("%H:%M")
    return f"""다음 메시지에서 미팅 정보를 추출해줘. 오늘 날짜는 {today}이야. 현재 시각은 {now_time} (KST)이야.

메시지: "{user_message}"

JSON 형식으로만 답변 (다른 텍스트 없이):
{{
  "date": "YYYY-MM-DD",
  "time": "HH:MM",
  "duration_minutes": 60,
  "participants": ["실제 담당자 개인 이름1", "실제 담당자 개인 이름2"],
  "participant_emails": {{"이름1": "email@example.com"}},
  "company_candidates": ["업체명1", "업체명2"],
  "company_confirmed": false,
  "title": "미팅 제목",
  "agenda": "어젠다 (없으면 빈 문자열)",
  "location": "장소 (없으면 빈 문자열)"
}}

추출 규칙:
- participants: **실제 개인 담당자 이름만** 포함
  - 기관명·업체명·약어(KISA, 삼성전자, 카카오 등)는 절대 포함하지 말 것
  - 개인 이름이 없으면 빈 배열 []
  - 이름만 추출 ("김민환(kim@co.com)" → "김민환")
- company_candidates: 메시지에서 감지된 외부 기관/업체명 목록 (배열)
  - "업체는 한국은행이야" → ["한국은행"], company_confirmed: true
  - "KISA 미팅 잡아줘" → ["KISA"], company_confirmed: false
  - "한국은행과 미팅, 업체명은 한국은행" → ["한국은행"], company_confirmed: true
  - "KISA 블록체인특성화사업 평가 사전미팅" → ["KISA"], company_confirmed: false
  - "내일 3시 홍길동 미팅" → [], company_confirmed: false
  - 기관명·약어도 업체 후보에 포함 (participants에는 넣지 말 것)
- company_confirmed: **"업체는 XXX"**, **"업체명은 XXX"** 처럼 명시적 언급이면 true, 아니면 false
- participant_emails: 메시지에 이메일이 명시된 경우만 포함, 없으면 {{}}
- "15시" → "15:00", "오후 3시" → "15:00", "오전 10시" → "10:00"
- "2시간 뒤" → 현재 시각({now_time})에 2시간 추가하여 계산
- "30분 뒤" / "30분 후" → 현재 시각에 30분 추가
- "내일 이 시간" → 내일 날짜 + 현재 시각({now_time})
- 상대 시간 계산 시 날짜가 넘어가면 date도 함께 업데이트
- 시간 언급 없으면 "09:00"
- "오늘" → {today}, "내일" → 오늘 날짜 +1일 계산
- duration 언급 없으면 60
"""


def merge_meeting_prompt(existing_info: dict, new_message: str) -> str:
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    today = now.strftime("%Y-%m-%d")
    now_time = now.strftime("%H:%M")
    existing_json = json.dumps(existing_info, ensure_ascii=False, indent=2)
    return f"""현재 진행 중인 일정 드래프트가 있어. 사용자의 새 메시지가 이 일정에 대한 추가/수정 정보인지 판단하고, 맞다면 드래프트를 업데이트해줘. 오늘 날짜는 {today}. 현재 시각은 {now_time} (KST).

현재 드래프트:
{existing_json}

새 메시지: "{new_message}"

판단 규칙:
- 일정 관련 정보(제목, 참석자, 어젠다, 날짜, 시간, 소요시간, 장소, 업체 등)를 제공하는 메시지면 is_update: true
- 전혀 다른 주제("브리핑 해줘", "회의실 예약해줘", "회사 알아봐줘" 등)면 is_update: false

JSON으로만 반환 (설명 없이):
{{
  "is_update": true,
  "updated_info": {{
    "date": "YYYY-MM-DD",
    "time": "HH:MM",
    "duration_minutes": 60,
    "participants": [],
    "participant_emails": {{}},
    "company_candidates": [],
    "company_confirmed": false,
    "title": "미팅 제목",
    "agenda": "",
    "location": ""
  }},
  "changed_fields": ["변경된 필드명 목록"]
}}

업데이트 규칙:
- 명시되지 않은 필드는 기존 드래프트 값 그대로 유지
- "참석자 추가해줘 홍길동" → participants에 홍길동 추가 (기존 유지)
- "참석자는 홍길동이야" → participants를 [홍길동]으로 대체
- participants는 개인 이름만, 업체명 제외
- company_candidates: 새로 감지된 업체명이 있으면 배열에 추가. "업체는 XXX" 명시적 언급이면 company_confirmed: true
- 업체 관련 언급 없으면 기존 값 유지
- is_update가 false면 updated_info는 기존 드래프트 그대로, changed_fields는 []
"""


def minutes_internal_prompt(meeting_title: str, meeting_date: str, attendees: str,
                             transcript: str, notes_text: str) -> str:
    """내부용 회의록 — prompts/templates/minutes_internal.md 템플릿 사용.

    transcript가 이미 전처리된 상태(원본 또는 요약)로 전달됩니다.
    """
    sources = []
    if transcript and notes_text:
        sources.append(
            "[입력 소스 안내]\n"
            "트랜스크립트와 수동 노트가 모두 제공됩니다.\n"
            "- 트랜스크립트: 실제 발언 기록으로, 논의 내용의 1차 소스입니다. 발언자와 맥락을 충실히 반영하세요.\n"
            "- 수동 노트: 참석자가 직접 기록한 핵심 사항입니다. 트랜스크립트에 없는 보충 정보로 활용하세요.\n"
            "- 두 소스가 충돌하면 트랜스크립트를 우선합니다."
        )
    sources.append("")
    if transcript:
        sources.append(f"[트랜스크립트]\n{transcript}")
    if notes_text:
        sources.append(f"[수동 노트]\n{notes_text}")
    sources_block = "\n\n".join(sources).strip() if any(s.strip() for s in sources) else "(자료 없음)"

    template = _load_template("minutes_internal.md")
    return (template
            .replace("{{title}}", meeting_title)
            .replace("{{date}}", meeting_date)
            .replace("{{attendees}}", attendees)
            .replace("{{sources}}", sources_block))


def minutes_external_prompt(meeting_title: str, meeting_date: str, attendees: str,
                              internal_minutes: str) -> str:
    """외부용 회의록 — prompts/templates/minutes_external.md 템플릿 사용"""
    template = _load_template("minutes_external.md")
    return (template
            .replace("{{title}}", meeting_title)
            .replace("{{date}}", meeting_date)
            .replace("{{attendees}}", attendees)
            .replace("{{internal_minutes}}", internal_minutes))


# 하위 호환성 유지 (기존 코드에서 참조 시)
def minutes_from_transcript_prompt(transcript: str, meeting_title: str,
                                   meeting_date: str, attendees: str) -> str:
    return minutes_internal_prompt(meeting_title, meeting_date, attendees, transcript, "")


def minutes_from_notes_prompt(notes_text: str, meeting_title: str, started_at: str) -> str:
    return minutes_internal_prompt(meeting_title, started_at, "정보 없음", "", notes_text)


def update_knowledge_prompt(drive_files_content: str) -> str:
    today = datetime.now().strftime("%Y년 %m월 %d일")
    return f"""다음 자료를 바탕으로 우리 회사(아이콘루프/파라메타) 서비스 요약을 {today} 기준으로 업데이트해줘.

[자료]
{drive_files_content}

다음 마크다운 형식으로 작성:
# 아이콘루프 (ICONLOOP) 서비스 요약

## 회사 개요
...

## 주요 제품 및 서비스
### 1. 제품명
...

## 핵심 강점
...

## 서비스 연결 포인트 (미팅 활용)
| 상대 업체 관심사 | 연결 가능 서비스 |
...

*last_updated: {today}*
"""


def extract_action_items_prompt(internal_body: str) -> str:
    return f"""다음 회의록에서 액션아이템만 추출해줘.

[회의록]
{internal_body}

JSON 배열로만 답변해줘. 다른 텍스트 없이 JSON만:
[
  {{"assignee": "담당자 이름 (없으면 null)", "content": "액션아이템 내용", "due_date": "YYYY-MM-DD (없으면 null)"}}
]

조건:
- 명확한 할 일이나 결정된 작업만 포함 (논의 내용 제외)
- 담당자가 명시되지 않은 경우 null
- 기한이 명시되지 않은 경우 null
- 액션아이템이 없으면 빈 배열 [] 반환
"""
