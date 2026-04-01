# Before 에이전트 설계 문서

> 최종 갱신: 2026-04-01 | 명함 OCR, Dreamplus 연동, _to_bullet_lines 헬퍼, 업체 파일 섹션 순서 변경, 참석자 이메일 다중 후보 선택 UI, Gmail/Google Contacts 이메일 조회 추가

---

## 1. 개요

Before 에이전트는 미팅 준비 단계를 자동화하는 오케스트레이터입니다.
Google Calendar에서 오늘의 미팅을 감지하고, 업체/인물 리서치 → 이전 맥락 수집 → 브리핑 생성 → Slack 발송까지 전 과정을 처리합니다.

- **파일 경로**: `agents/before.py`
- **LLM**: Gemini `gemini-2.0-flash` (우선) → Claude `claude-haiku-4-5` (폴백)
- **인터페이스**: Slack (DM + 채널 멘션 + Slash Command)
- **Google 연동**: Google API Python Client (OAuth 2.0 Credentials)
- **사용자 관리**: 다중 사용자, 사용자별 독립 Drive/Calendar/Gmail

---

## 2. 트리거

| 유형 | 발동 조건 | 동작 |
|------|-----------|------|
| **자동** | APScheduler, 매일 09:00 KST | 전체 등록 사용자 순회 → 오늘 미팅 브리핑 |
| **수동** | `/brief`, `/브리핑`, 자연어 DM/멘션 | 즉시 브리핑 생성 |
| **미팅 생성 후** | `create_meeting_from_text()` 완료 시 | 생성된 미팅에 대한 즉시 브리핑 |
| **기업 리서치** | `/company {업체명}`, `/기업 {업체명}` | 강제 리서치 후 Drive 저장 |
| **인물 리서치** | `/person {이름} (회사)`, `/인물 {이름} (회사)` | 강제 리서치 후 Drive 저장 + 연관 기업 갱신 |
| **지식 갱신** | `/update`, `/업데이트` | `company_knowledge.md` 자동 재작성 |

---

## 3. 전체 실행 흐름

```
[트리거: 09:00 자동 / Slack 수동]
         │
         ▼
run_briefing(slack_client, user_id, event=None)
         │
         ├─ event 지정 시: 해당 이벤트만 처리
         └─ event=None: cal.get_upcoming_meetings(creds, days=1)
                              오늘 KST 자정~자정 조회
         │
         ▼
drive.get_company_names(creds, contacts_folder_id)
   Contacts/Companies 폴더 업체명 목록 캐시
         │
         ▼ 각 이벤트별 루프
cal.parse_event(ev)  →  meeting dict
         │
         ▼
_extract_company_name(meeting, known_companies)
  1순위: 외부 도메인 참석자 확인 (외부 미팅 여부 판별)
         → 단, 제목에 Contacts 업체명 있으면 그 한국어 정식명 우선 반환
         → 없으면 도메인 앞부분 반환 (예: hanwhainvestment.com → hanwhainvestment)
  2순위: 제목 ∋ known_companies (참석자 없는 경우)
  3순위: LLM으로 제목에서 업체명 추출
         │
    ┌────┴────────────┐
    │ company_name 있음  │ 없음 (내부 미팅)
    ▼                   ▼
_send_briefing()     _send_internal_briefing()
    │                   │
    │                   └─ 간단 정보 + 어젠다 입력 안내
    │
    ├─ research_company()       업체 정보 수집
    ├─ research_person() × 3   담당자 정보 수집 (최대 3명)
    ├─ get_previous_context()  Gmail 이전 이메일 + Drive 회의록
    │
    ▼
build_briefing_message() → Slack blocks
_post() → DM 또는 채널 스레드 발송
    │
    ▼
_pending_agenda[msg_ts] = (event_id, user_id)
   브리핑 스레드 답장 대기 등록
```

---

## 4. 외부 미팅 식별 로직

```python
INTERNAL_DOMAINS = {"parametacorp.com", "iconloop.com"}  # tools/calendar.py (환경변수 INTERNAL_DOMAINS)

# 공개 이메일 서비스 도메인 — 업체명 추출 대상에서 제외
_PUBLIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com",
    "naver.com", "daum.net", "hanmail.net",
    "yahoo.com", "yahoo.co.kr", "hotmail.com", "outlook.com",
    "icloud.com", "me.com", "mac.com", "nate.com", "empas.com",
}

def _extract_company_name(meeting, known_companies) -> str | None:
    # 1순위: 외부 도메인 참석자 확인 (공개 이메일 서비스 제외)
    is_external = any(
        domain not in INTERNAL_DOMAINS and domain not in _PUBLIC_EMAIL_DOMAINS
        for a in meeting["attendees"]
        if (domain := a["email"].split("@")[-1])
    )

    if is_external:
        # 제목에 Contacts 업체명 있으면 한국어 정식명 우선 반환
        for company in (known_companies or []):
            if company.lower() in summary.lower():
                return company
        # 없으면 도메인 앞부분 반환
        for attendee in meeting["attendees"]:
            domain = attendee["email"].split("@")[-1]
            if domain not in INTERNAL_DOMAINS and domain not in _PUBLIC_EMAIL_DOMAINS:
                return domain.split(".")[0]

    # 2순위: 제목에 Contacts/Companies 업체명 포함 여부
    for company in (known_companies or []):
        if company.lower() in summary.lower():
            return company

    # 3순위: LLM으로 제목에서 업체명 추출
    result = _generate(extract_company_prompt(summary))
    if result and result.lower() != "null" and len(result) <= 30:
        return result
    return None
```

> **설계 의도**: 1순위는 외부 미팅 *여부*를 판별하고, 업체 *이름*은 Contacts 정식명(한국어)을 우선 사용한다.
> 이렇게 해야 Drive 회의록 파일명(`한화투자증권_내부용.md`)과 Gmail 검색 쿼리가 정확하게 동작한다.

---

## 5. LLM 호출 구조

```python
from google import genai
_gemini = genai.Client(api_key=GOOGLE_API_KEY)
_GEMINI_MODEL = "gemini-2.0-flash"

_claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
_CLAUDE_MODEL = "claude-haiku-4-5"
```

| 함수 | 동작 |
|------|------|
| `_search(prompt)` | Gemini + GoogleSearch → 실패 시 Claude + web_search |
| `_generate(prompt)` | Gemini generate → 실패 시 Claude messages |

---

## 6. 기능 상세

### 6.1 업체 리서치 (`research_company`)

```python
def research_company(user_id, company_name, force=False) -> (content, file_id):
```

```
drive.get_company_info() → (content, file_id, is_fresh)
  is_fresh=True and not force: 캐시 반환 (7일 이내)
  그 외:
    │
    ├─ 0단계: _query_parascope(company_name)   ParaScope 채널 조회
    │         #meeting-agent-testing 채널에 "@ParaScope {업체명}" 전송
    │         → "생성 중" 메시지 건너뛰고 실제 응답 대기 (최대 60초, 3초 간격 폴링)
    │         → text + blocks + attachments 전체 내용 수집
    │         → 없으면 None (경고 로그 후 계속 진행)
    │
    ├─ 1단계: gmail.search_recent_emails(creds, company_name, company_name)
    │         쿼리: "{company_name}" after:{90일전}
    │         → 날짜 | 제목 | 본문100자 형식으로 이메일 맥락 구성
    │
    ├─ 2단계: _search(company_news_prompt(company_name))  웹 검색
    │         → _clean_news_text()로 LLM 프리앰블 제거 후 저장
    │
    ├─ 3단계: _generate(service_connection_prompt(news, knowledge))  연결점 분석
    │
    └─ drive.save_company_info()  → Companies/{업체명}.md 저장/갱신
```

저장 구조:
```markdown
# {업체명}

## 최근 동향
- last_searched: YYYY-MM-DD
- [기사 제목] (https://출처URL)
- [기사 제목] (https://출처URL)

## 이메일 맥락
- last_searched: YYYY-MM-DD
- 2026-03-20 | 제목 | snippet (newlines stripped)

## 파라메타 서비스 연결점
- [ICONLOOP DID] ↔ [관심사]: 설명

## ParaScope 브리핑
- last_searched: YYYY-MM-DD
- bullet line
```

> 섹션 순서: `최근 동향` → `이메일 맥락` → `파라메타 서비스 연결점` → `ParaScope 브리핑`
> `last_searched` 라인은 Drive에 저장되지만 브리핑 출력에는 표시하지 않음.
> 이메일 snippet의 줄바꿈(`\n`, `\r`)은 저장 전 공백으로 치환됨.

#### ParaScope 채널 연동 (`_query_parascope`)

Slack 봇 토큰은 다른 봇에게 DM 전송이 불가(`cannot_dm_bot`)하므로, 두 봇이 모두 멤버인 채널(`#meeting-agent-testing`)을 경유합니다.

```python
_PARASCOPE_BOT_ID       # ParaScope 봇 유저 ID (환경변수 PARASCOPE_BOT_ID)
_PARASCOPE_BOT_APP_ID   # ParaScope 봇 App ID (환경변수 PARASCOPE_BOT_APP_ID)
_PARASCOPE_CHANNEL_ID   # 공유 채널 ID (환경변수 PARASCOPE_CHANNEL_ID)

def _query_parascope(company_name, timeout=60) -> str | None:
    1. chat_postMessage(channel, text=f"<@PARASCOPE> {company_name}")
    2. conversations_history 폴링 (3초 간격, 최대 timeout초)
    3. "생성 중" / "hourglass" 포함 메시지 → 건너뜀
    4. text + blocks[].text + attachments[].text 전체 합산 반환
    5. 응답 없으면 None 반환 (경고 로그)
```

#### 뉴스 프리앰블 필터링 (`_clean_news_text`)

LLM이 뉴스 앞에 삽입하는 안내 문구("검색하겠습니다", "다음과 같습니다" 등)를 Drive 저장 전 제거합니다.

```python
_NEWS_PREAMBLE_KEYWORDS = (
    "검색하겠습니다", "검색해 드리겠습니다", "알려드리겠습니다",
    "정리해 드리겠습니다", "살펴보겠습니다", "다음과 같습니다",
    # 2026-04-01 추가
    "추천합니다", "추천드립니다", "확인하시기 바랍니다", "참고하시기 바랍니다",
    "더 있을 수 있", "추가로 확인", "도움이 되", "위의 정보", "위 정보를",
    "이외에도", "더 자세한 정보", "기타 정보",
    # markdown 헤딩(#으로 시작하는 줄)도 필터링
)
def _clean_news_text(text) -> str:
    # 프리앰블 키워드 포함 줄 및 '#'으로 시작하는 헤딩 줄 제거 후 반환
```

#### 브리핑 출력 정규화 (`_to_bullet_lines`)

브리핑에 표시할 텍스트를 `- ` 불릿 형식으로 정규화합니다:

```python
def _to_bullet_lines(text) -> list[str]:
    # 모든 줄을 '- ' 불릿으로 정규화
    # 보일러플레이트(last_searched 등) 줄 필터링
    # '#'으로 시작하는 헤딩 줄 제거
```

### 6.2 인물 리서치 (`research_person`)

```python
def research_person(user_id, person_name, company_name, force=False, card_data: dict = None) -> (content, file_id):
```

`card_data`가 전달되면 People 파일에 `## 명함 정보` 섹션을 추가합니다.
이메일 우선순위: `card_data["email"]` > Gmail 헤더 자동 추출.

```
drive.get_person_info() → (content, file_id)
  content 있음 and not force: 캐시 반환
  그 외:
    │
    ├─ 1단계: gmail.search_recent_emails(creds, person_name, company_name)
    │         쿼리: "{person_name}" "{company_name}" after:{90일전}
    │         → 이메일 맥락 구성
    │         → From/To/CC 헤더에서 person_name 이름 매칭 → 이메일 주소 자동 추출
    │
    ├─ 2단계: _search(person_info_prompt(person_name, company_name))  웹 검색
    │
    ├─ drive.save_person_info()  → People/{이름}.md 저장/갱신
    │
    └─ research_company(user_id, company_name, force=False)  연관 기업정보 자동 갱신
```

저장 구조:
```markdown
# {이름}

## 기본 정보
- 소속: {company_name}
- last_searched: YYYY-MM-DD
- 이메일: (card_data 또는 Gmail 헤더 자동 추출)

## 명함 정보    ← 명함 OCR 시에만 추가 (card_data 전달 시)
- last_updated: YYYY-MM-DD
- 직책: 팀장
- 부서: AI혁신팀
- 전화: 02-xxx
- 휴대폰: 010-xxx
- (기타 명함 필드, 값이 있는 항목만 기록)

## 이메일 맥락
- 날짜 | 제목 | 본문...

## 공개 정보
{웹 검색 결과}
```

### 6.3 이전 맥락 수집 (`get_previous_context`)

```python
def get_previous_context(user_id, company_name, person_names) -> dict:
    # Gmail: 참석자 최대 2명 × 90일 이내 이메일 검색, 최대 3개
    emails = [...]

    # Drive 회의록: minutes_folder_id 내 파일명에 업체명 포함된 _내부용.md, 최대 3개
    # NFD/NFC 유니코드 정규화 적용 (macOS 업로드 파일은 NFD, 코드 문자열은 NFC)
    minutes = [...]  # [{id, name, modifiedTime}]

    return {"trello": [], "emails": emails[:3], "minutes": minutes}
```

브리핑 표시:
- **회의록**: 파일명 + 날짜 + Drive 열기 링크 (최대 3개)
- **이메일**: 가장 최근 1개의 snippet 앞 60자
- 둘 다 없으면: "이전 미팅 기록 없음"

> ⚠️ Trello 미구현 (`"trello": []` 하드코딩)

### 6.4 브리핑 메시지 (`_send_briefing`)

#### 브리핑 인트로 메시지

`run_briefing()` 호출 시 `event=None`(전체 브리핑)이면 브리핑 목록 출력 전 먼저 인트로 메시지를 발송합니다:

```
📅 {display_name}님의 향후 24시간 일정을 보여드리겠습니다.
```

브리핑 표시 섹션 순서:
1. 미팅 기본 정보 (제목, 시간, 장소, Google Meet 링크)
2. 업체 최근 동향 (최대 3줄, URL은 텍스트 링크로 변환 — 원시 URL 미표시)
3. 담당자 정보 (최대 3명)
4. 파라메타 서비스 연결점 (최대 3줄)
5. 이전 미팅 맥락 (Drive 회의록 최대 3개, Drive 열기 링크 포함)
6. 이메일 맥락 📧 (Gmail 최근 이메일 최대 1개 — 별도 섹션)
7. 어젠다: Calendar 이벤트 `description`에 내용이 있으면 표시, 없으면 스레드 답장 안내

#### URL 링크화 (`_slack_linkify`)

뉴스 텍스트 내 URL을 Slack `<URL|텍스트>` 형식으로 변환합니다:
- `[제목](URL)` 또는 `[제목] (URL)` → `<URL|제목>`
- `텍스트 (URL)` → `<URL|텍스트>`
- 나머지 bare URL → `<URL|링크>`
- 이미 변환된 `<...>` 토큰은 재처리하지 않음

#### 브리핑 시간 범위

현재 시각 기준 **24시간 이내** 미팅만 표시합니다. 이미 시작된(과거) 미팅은 포함하지 않습니다.
또한 제목이 `집` 또는 `사무실`이고 종일 이벤트인 경우 브리핑 대상에서 제외합니다.

발송 후 `_pending_agenda[msg_ts] = (event_id, user_id)` 등록.

### 6.5 어젠다 등록 (`handle_agenda_reply`)

```
브리핑 스레드 답장 감지
  └─ _pending_agenda[thread_ts] → (event_id, user_id)
       ├─ cal.update_event_description(creds, event_id, "[어젠다]\n{text}")
       ├─ "✅ 어젠다 등록 완료" 답장
       └─ _pending_agenda에서 삭제
```

### 6.6 자연어 미팅 생성 (`create_meeting_from_text`)

```
_generate(parse_meeting_prompt(message)) → JSON
  {"title", "date", "time", "duration_minutes", "participants", "participant_emails", "agenda"}
  │
  ├─ 참석자별 이메일 후보 수집 (_find_email_candidates):
  │    1. LLM 인라인 이메일 → 바로 사용
  │    2. Slack users_list() 이름 매칭
  │    3. Gmail 헤더 검색 (find_email_by_name)
  │    4. Google Contacts 조회 (find_email_in_contacts, contacts.readonly 스코프 필요)
  │    5. Drive People/{이름}.md 이메일 파싱
  │    중복 제거 후 순서 유지 → list[str] 반환
  │
  ├─ 후보 수 별 처리:
  │    ① LLM 인라인 이메일 있음 → 직접 사용
  │    ② 후보 1개 → 자동 선택
  │    ③ 후보 2개 이상 → pending_selections에 추가 (Block Kit 선택 UI 대기)
  │    ④ 후보 없음 → missing_names에 추가 (경고 후 건너뜀)
  │
  ├─ pending_selections 있음:
  │    └─ _pending_meetings[user_id] = 전체 대기 상태 저장
  │       _post_email_selection() → 첫 번째 모호한 이름에 대한 버튼 UI 발송
  │       (사용자 클릭 대기 후 handle_email_selection() 호출로 이어짐)
  │
  └─ 모두 확정 시:
       missing_names 경고 Slack 메시지 후
       _create_calendar_event() → Calendar 이벤트 + Google Meet 생성
       run_briefing() → 생성된 미팅 즉시 브리핑
```

#### 이메일 후보 수집 내부 함수

| 함수 | 설명 |
|------|------|
| `_find_email_candidates(user_id, name, slack_client) -> list[str]` | 모든 소스에서 이메일 후보를 수집, 중복 제거 후 순서 유지한 리스트 반환 |
| `_find_email(user_id, name, slack_client) -> str \| None` | `_find_email_candidates()` 래퍼 — 첫 번째 결과만 반환 (하위 호환) |

#### 다중 후보 선택 UI

| 함수 | 설명 |
|------|------|
| `_post_email_selection(slack_client, user_id, selection, channel, thread_ts)` | Block Kit 버튼 UI 발송: 후보 이메일별 버튼 + "이 참석자 제외" (danger) |
| `handle_email_selection(slack_client, body)` | `select_attendee_email` 버튼 클릭 처리 — 선택 완료 시 다음 미확정 항목 UI 또는 `_create_calendar_event()` 호출 |

모듈 수준 상태: `_pending_meetings: dict[str, dict]` — user_id 키로 대기 중인 미팅 생성 상태 저장.

#### Calendar 이벤트 생성 (`_create_calendar_event`)

`create_meeting_from_text()`에서 분리된 함수. 이메일이 모두 확정된 후 호출:

```python
def _create_calendar_event(slack_client, user_id, info, company, attendee_emails, channel, thread_ts):
    # cal.create_event() → Calendar 이벤트 + Google Meet 생성
    # run_briefing() → 생성된 미팅 즉시 브리핑
```

### 6.7 company_knowledge.md 갱신 (`update_company_knowledge`)

```
drive.get_company_knowledge()
  └─ _generate(update_knowledge_prompt(current))
       └─ drive.update_company_knowledge()
```

---

## 7. Gmail 연동 (`tools/gmail.py`)

### `search_recent_emails`
```python
def search_recent_emails(creds, person_name, company_name, days=90) -> list[dict]:
    # person_name != company_name: "{person}" "{company}" after:...  (AND 조건)
    # company만: "{company}" after:...
    # Returns: [{"date", "subject", "snippet", "from", "to", "cc"}]
```

### `parse_address_header`
```python
def parse_address_header(header_value) -> list[dict]:
    # "이름 <email>" 또는 "email" 형식 파싱
    # 쉼표 구분 복수 주소 지원
    # Returns: [{"name": str, "email": str}]
```

### `find_email_by_name`
```python
def find_email_by_name(creds, name) -> str | None:
    # Gmail 검색 쿼리: "{name}" (metadata-only, 빠름)
    # From/To/Cc 헤더에서 name과 일치하는 이메일 주소 추출
    # 일치 없으면 None 반환
```

### `find_email_in_contacts`
```python
def find_email_in_contacts(creds, name) -> str | None:
    # Google People API searchContacts 사용
    # contacts.readonly 스코프 필요 — 스코프 없으면 gracefully None 반환
    # name과 일치하는 첫 번째 이메일 주소 반환
```

---

## 8. Drive 파일 관리 (`tools/drive.py`)

### `_find_file` — NFD + NFC 이중 검색
```python
def _find_file(creds, name, parent_id):
    for form in ("NFD", "NFC"):    # macOS 업로드(NFD), 봇 생성(NFC) 모두 대응
        normalized = unicodedata.normalize(form, name)
        # Drive API 쿼리 후 찾으면 반환
```

### `_write_file` — 중복 생성 방지 (upsert)
```python
def _write_file(creds, name, content, parent_id, file_id=None):
    if not file_id:
        existing = _find_file(creds, name, parent_id)  # 기존 파일 검색
        if existing:
            file_id = existing["id"]
    if file_id:
        # 기존 파일 업데이트
    else:
        # 신규 파일 생성 (NFD 이름으로 저장)
```

---

## 9. 사용자별 격리 구조

### `_get_creds_and_config(user_id)`
```python
creds = user_store.get_credentials(user_id)    # 복호화된 Google Credentials
user  = user_store.get_user(user_id)
return creds, user["contacts_folder_id"], user["knowledge_file_id"]
```

모든 Drive/Calendar/Gmail API 호출 시 사용자별 `creds` 전달.

### `_post()` 헬퍼
```python
def _post(slack_client, *, user_id, channel=None, thread_ts=None, text=None, blocks=None):
    channel = channel or user_id    # 채널 없으면 DM
```

---

## 10. 에러 처리

| 오류 | 처리 |
|------|------|
| Gemini 429 (할당량 초과) | Claude 폴백 |
| JSON 파싱 실패 | 원본 응답과 함께 사용자 알림 |
| 참석자 이메일 미발견 | 경고 로그 후 해당 참석자 건너뛰고 계속 진행 |
| Drive API 오류 | 예외 캐치, 사용자 알림 |
| Gmail 검색 실패 | 경고 로그 후 이메일 맥락 없이 진행 |

---

## 11. 파일 구조

```
meeting-agent/
├── .env
├── main.py                     # Slack Bolt + Scheduler 진입점
├── agents/
│   ├── before.py               # Before 에이전트
│   ├── during.py               # During 에이전트
│   ├── after.py                # After 에이전트
│   ├── card.py                 # 명함 OCR 에이전트 (Claude Haiku Vision)
│   └── room.py                 # Dreamplus 회의실 예약 에이전트
├── tools/
│   ├── calendar.py             # Google Calendar API 래퍼
│   ├── docs.py                 # Google Docs API 래퍼
│   ├── drive.py                # Google Drive API 래퍼
│   ├── gmail.py                # Gmail API 래퍼 (검색 + 헤더 파싱)
│   ├── slack_tools.py          # 브리핑 메시지 빌더
│   └── dreamplus.py            # Dreamplus API 클라이언트
├── prompts/
│   └── briefing.py             # LLM 프롬프트 템플릿
├── store/
│   └── user_store.py           # SQLite + Fernet 사용자 토큰 관리
├── server/
│   └── oauth.py                # FastAPI OAuth 콜백 서버
└── docs/
    ├── requirements.md
    ├── before-agent-design.md
    ├── during-agent-design.md
    └── llm-usage.md
```

---

## 12. 미구현 / 개선 예정

| 항목 | 현황 |
|------|------|
| Trello 연동 | `"trello": []` 하드코딩 |
| 인물 정보 7일 신선도 체크 | 파일 존재 여부만 확인 |
| OAuth 토큰 자동 갱신 | 만료 시 `/재등록` 필요 |
| 이메일 본문 LLM 요약 | 본문 앞 100자 저장 |
