"""Gemini 프롬프트 템플릿"""
import json
from datetime import datetime


def company_news_prompt(company_name: str) -> str:
    today = datetime.now().strftime("%Y년 %m월 %d일")
    return f"""오늘({today}) 기준으로 '{company_name}'의 최근 동향을 검색해줘.

다음 형식으로 3~5개 항목을 반환해줘. 반드시 한국어로:
- [제목] (출처 URL)

조건:
- 투자, 신사업, 제품 출시, 파트너십, 주요 인사 등 비즈니스 관련 뉴스
- 각 항목에 출처 링크 필수 포함
- 없으면 "최근 공개된 정보 없음" 으로만 답변
"""


def person_info_prompt(person_name: str, company_name: str) -> str:
    return f"""'{company_name}'의 '{person_name}'에 대한 공개 정보를 검색해줘.

다음 형식으로 반환해줘. 반드시 한국어로:
- 직책/역할:
- LinkedIn: (URL 또는 없음)
- 주요 활동: (인터뷰, 발표, SNS 등 공개 정보, 출처 링크 포함)
- 성향/특이사항: (공개된 정보 기반)

조건:
- 동명이인 주의 — 반드시 '{company_name}' 소속임을 확인할 것
- 확인되지 않은 정보는 포함하지 말 것
- 공개 정보가 없으면 "공개 정보 없음" 으로만 답변
"""


def service_connection_prompt(company_info: str, knowledge: str) -> str:
    return f"""우리 회사 서비스와 상대 업체의 접점을 분석해줘. 반드시 한국어로.

[우리 회사 서비스]
{knowledge}

[상대 업체 정보]
{company_info}

다음 형식으로 2~3가지만 간결하게:
- [우리 서비스] ↔ [상대 업체 관심사/니즈]: 한 줄 설명

억지로 끼워맞추지 말고, 실제 접점이 없으면 "명확한 접점 없음"으로 답변.
"""


def briefing_summary_prompt(
    company_name: str,
    company_news: str,
    person_info: str,
    service_connections: str,
    email_context: str,
) -> str:
    return f"""다음 정보를 바탕으로 '{company_name}' 미팅 브리핑을 한국어로 작성해줘.

[업체 동향]
{company_news}

[담당자 정보]
{person_info}

[서비스 연결점]
{service_connections}

[이전 이메일 맥락]
{email_context}

각 섹션을 간결하게 bullet point로 정리해줘. 불필요한 인사말 없이 바로 내용만.
"""


def parse_meeting_prompt(user_message: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    return f"""다음 메시지에서 미팅 정보를 추출해줘. 오늘 날짜는 {today}이야.

메시지: "{user_message}"

JSON 형식으로만 답변 (다른 텍스트 없이):
{{
  "date": "YYYY-MM-DD",
  "time": "HH:MM",
  "duration_minutes": 60,
  "participants": ["실제 담당자 개인 이름1", "실제 담당자 개인 이름2"],
  "participant_emails": {{"이름1": "email@example.com"}},
  "company": "외부 기관/업체명 또는 null",
  "title": "미팅 제목",
  "agenda": "어젠다 (없으면 빈 문자열)"
}}

추출 규칙:
- participants: **실제 개인 담당자 이름만** 포함
  - 기관명·업체명·약어(KISA, 삼성전자, 카카오 등)는 절대 포함하지 말 것
  - 개인 이름이 없으면 빈 배열 []
  - 이름만 추출 ("김민환(kim@co.com)" → "김민환")
- company: 미팅 상대방인 **외부 기관·업체·단체명**
  - "KISA 미팅" → "KISA"
  - "삼성전자 홍길동 미팅" → "삼성전자"
  - "팀 스탠드업", "내부 회의", "점심" 등 내부/사적 일정 → null
  - 불확실하면 null
- participant_emails: 메시지에 이메일이 명시된 경우만 포함, 없으면 {{}}
- "15시" → "15:00", "오후 3시" → "15:00", "오전 10시" → "10:00"
- 시간 언급 없으면 "09:00"
- "오늘" → {today}, "내일" → 오늘 날짜 +1일 계산
- duration 언급 없으면 60
"""


def merge_meeting_prompt(existing_info: dict, new_message: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    existing_json = json.dumps(existing_info, ensure_ascii=False, indent=2)
    return f"""현재 진행 중인 일정 드래프트가 있어. 사용자의 새 메시지가 이 일정에 대한 추가/수정 정보인지 판단하고, 맞다면 드래프트를 업데이트해줘. 오늘 날짜는 {today}.

현재 드래프트:
{existing_json}

새 메시지: "{new_message}"

판단 규칙:
- 일정 관련 정보(제목, 참석자, 어젠다, 날짜, 시간, 소요시간 등)를 제공하는 메시지면 is_update: true
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
    "company": null,
    "title": "미팅 제목",
    "agenda": ""
  }},
  "changed_fields": ["변경된 필드명 목록"]
}}

업데이트 규칙:
- 명시되지 않은 필드는 기존 드래프트 값 그대로 유지
- "참석자 추가해줘 홍길동" → participants에 홍길동 추가 (기존 유지)
- "참석자는 홍길동이야" → participants를 [홍길동]으로 대체
- participants는 개인 이름만, 업체명 제외
- is_update가 false면 updated_info는 기존 드래프트 그대로, changed_fields는 []
"""


def minutes_internal_prompt(meeting_title: str, meeting_date: str, attendees: str,
                             transcript: str, notes_text: str) -> str:
    """내부용 회의록 — 전체 내용, 전략적 맥락 포함"""
    sources = []
    if transcript:
        sources.append(f"[트랜스크립트]\n{transcript[:40000]}")
    if notes_text:
        sources.append(f"[수동 노트]\n{notes_text}")
    sources_block = "\n\n".join(sources) if sources else "(자료 없음)"

    return f"""다음 자료를 바탕으로 내부용 회의록을 작성해줘. 반드시 한국어로.

중요 원칙:
- 트랜스크립트와 수동 노트에 실제로 언급된 내용만 작성할 것
- 내용을 유추하거나 추론하지 말 것. 자료에 없는 내용은 절대 추가하지 말 것
- 불명확한 부분은 그대로 "(불명확)" 또는 생략할 것

[미팅 정보]
- 제목: {meeting_title}
- 날짜/시간: {meeting_date}
- 참석자: {attendees}

{sources_block}

다음 마크다운 형식으로 작성:

## 회의 요약
(자료에 실제로 언급된 핵심 내용만 3~5줄로 요약. 유추/추론 금지)

## 액션 아이템
(반드시 포함. 자료에 언급된 것만. 없으면 "없음"으로 명시)
| 담당자 | 내용 | 기한 |
|--------|------|------|

## 주요 결정 사항
(자료에서 명확히 결정된 것만)
- ...

## 주요 논의 내용
(주제별로 실제 논의된 내용 정리. 전략적 맥락 포함)

## 내부 메모
(주의사항, 후속 전략, 상대방 관찰 등 내부에서만 공유할 내용)
"""


def minutes_external_prompt(meeting_title: str, meeting_date: str, attendees: str,
                              internal_minutes: str) -> str:
    """외부용 회의록 — 상대방과 공유 가능한 정제된 버전"""
    return f"""다음 내부용 회의록을 바탕으로 외부 공유용 회의록을 작성해줘. 반드시 한국어로.
외부용이므로 양측이 합의한 내용과 공유 액션 아이템만 포함하고,
내부 전략이나 상대방에 대한 평가 등 내부 의견은 제외해줘.
전문적이고 중립적인 톤으로 작성해줘.

[미팅 정보]
- 제목: {meeting_title}
- 날짜/시간: {meeting_date}
- 참석자: {attendees}

[내부용 회의록]
{internal_minutes}

다음 마크다운 형식으로 작성:

## 회의 개요
(양측이 공유할 수 있는 미팅 목적과 배경)

## 주요 합의 사항
- ...

## 공동 액션 아이템
| 담당자 | 내용 | 기한 |
|--------|------|------|

## 다음 단계
(향후 일정 및 협의 방향)
"""


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
