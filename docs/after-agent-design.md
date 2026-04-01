# After Agent 설계 문서

> 작성일: 2026-03-25
> 최종 갱신: 2026-03-26
> 상태: 구현 완료 (Phase 4)
> 목적: 미팅 종료 후 사후 관리 자동화

---

## 1. 개요

After Agent는 During Agent가 회의록 생성을 완료한 직후 자동으로 트리거되어,
외부용 회의록 발송, 액션아이템 추적, Contacts 갱신, 후속 일정 제안 등을 처리합니다.

- **파일 경로**: `agents/after.py` (신규 생성)
- **LLM**: Gemini `gemini-2.0-flash` / Claude 폴백 (기존 `_generate` 재사용)
- **트리거**: `agents/during.py` → `_generate_and_post_minutes()` 완료 직후 호출
- **사용자 관리**: 다중 사용자, 사용자별 독립 Drive/Credentials

---

## 2. 트리거 진입점

During Agent의 `_generate_and_post_minutes()` 가 Drive 저장 + Slack 발송 후 종료되는 시점에
After Agent를 호출합니다.

```python
# agents/during.py — _generate_and_post_minutes() 끝부분 (현재 line 517 이후)

_post_combined_minutes(...)   # 기존 Slack 발송 (현재 마지막 라인)

# After Agent 트리거 추가 예정
after.trigger_after_meeting(
    slack_client,
    user_id=user_id,
    event_id=event_id,          # 캘린더 이벤트 ID (있는 경우)
    title=title,
    date_str=date_str,
    attendees=attendees,        # 쉼표 구분 문자열
    internal_body=internal_body,
    external_body=external_body,
    creds=creds,
)
```

### After Agent로 전달되는 데이터

| 변수 | 타입 | 출처 |
|------|------|------|
| `user_id` | str | Slack 주최자 ID |
| `event_id` | str \| None | Google Calendar 이벤트 ID |
| `title` | str | 미팅 제목 |
| `date_str` | str | "YYYY-MM-DD" |
| `attendees` | str | 쉼표 구분 이름/이메일 문자열 |
| `internal_body` | str | LLM 생성 내부용 회의록 (마크다운) |
| `external_body` | str | LLM 생성 외부용 회의록 (마크다운) |
| `creds` | Credentials | 주최자 Google OAuth 자격증명 |

---

## 3. 전체 흐름도

```
_generate_and_post_minutes() 완료
          │
          ▼
after.trigger_after_meeting()
          │
          ├─ [Step A] 참석자 이메일 조회
          │    └─ event_id 있음 → Calendar API get_event_attendees()
          │    └─ event_id 없음 → Drive People/ 파일 이름 매칭
          │
          ├─ [Step B] 액션아이템 추출 → DB 저장
          │    └─ LLM: extract_action_items_prompt(internal_body)
          │    └─ action_items 테이블에 INSERT
          │
          ├─ [Step C] 외부용 회의록 Draft → Slack Block Kit 버튼 발송
          │    └─ pending_drafts 테이블에 INSERT (status='pending')
          │    └─ Slack DM: "외부용 회의록 발송 준비" + [발송하기] [발송 안 함]
          │
          ├─ [Step D] 액션아이템 담당자 Slack DM 알림
          │    └─ 담당자별 그룹핑 → Slack 멤버 이름 매칭 → DM 발송
          │
          └─ [Step E] Contacts 자동 갱신
               └─ 참석자 People/{이름}.md → last_met, 미팅 이력 업데이트

[APScheduler 매일 08:00]
  └─ action_item_reminder()
       └─ due_date = 오늘 or 내일인 open 항목 → 담당자 Slack DM

[Slack Block Kit 버튼 이벤트]
  └─ "발송하기" → gmail.send_email() → pending_drafts.status='sent'
  └─ "발송 안 함" → pending_drafts.status='cancelled'
```

---

## 4. 사전 작업 (구현 전 필수)

### 4.1 Gmail 발송 스코프 추가 ✅

```python
# 반드시 두 파일 모두 추가해야 함
# server/oauth.py  — OAuth URL 생성 시 Google에 요청할 스코프 목록 (이 파일이 핵심)
# store/user_store.py — 토큰 복원 시 사용하는 스코프 목록
"https://www.googleapis.com/auth/gmail.send"
```

> ⚠️ **스코프 변경 후 전체 사용자 `/재등록` 필요**
> `server/oauth.py` 누락 시 Google 동의 화면에서 권한 자체가 요청되지 않아 403 `insufficientPermissions` 발생

### 4.2 DB 스키마 추가

`store/user_store.py`의 `init_db()`에 아래 두 테이블을 추가합니다.
기존 패턴(컬럼 없으면 `ALTER TABLE` 자동 추가)과 동일하게 구현합니다.

```sql
-- 액션아이템 테이블
CREATE TABLE IF NOT EXISTS action_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL,
    user_id      TEXT NOT NULL,       -- 주최자 Slack ID
    assignee     TEXT,                -- 담당자 이름 (NULL 허용: 미지정)
    content      TEXT NOT NULL,
    due_date     TEXT,                -- "YYYY-MM-DD" (NULL 허용)
    status       TEXT DEFAULT 'open', -- open | done
    created_at   TEXT NOT NULL
);

-- 외부 발송 Draft 대기 테이블
CREATE TABLE IF NOT EXISTS pending_drafts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    title           TEXT,
    external_body   TEXT NOT NULL,
    recipients      TEXT,             -- JSON: [{"name": "...", "email": "..."}]
    status          TEXT DEFAULT 'pending',  -- pending | sent | cancelled
    created_at      TEXT NOT NULL
);
```

### 4.3 Calendar 참석자 조회 함수 추가

```python
# tools/calendar.py
def get_event_attendees(creds, event_id: str) -> list[dict]:
    """캘린더 이벤트에서 참석자 이름+이메일 반환
    Returns: [{"name": "...", "email": "..."}]
    """
```

### 4.4 Gmail 발송 함수 추가

```python
# tools/gmail.py
def send_email(creds, to: list[str], subject: str, body_html: str) -> bool:
    """Gmail API로 이메일 발송
    to: 수신자 이메일 주소 목록
    Returns: 성공 여부
    """
```

---

## 5. 구현 단계

### Step 1 — 공통 인프라

| 작업 | 파일 | 내용 |
|------|------|------|
| gmail.send 스코프 추가 | `tools/calendar.py` | SCOPES 리스트 |
| `send_email()` 함수 | `tools/gmail.py` | Gmail API `users.messages.send` |
| `get_event_attendees()` 함수 | `tools/calendar.py` | Calendar API 이벤트 참석자 조회 |
| DB 테이블 추가 | `store/user_store.py` | `action_items`, `pending_drafts` 생성 |
| DB CRUD 함수 | `store/user_store.py` | `save_action_items`, `get_pending_draft`, `update_draft_status` 등 |

---

### Step 2 — 구조화 액션아이템 추출

LLM으로 내부용 회의록에서 액션아이템을 파싱합니다.

**프롬프트** (`prompts/briefing.py`에 추가):
```
다음 회의록에서 액션아이템만 추출해줘.

[회의록]
{internal_body}

JSON 배열로만 답변 (다른 텍스트 없이):
[
  {"assignee": "담당자 이름 (없으면 null)", "content": "액션아이템 내용", "due_date": "YYYY-MM-DD (없으면 null)"}
]
```

**함수** (`agents/after.py`):
```python
def _extract_action_items(event_id, user_id, internal_body) -> list[dict]:
    # LLM 호출 → JSON 파싱 → action_items 테이블 저장
    # 실패 시 빈 리스트 반환 (After Agent 전체를 중단시키지 않음)
```

---

### Step 3 — 외부용 회의록 Draft 발송 (핵심)

#### 3-1. 참석자 이메일 해석

이메일 조회 우선순위:

```
1. event_id 있음 → Calendar API get_event_attendees()   (가장 신뢰성 높음)
2. attendees 문자열에 이메일 포함 → 정규식 추출
3. Drive People/{이름}.md 파일에서 이메일 필드 파싱
4. 모두 실패 → 내부 도메인 제외한 이름만 표시, 사용자에게 알림
```

내부 도메인(`parametacorp.com`, `iconloop.com`) 참석자는 발송 대상에서 제외합니다.

#### 3-2. Slack Block Kit 메시지

```
📋 외부용 회의록이 생성되었습니다.

미팅: {title} ({date_str})
발송 대상: {외부 참석자 이름 목록}

[발송하기]  [발송 안 함]
```

- `pending_drafts` 테이블에 `status='pending'`으로 저장 후 메시지 발송
- Block Action ID: `after_send_minutes`, `after_cancel_minutes`

#### 3-3. 버튼 핸들러 (`main.py`)

```python
@app.action("after_send_minutes")
def handle_send_minutes(ack, body, client):
    ack()
    draft_id = body["actions"][0]["value"]
    after.handle_send_draft(client, draft_id)

@app.action("after_cancel_minutes")
def handle_cancel_minutes(ack, body, client):
    ack()
    draft_id = body["actions"][0]["value"]
    after.handle_cancel_draft(client, draft_id)
```

#### 3-4. 발송 처리 (`agents/after.py`)

```python
def handle_send_draft(slack_client, draft_id):
    draft = user_store.get_pending_draft(draft_id)
    recipients = json.loads(draft["recipients"])  # [{"name": ..., "email": ...}]

    # Gmail 발송
    subject = f"[회의록] {draft['title']}"
    body_html = markdown_to_html(draft["external_body"])
    creds = user_store.get_credentials(draft["user_id"])
    success = gmail.send_email(creds, [r["email"] for r in recipients], subject, body_html)

    # 상태 업데이트
    user_store.update_draft_status(draft_id, "sent" if success else "failed")

    # Slack 결과 알림
    slack_client.chat_postMessage(...)
```

---

### Step 4 — 액션아이템 담당자 알림 + 리마인더

#### 4-1. 회의 직후 담당자 DM

```python
def notify_action_items(slack_client, event_id, user_id):
    items = user_store.get_action_items(event_id)
    # 담당자 이름 → Slack 멤버 이름 매칭 → user_id 조회
    # 담당자별 그룹핑 → 각 담당자에게 DM

    # DM 메시지 예시:
    # 📋 {title} 미팅 후 액션아이템
    # - [ ] 내용 (기한: YYYY-MM-DD)
    # - [ ] 내용2
```

#### 4-2. APScheduler 리마인더 (매일 08:00)

```python
# main.py 스케줄러에 추가
scheduler.add_job(action_item_reminder, "cron", hour=8, minute=0, timezone="Asia/Seoul")

def action_item_reminder(slack_client):
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    # due_date = today or tomorrow인 open 항목 전체 조회
    # 담당자별 Slack DM 발송
```

#### 4-3. 완료 처리 버튼

담당자 DM 메시지에 `[완료 ✅]` 버튼 포함:
- 클릭 → `action_items.status = 'done'`
- 리마인더에서 `done` 항목은 제외

---

### Step 5 — Contacts 자동 갱신

회의록 생성 완료 후 외부 참석자 People 파일을 업데이트합니다.

```python
def update_contacts_after_meeting(creds, attendees_with_email: list[dict],
                                  title: str, date_str: str):
    for person in attendees_with_email:
        if _is_internal(person["email"]):
            continue
        content = drive.get_person_info(creds, person["name"])
        if content:
            # last_met 날짜 업데이트
            # "## 미팅 이력" 섹션에 한 줄 추가: "- YYYY-MM-DD {title}"
            drive.save_person_info(creds, person["name"], updated_content)
```

---

### Step 6 — 후속 일정 자동 제안 (선택)

internal_body에서 "다음 미팅", "후속 미팅", "follow-up" 등의 패턴을 감지하여
일정 생성 제안 메시지를 발송합니다.

```python
_FOLLOWUP_PATTERNS = ["다음 미팅", "후속 미팅", "다시 만나", "follow-up", "후속 일정"]

def _suggest_followup(slack_client, user_id, internal_body, title):
    if any(p in internal_body for p in _FOLLOWUP_PATTERNS):
        slack_client.chat_postMessage(
            channel=user_id,
            text=f"📅 회의록에서 후속 미팅 언급이 감지되었습니다.",
            blocks=[..., Button("후속 일정 잡기", action_id="suggest_followup_meeting")]
        )
```

---

## 6. 에러 처리

| 오류 | 처리 |
|------|------|
| 참석자 이메일 조회 실패 | 이름 목록만 표시, Slack에 "이메일 확인 필요" 안내 |
| LLM 액션아이템 추출 실패 | 빈 리스트 처리, 나머지 흐름 계속 |
| Gmail 발송 실패 | Slack에 에러 알림, `pending_drafts.status='failed'`로 저장. **재시도 가능**: "발송하기" 버튼을 다시 누르면 `status='pending'` 또는 `'failed'` 상태일 때 재시도 허용 (`'sent'`/`'cancelled'`이면 차단) |
| Drive Contacts 갱신 실패 | 경고 로그, 회의록 흐름에 영향 없음 |
| Slack 멤버 이름 매칭 실패 | 해당 담당자 알림 건너뜀, 주최자에게 "알림 실패" 보고 |
| event_id 없음 (캘린더 미연동) | Calendar API 조회 생략, Drive/문자열 기반 이메일 조회로 대체 |

---

## 7. 모듈 구조

```
agents/
└── after.py                    # ✅ 구현 완료
    ├── trigger_after_meeting()         # During Agent 호출 진입점 (백그라운드 스레드)
    ├── _resolve_attendee_emails()      # Calendar API → attendees_raw 순서로 외부 참석자 해석
    ├── _extract_and_save_action_items()  # LLM 추출 → action_items 테이블 저장
    ├── _send_draft_to_slack()          # pending_drafts 저장 + Block Kit 버튼 메시지 발송
    ├── handle_send_draft()             # "발송하기" 핸들러: pending/failed 상태 모두 재시도 허용
    ├── handle_cancel_draft()           # "발송 안 함" 핸들러
    ├── handle_complete_action_item()   # "완료 ✅" 버튼 핸들러
    ├── _notify_action_items()          # 담당자별 Slack DM 발송
    ├── action_item_reminder()          # 매일 08:00 KST 리마인더 (main.py에서 호출)
    ├── _update_contacts()              # People/{이름}.md last_met + 미팅 이력 갱신
    └── _suggest_followup()             # 패턴 감지 후 후속 일정 제안 버튼 발송

tools/
├── gmail.py                    # ✅ send_email(), markdown_to_html() 추가
│                               #    SCOPES에 gmail.send 추가 (informational)
└── calendar.py                 # ✅ get_event_attendees() 추가, SCOPES에 gmail.send 추가

store/
└── user_store.py               # ✅ action_items, pending_drafts 테이블 + CRUD 7개
                                #    SCOPES에 gmail.send 추가 (토큰 복원 시 사용)

server/
└── oauth.py                    # ✅ SCOPES에 gmail.send 추가 (Google 동의 화면 요청용, 핵심)

prompts/
└── briefing.py                 # ✅ extract_action_items_prompt() 추가

main.py                         # ✅ 버튼 핸들러 3개 + 리마인더 스케줄러 추가
```

---

## 8. 구현 순서 및 의존성

```
Step 1 (인프라)                    ← 시작점, 모든 Step의 전제
  ├── Step 3 (Draft 발송)          ← 사용자 체감 가치 최대, 우선 구현 권장
  ├── Step 2 (액션아이템 추출)
  │     └── Step 4 (리마인더)      ← Step 2 완료 후 구현
  ├── Step 5 (Contacts 갱신)       ← 독립적, 언제든 구현 가능
  └── Step 6 (후속 일정 제안)      ← 독립적, 마지막 구현 권장
```

**권장 구현 순서**: Step 1 → Step 3 → Step 2 → Step 4 → Step 5 → Step 6

---

## 9. 환경변수 (추가 필요 없음)

After Agent는 기존 환경변수를 그대로 사용합니다.
Gmail 발송 스코프는 OAuth 스코프 추가로 처리되며 별도 API 키가 필요 없습니다.

> 향후 Trello 연동(Phase 4.5) 구현 시에는 `TRELLO_API_KEY`, `TRELLO_TOKEN`, `TRELLO_BOARD_ID` 추가 필요.

---

## 10. 구현 결과 및 잔여 이슈

| 항목 | 결과 |
|------|------|
| `markdown_to_html()` 유틸 | ✅ `tools/gmail.py`에 `re` 기반 구현 완료 (헤딩·목록·볼드·구분선 변환) |
| 이메일 템플릿 | ✅ `<html><body style="...">` 인라인 스타일 적용 |
| 액션아이템 "완료" 권한 | 현재 누구든 버튼 클릭 가능 — 향후 담당자 본인만 허용 검토 |
| Slack 멤버 이름 매칭 정확도 | 동명이인 미처리 (Before Agent와 동일 한계) |
| Gmail 재시도 | ✅ `status='failed'`인 draft도 "발송하기" 재클릭 시 재시도 허용 |
