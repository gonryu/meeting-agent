# LLM 사용 현황

> 최종 갱신: 2026-04-02 | 회의록 생성 Claude Sonnet 전환, 업체명 자동추출 제거, 통합 스레드 업데이트

---

## 1. 모델 구성

| 역할 | 모델 | 비고 |
|------|------|------|
| 기본 | Gemini `gemini-2.0-flash` | Google Search 도구 포함 |
| 폴백 | Claude `claude-haiku-4-5` | Gemini 실패(429 등) 시 자동 전환 |
| 회의록 생성·수정 전용 | Claude `claude-sonnet-4-5` | `agents/during.py` `_generate_minutes()`, Gemini 폴백 없음 |
| 명함 OCR 전용 | Claude `claude-haiku-4-5` (Vision) | `agents/card.py` 단독 사용, Gemini 미사용 |

---

## 2. 호출 함수

| 함수 | 에이전트 | 도구 | 용도 |
|------|----------|------|------|
| `_search(prompt)` | Before | Gemini + GoogleSearch / Claude + web_search | 웹 검색이 필요한 경우 |
| `_generate(prompt)` | Before, During, After | Gemini / Claude (검색 없음) | 텍스트 생성·분석만 필요한 경우 |
| `_generate_minutes(prompt)` | During | Claude Sonnet 직접 호출 | 회의록 생성·수정 전용 (Gemini 폴백 없음) |
| `ocr_business_card(image_bytes)` | Card | Claude Haiku Vision (직접 호출) | 명함 이미지 OCR + 구조화 |

> **STT (음성→텍스트)**: LLM 미사용. Deepgram REST API (`nova-2`, `tools/stt.py`) 별도 처리.

---

## 3. Before Agent LLM 호출

### 3.1 업체 뉴스 검색

- **위치**: `agents/before.py` → `research_company()`
- **함수**: `_search`
- **프롬프트**: `prompts/briefing.py` → `company_news_prompt(company_name)`

```
오늘(YYYY년 MM월 DD일) 기준으로 '{company_name}'의 최근 동향을 검색해줘.

다음 형식으로 3~5개 항목을 반환해줘. 반드시 한국어로:
- [제목] (출처 URL)

조건:
- 투자, 신사업, 제품 출시, 파트너십, 주요 인사 등 비즈니스 관련 뉴스
- 각 항목에 출처 링크 필수 포함
- 없으면 "최근 공개된 정보 없음" 으로만 답변
```

---

### 3.2 인물 정보 검색

- **위치**: `agents/before.py` → `research_person()`
- **함수**: `_search`
- **프롬프트**: `prompts/briefing.py` → `person_info_prompt(person_name, company_name)`

```
'{company_name}'의 '{person_name}'에 대한 공개 정보를 검색해줘.

다음 형식으로 반환해줘. 반드시 한국어로:
- 직책/역할:
- LinkedIn: (URL 또는 없음)
- 주요 활동: (인터뷰, 발표, SNS 등 공개 정보, 출처 링크 포함)
- 성향/특이사항: (공개된 정보 기반)

조건:
- 동명이인 주의 — 반드시 '{company_name}' 소속임을 확인할 것
- 확인되지 않은 정보는 포함하지 말 것
- 공개 정보가 없으면 "공개 정보 없음" 으로만 답변
```

---

### 3.3 서비스 연결점 분석

- **위치**: `agents/before.py` → `research_company()`
- **함수**: `_generate`
- **프롬프트**: `prompts/briefing.py` → `service_connection_prompt(company_info, knowledge)`

```
우리 회사 서비스와 상대 업체의 접점을 분석해줘. 반드시 한국어로.

[우리 회사 서비스]
{company_knowledge.md 내용}

[상대 업체 정보]
{업체 뉴스 검색 결과}

다음 형식으로 2~3가지만 간결하게:
- [우리 서비스] ↔ [상대 업체 관심사/니즈]: 한 줄 설명

억지로 끼워맞추지 말고, 실제 접점이 없으면 "명확한 접점 없음"으로 답변.
```

---

### 3.4 자연어 미팅 파싱

- **위치**: `agents/before.py` → `create_meeting_from_text()`
- **함수**: `_generate`
- **프롬프트**: `prompts/briefing.py` → `parse_meeting_prompt(user_message)`

### 3.4-2 미팅 초안 병합 (대화형 미팅 생성)

- **위치**: `agents/before.py` → `update_meeting_from_text()`
- **함수**: `_generate`
- **프롬프트**: `prompts/briefing.py` → `merge_meeting_prompt(existing_info, new_message)`

```
기존 미팅 초안이 있고, 사용자가 새 메시지를 보냈습니다.
이 메시지가 기존 초안의 업데이트인지, 아니면 무관한 메시지인지 판단해줘.

[기존 미팅 초안]
{existing_info}

[새 메시지]
{new_message}

JSON으로만 반환:
{"is_update": true/false, "updated_info": {...}, "changed_fields": [...]}
```

---

### 3.5 자연어 미팅 파싱

```
다음 메시지에서 미팅 정보를 추출해줘. 오늘 날짜는 {today}이야.

메시지: "{user_message}"

JSON 형식으로만 답변 (다른 텍스트 없이):
{
  "date": "YYYY-MM-DD",
  "time": "HH:MM",
  "duration_minutes": 60,
  "participants": ["이름1", "이름2"],
  "participant_emails": {"이름1": "email@example.com"},
  "title": "미팅 제목",
  "agenda": "어젠다 (없으면 빈 문자열)",
  "location": "장소 (없으면 빈 문자열)"
}

추출 규칙:
- participants: 이름만 추출 ("김민환(kim@co.com)" → "김민환")
- participant_emails: 메시지에 이메일이 명시된 경우만 포함, 없으면 {}
- "15시" → "15:00", "오후 3시" → "15:00", "오전 10시" → "10:00"
- 시간 언급 없으면 "09:00"
- "오늘" → {today}, "내일" → 오늘 날짜 +1일 계산
- duration 언급 없으면 60
- location: 명시된 장소가 없으면 빈 문자열 ""
```

---

### ~~3.5 업체명 추출~~ (제거됨)

> 2026-04-02: `_extract_company_with_llm()`, `_extract_company_name()`, `_get_internal_products_from_knowledge()` 함수 제거.
> 업체명은 `extendedProperties.private.company`로만 식별 (일정 생성 시 사용자가 명시하거나 스레드에서 설정).

---

### 3.6 company_knowledge.md 갱신

- **위치**: `agents/before.py` → `update_company_knowledge()`
- **함수**: `_generate`
- **프롬프트**: `prompts/briefing.py` → `update_knowledge_prompt(drive_files_content)`

```
다음 자료를 바탕으로 우리 회사(아이콘루프/파라메타) 서비스 요약을
{today} 기준으로 업데이트해줘.

[자료]
{현재 company_knowledge.md 내용}

다음 마크다운 형식으로 작성:
# 아이콘루프 (ICONLOOP) 서비스 요약

## 회사 개요
## 주요 제품 및 서비스
## 핵심 강점
## 서비스 연결 포인트 (미팅 활용)
| 상대 업체 관심사 | 연결 가능 서비스 |

*last_updated: {today}*
```

---

## 4. During Agent LLM 호출

> During Agent는 트랜스크립트(자동) + 수동 노트를 결합하여 **내부용·외부용** 2종의 회의록을 생성합니다.
> 내부용은 `_generate_and_post_minutes()`에서 1회 호출하여 초안을 저장하고,
> 외부용은 `[저장 및 완료]` 확정 후 `finalize_minutes()`에서 별도로 생성합니다.

### 4.1 내부용 회의록 생성 (1차 호출)

- **위치**: `agents/during.py` → `_generate_and_post_minutes()`
- **함수**: `_generate_minutes` (Claude `claude-sonnet-4-5` 직접 호출)
- **프롬프트**: `prompts/briefing.py` → `minutes_internal_prompt(meeting_title, meeting_date, attendees, transcript, notes_text)`
- **입력**: 트랜스크립트 (있으면) + 수동 노트 (있으면) 조합

```
다음 자료를 바탕으로 내부용 회의록을 작성해줘. 반드시 한국어로.
내부용이므로 전략적 판단, 내부 의견, 주의사항 등 모든 논의 내용을 포함해줘.

[미팅 정보]
- 제목: {meeting_title}
- 날짜/시간: {meeting_date}
- 참석자: {attendees}

[트랜스크립트]         ← 있는 경우만 포함
{transcript[:8000]}

[수동 노트]            ← 있는 경우만 포함
{notes_text}

다음 마크다운 형식으로 작성:

## 회의 요약
## 주요 결정 사항
## 액션 아이템
## 주요 논의 내용
## 내부 메모
```

---

### 4.2 외부용 회의록 생성 (2차 호출)

- **위치**: `agents/during.py` → `finalize_minutes()` — `'저장 및 완료'` 확정 후 생성
- **함수**: `_generate_minutes` (Claude `claude-sonnet-4-5` 직접 호출)
- **프롬프트**: `prompts/briefing.py` → `minutes_external_prompt(meeting_title, meeting_date, attendees, internal_minutes)`
- **입력**: 최종 확정된 내부용 회의록 전문 (직접 편집 내용 반영 후)

```
다음 내부용 회의록을 바탕으로 외부 공유용 회의록을 작성해줘. 반드시 한국어로.
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
## 주요 합의 사항
## 공동 액션 아이템
## 다음 단계
```

---

### 4.3 회의록 수정 요청 재생성 (검토 단계)

- **위치**: `agents/during.py` → `handle_minutes_edit_reply()`
- **함수**: `_generate_minutes` (Claude Sonnet, 1회 — 내부용만)
- **트리거**: `[✏️ 수정 요청]` 버튼 클릭 후 스레드 답글

```
다음 회의록을 아래 수정 요청에 따라 수정해줘. 반드시 한국어로.

[기존 회의록]
{internal_body}

[수정 요청]
{edit_text}

수정된 전체 회의록을 동일한 마크다운 형식으로 반환해줘.
```

내부용만 재생성 → 새 초안 메시지 발송.
외부용은 재생성하지 않으며, `[저장 및 완료]` 시 `finalize_minutes()`에서 최종 내부용을 기반으로 생성.

### 4.4 입력 소스 조합 규칙

| 트랜스크립트 | 수동 노트 | 생성 방식 |
|------------|---------|---------|
| O | O | 두 소스 모두 포함하여 내부용 생성 |
| O | X | 트랜스크립트만으로 내부용 생성 |
| X | O | 노트만으로 내부용 생성 (90분 fallback 포함) |
| X | X | 해당 없음 (트랜스크립트 없으면 처리 안 함) |

---

## 5. After Agent LLM 호출

> After Agent는 회의록 생성 완료 직후 백그라운드 스레드로 실행됩니다.
> LLM은 1회 호출됩니다: 내부용 회의록에서 액션아이템 추출.

### 5.1 액션아이템 추출

- **위치**: `agents/after.py` → `_extract_and_save_action_items()`
- **함수**: `_generate`
- **프롬프트**: `prompts/briefing.py` → `extract_action_items_prompt(internal_body)`

```
다음 회의록에서 액션아이템만 추출해줘.

[회의록]
{internal_body}

JSON 배열로만 답변해줘. 다른 텍스트 없이 JSON만:
[
  {"assignee": "담당자 이름 (없으면 null)", "content": "액션아이템 내용", "due_date": "YYYY-MM-DD (없으면 null)"}
]

조건:
- 명확한 할 일이나 결정된 작업만 포함 (논의 내용 제외)
- 담당자가 명시되지 않은 경우 null
- 기한이 명시되지 않은 경우 null
- 액션아이템이 없으면 빈 배열 [] 반환
```

추출 결과는 `store/user_store.py` → `action_items` 테이블에 저장됩니다.

---

### 5.2 후속 일정 패턴 감지

LLM 호출 없이 키워드 매칭으로 처리합니다.

- **위치**: `agents/after.py` → `_suggest_followup()`
- **방식**: 내부용 회의록 본문에서 아래 키워드 포함 여부를 단순 검색

```python
_FOLLOWUP_PATTERNS = [
    "다음 미팅", "후속 미팅", "다시 만나", "follow-up", "follow up",
    "후속 일정", "다음에 만나", "재미팅",
]
```

패턴 감지 시 Slack Block Kit 버튼 메시지 발송 (LLM 미사용).

---

### 3.7 자연어 인텐트 분류

- **위치**: `main.py` → `_classify_intent(text)`
- **함수**: `generate_text` (before.py의 `_generate` public 래퍼)
- **프롬프트**: `main.py` → `_INTENT_PROMPT` (인라인 정의)

```
다음 메시지의 의도를 분류해줘.

메시지: "{text}"

아래 인텐트 중 하나로 분류하고, 관련 파라미터를 추출해줘.
JSON으로만 응답해줘:
{"intent": "...", "params": {...}}

인텐트 목록:
- briefing: 브리핑 요청
- create_meeting: 미팅 생성 요청 (params: title, date, time, participants)
- start_session: 미팅 시작 (params: title)
- add_note: 노트 추가 (params: note)
- end_session: 미팅 종료
- get_minutes: 회의록 조회
- research_company: 기업 리서치 (params: company)
- research_person: 인물 리서치 (params: person, company)
- update_knowledge: 지식 갱신
- dreamplus_book: 드림플러스 회의실 예약 (params: start, end, title, attendee_count, ...)
- dreamplus_list: 내 회의실 예약 목록 조회
- dreamplus_cancel: 회의실 예약 취소
- help: 도움말·사용법 요청
- unknown: 위 항목에 해당 없음
```

분류 실패(JSON 파싱 오류) 시 `{"intent": "unknown", "params": {}}` 반환.
`unknown` 인텐트 수신 시 `/도움말` 안내 메시지 발송 (단순 에러 메시지 → 도움말 안내로 개선, 2026-04-02).

---

## 6. Card Agent LLM 호출 (명함 OCR)

### 6.1 명함 OCR

- **위치**: `agents/card.py` → `ocr_business_card(image_bytes)`
- **모델**: Claude `claude-haiku-4-5` (Vision) — `_OCR_MODEL = "claude-haiku-4-5"`
- **입력**: Slack에서 다운로드한 명함 이미지 바이트 (base64 인코딩 후 전달)
- **처리 흐름**: DM 이미지 업로드 감지 (`handle_image_upload()`) → 백그라운드 스레드에서 이미지 다운로드 → Claude Haiku Vision 호출 → 구조화된 dict 반환

**반환 구조**:
```python
{
    "name": str,
    "company": str,
    "title": str,
    "department": str,
    "phone": str,
    "mobile": str,
    "fax": str,
    "email": str,
    "address": str,
    "website": str,
    "sns": str,
}
```

**후속 처리**:
- OCR 결과를 `_pending_cards[user_id]`에 임시 저장
- Block Kit UI 발송: ✅저장 (`card_confirm_save`) / ✏️수정 (`card_open_edit`) / ❌취소 (`card_cancel`)
- 저장: `research_person(card_data=card_data)` → `People/{이름}.md` 자동 생성/갱신
- 수정: 필드별 편집 모달 (`card_edit_modal`), OCR 값 `initial_value` 사전 입력 (비어있으면 생략)

---

## 6. 프롬프트 템플릿 파일 관리

> 2026-04-02: 주요 프롬프트를 `prompts/templates/` 외부 파일로 분리.
> `prompts/briefing.py`의 각 함수는 `_load_template(filename)`으로 파일을 읽고, `str.replace("{{var}}", value)` 방식으로 변수를 치환하여 반환.

| 템플릿 파일 | 대응 함수 | 변수 |
|------------|----------|------|
| `minutes_internal.md` | `minutes_internal_prompt()` | `{{title}}`, `{{date}}`, `{{attendees}}`, `{{sources}}` |
| `minutes_external.md` | `minutes_external_prompt()` | `{{title}}`, `{{date}}`, `{{attendees}}`, `{{internal_minutes}}` |
| `company_news.md` | `company_news_prompt()` | `{{today}}`, `{{company_name}}` |
| `person_info.md` | `person_info_prompt()` | `{{person_name}}`, `{{company_name}}` |
| `service_connection.md` | `service_connection_prompt()` | `{{knowledge}}`, `{{company_info}}` |
| `briefing_summary.md` | `briefing_summary_prompt()` | `{{company_name}}`, `{{company_news}}`, `{{person_info}}`, `{{service_connections}}`, `{{email_context}}` |

인라인 프롬프트로 유지되는 함수: `parse_meeting_prompt`, `merge_meeting_prompt`, `update_knowledge_prompt`, `extract_action_items_prompt`

---

## 7. 미사용 프롬프트

| 프롬프트 | 위치 | 현황 |
|---------|------|------|
| `briefing_summary_prompt` | `prompts/briefing.py` | 정의만 있고 호출 없음. 브리핑 메시지는 LLM 없이 `build_meeting_header_block()`, `build_company_research_block()`, `build_persons_block()`, `build_context_block()`으로 Slack blocks 직접 조합 (2026-04-02 비동기화 리팩토링). |
| `minutes_from_transcript_prompt` | `prompts/briefing.py` | `minutes_internal_prompt`의 하위 호환 래퍼. 직접 호출 없음. |
| `minutes_from_notes_prompt` | `prompts/briefing.py` | `minutes_internal_prompt`의 하위 호환 래퍼. 직접 호출 없음. |
