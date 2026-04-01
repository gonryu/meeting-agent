# During 에이전트 설계 문서

> 최종 갱신: 2026-03-26 | `/미팅종료` 즉시 회의록 생성으로 변경 (트랜스크립트 1회 확인 후 결과 무관 즉시 생성)

---

## 1. 개요

During 에이전트는 미팅 진행 중 회의록을 자동 수집 또는 수동으로 작성하고, 종료 후 LLM으로 구조화된 회의록을 생성합니다.

- **파일 경로**: `agents/during.py`
- **LLM**: Gemini `gemini-2.0-flash` (우선) → Claude `claude-haiku-4-5` (폴백)
- **입력 방식**: Google Meet 트랜스크립트 자동 수집 (방식 A) + 수동 Slack 노트 (방식 B)
  - 두 방식은 독립적으로 동작하며, 동일 미팅이면 **결합하여** 회의록 생성
- **필요 권한**: `documents.readonly` (Google Meet 트랜스크립트 읽기)
- **회의록 종류**: 내부용 + 외부용 2종 생성
- **사용자 관리**: 다중 사용자, 사용자별 독립 Drive/Minutes 폴더

---

## 2. 설계 원칙

| 원칙 | 내용 |
|------|------|
| **트랜스크립트 우선** | 수동 세션 여부와 무관하게 항상 폴링 |
| **결합 생성** | 트랜스크립트 + 수동 노트 모두 있으면 합쳐서 회의록 생성 |
| **내부용/외부용 분리** | 내부용: 전략적 맥락 포함 / 외부용: 합의 내용만, 상대방 공유 가능 |
| **즉시 생성** | `/미팅종료` 시 트랜스크립트를 1회 확인하고, 있으면 노트와 결합, 없으면 노트만으로 즉시 회의록 생성 |
| **90분 fallback** | 폴러가 처리하는 미팅(수동 세션 없이 Calendar 이벤트만 있는 경우) 트랜스크립트 미수집 시 노트만으로 회의록 생성 |

---

## 3. 트리거

| 유형 | 발동 조건 | 동작 |
|------|-----------|------|
| **서버 시작** | 서버 프로세스 기동 시 | 즉시 1회 트랜스크립트 폴링 실행 (`next_run_time=now()`) |
| **자동 폴링** | APScheduler, 10분 주기 | 최근 종료 미팅의 트랜스크립트 탐색 |
| **수동 시작** | `/미팅시작 {제목}` | 수동 노트 세션 시작 |
| **노트 추가** | `/메모 {내용}` | 진행 중 세션에 노트 저장 |
| **수동 종료** | `/미팅종료` | 백그라운드 스레드로 트랜스크립트 1회 확인 → 있으면 노트 결합, 없으면 노트만으로 즉시 회의록 생성 |
| **목록 조회** | `/회의록` | Drive Minutes 파일 목록 조회 |

---

## 4. 상태 저장소

```python
# 진행 중인 수동 노트 세션
_active_sessions: dict[str, dict]  # user_id → session
# 예: { "U123": { "title": "카카오", "started_at": "2026-03-25 14:00",
#                 "notes": [{"time": "14:05", "text": "..."}], "event_id": "evt1" } }

# 폴러 대기 중인 완료 노트 (서버 재시작 복구용 · 레거시)
# /미팅종료는 더 이상 이 딕셔너리를 사용하지 않음
_completed_notes: dict[str, dict]  # event_id → data

# 트랜스크립트 처리 완료 이벤트 (중복 방지)
_processed_events: dict[str, set]  # user_id → set(event_id)
```

### 세션 파일 영속성 (`.sessions/`)

메모리 상태를 로컬 JSON 파일로 백업하여 서버 재시작 후에도 복구 가능합니다.

```
.sessions/
├── active_{user_id}.json       ← 진행 중인 세션
├── completed_{event_id}.json   ← 레거시 (신규 생성 안 함)
└── processed_{user_id}.json    ← 처리 완료된 event_id 목록 (중복 방지)
```

| 이벤트 | 파일 동작 |
|--------|-----------|
| `/미팅시작` | `active_{user_id}.json` 생성 |
| `/메모` | `active_{user_id}.json` 업데이트 |
| `/미팅종료` (캘린더 연동 있음) | active 파일 삭제 → 백그라운드 `_generate_from_session_end` 실행 |
| `/미팅종료` (캘린더 연동 없음) | active 파일 삭제 → 즉시 회의록 생성 |
| 회의록 생성 완료 | `processed_{user_id}.json` 갱신 |
| 폴러 트랜스크립트 처리 완료 | `completed_{event_id}.json` 삭제 (레거시) + `processed_{user_id}.json` 갱신 |
| 90분 fallback 처리 완료 | `completed_{event_id}.json` 삭제 (레거시) + `processed_{user_id}.json` 갱신 |

**서버 시작 시 자동 복구**: 모듈 로드 시 `_load_sessions()` 호출 → `.sessions/` 파일을 메모리로 복원.
`stored_at` datetime은 ISO 문자열로 직렬화/역직렬화.
`processed_{user_id}.json`은 처리된 event_id 배열로 저장 — 서버 재시작 후에도 중복 처리 방지.

> `.sessions/` 폴더는 `.gitignore`에 추가됨 (토큰/세션 정보 git 제외)

---

## 5. 통합 흐름도

```
[수동: /미팅시작]             [자동: APScheduler 10분]
      │                              │
      ▼                              ▼
_active_sessions[user_id]   check_transcripts(slack)
  = { title, notes, event_id }       │
      │                              ▼ 사용자별 루프
[/메모 내용]                  recently_ended_meetings
  └─ notes.append()                  │
      │                              ▼ 각 미팅별
[/미팅종료]               event_id ∈ _processed_events? → skip
      │                              │
      ├─ event_id 있음               ▼
      │    └─ threading.Thread   find_meet_transcript()
      │       _generate_from_        │
      │       session_end()      없음 → log & skip
      │         │                    │
      │         ├─ find_transcript  있음 ↓
      │         │  (1회)         read_document(transcript)
      │         ├─ cal 이벤트 조회     │
      │         │  (참석자)       _completed_notes.pop(event_id, None)
      │         └─ _generate_and_    (레거시 노트 수집)
      │            post_minutes(       │
      │            transcript or   _generate_and_post_minutes(
      │            notes)           transcript + notes
      │                            )
      └─ event_id 없음
           └─ 즉시 회의록 생성

[_flush_expired_notes]  ← 10분마다 함께 실행
  └─ _completed_notes stored_at > 90분 → fallback 생성 (레거시)
```

---

## 6. 방식 A: Google Meet 트랜스크립트 자동 수집

### 전제 조건
- Google Workspace 유료 계정 (무료 계정은 트랜스크립트 미지원)
- Google Meet에서 녹화 + 트랜스크립트 기능 활성화
- 미팅 종료 후 Drive `Meet Recordings/` 폴더에 자동 저장됨

### 트랜스크립트 탐색 경로

Google Meet 녹화 방식에 따라 두 가지 파일 구조를 모두 지원합니다.

**구형 Meet (영문 Transcript)**
```
Drive 루트
└── Meet Recordings/
    └── {회의명}/                          ← NFD/NFC 이중 탐색
        └── {회의명} - Transcript           (Google Docs)
```

**Gemini 회의록 (한국어, 루트 저장)**
```
Drive 루트
└── Meet Recordings/
    └── {회의명} - YYYY/MM/DD HH:MM KST - Gemini가 작성한 회의록  (Google Docs)
```

> Gemini 회의록은 서브폴더 없이 `Meet Recordings/` 루트에 직접 저장되며,
> 파일명에 `ended_after` 필터를 적용하지 않습니다.
> (파일이 미팅 시작 시각에 생성되므로 종료 시각 기준 필터 시 탐색 불가)

### 폴링 실행 흐름
```
check_transcripts(slack_client)         ← APScheduler 10분 주기 / /미팅종료 즉시 호출
  │                                        (즉시 호출 시 min_minutes_ago=0)
  ├─ [사용자별] _check_transcripts_for_user(slack_client, user_id,
  │                                         min_minutes_ago=10)  ← 기본값
  │    │
  │    ├─ cal.get_recently_ended_meetings(creds, min=min_minutes_ago, max=90분)
  │    │
  │    ├─ [각 미팅] event_id ∈ _processed_events? → skip
  │    │
  │    ├─ drive.find_meet_transcript(creds, title, ended_after)
  │    │    ├─ 없음 → log & skip
  │    │    └─ 있음 ↓
  │    │
  │    ├─ docs.read_document(creds, doc_id)
  │    │
  │    ├─ _completed_notes.pop(event_id) → 수동 노트 수집 (있으면)
  │    │   또는 _active_sessions에서 event_id 매칭 노트 수집
  │    │
  │    └─ _generate_and_post_minutes(transcript + notes)
  │
  └─ _flush_expired_notes(slack_client)
```

---

## 7. 방식 B: 수동 Slack 노트

### 세션 구조
```python
_active_sessions[user_id] = {
    "title": str,           # 미팅 제목
    "started_at": str,      # "YYYY-MM-DD HH:MM"
    "notes": [              # 노트 목록
        {"time": "HH:MM", "text": str}
    ],
    "event_id": str | None, # 캘린더 이벤트 자동 매칭 결과
}
```

### /미팅종료 처리 흐름
```
session = _active_sessions.pop(user_id)
      │
      ├─ event_id 있음
      │    └─ threading.Thread(
      │           target=_generate_from_session_end,
      │           kwargs={user_id, event_id, title, notes, ...},
      │           daemon=True,
      │       ).start()
      │         │
      │         ├─ drive.find_meet_transcript() → 1회 탐색
      │         │    있음 → transcript_text = docs.read_document()
      │         │    없음 → transcript_text = ""
      │         │
      │         ├─ cal.get_recently_ended_meetings() → 참석자·날짜 조회
      │         │
      │         └─ _generate_and_post_minutes(
      │                transcript_text,   ← 있으면 포함, 없으면 ""
      │                notes_text,        ← 수동 노트 항상 포함
      │            )
      │
      └─ event_id 없음 (캘린더 미연동)
           └─ 즉시 _generate_and_post_minutes(notes_text만)
```

> **동작 변경**: 기존에는 트랜스크립트를 찾지 못하면 `_completed_notes`에 저장하고 폴러에 위임(최대 90분 대기)했습니다.
> 현재는 트랜스크립트 유무에 관계없이 `/미팅종료` 즉시 회의록을 생성합니다.

---

## 8. 회의록 생성 (`_generate_and_post_minutes`)

### LLM 2회 호출
```python
# 1단계: 내부용 — 전체 내용, 전략적 맥락 포함
internal_body = _generate(
    minutes_internal_prompt(title, date, attendees, transcript_text, notes_text)
)

# 2단계: 외부용 — 내부용에서 공유 가능한 내용만 추출
external_body = _generate(
    minutes_external_prompt(title, date, attendees, internal_body)
)
```

### Drive 저장
| 파일명 | 내용 |
|--------|------|
| `{YYYY-MM-DD}_{제목}_내부용.md` | 전체 내용 + 원본 트랜스크립트/노트 첨부 |
| `{YYYY-MM-DD}_{제목}_외부용.md` | 합의 내용만, 원본 미첨부 |

### Slack 발송
- 회의록 전체 내용 대신 **Drive 링크**를 DM으로 발송
- 내부용·외부용 링크를 단일 메시지로 전송
- `/회의록` 목록에도 파일별 Drive 열기 링크 포함

```
📋 회의록이 생성되었습니다: {title}  |  소스: {source_label}
📄 내부용: https://drive.google.com/file/d/{internal_file_id}/view
📤 외부용 (상대방 공유 가능): https://drive.google.com/file/d/{external_file_id}/view
```

> Drive 저장 실패 시 해당 링크 대신 "Drive 저장 실패" 문구 표시

### LLM 실패 처리
```python
try:
    internal_body = _generate(internal_prompt)
except Exception as e:
    # 실패해도 원본 노트/트랜스크립트는 반드시 저장
    internal_body = f"## 회의 요약\n(생성 실패: {e})\n"
```

---

## 9. 회의록 포맷

### 내부용 (`_내부용.md`)
```markdown
# {회의명} (내부용)

## 기본 정보
- 날짜: YYYY-MM-DD
- 시간: HH:MM ~ HH:MM
- 참석자: (트랜스크립트 방식만 포함)
- 입력 소스: 트랜스크립트 / 수동 노트 / 트랜스크립트 + 수동 노트

## 회의 요약
## 주요 결정 사항
## 액션 아이템
## 주요 논의 내용
## 내부 메모

---
## 원본 트랜스크립트  (있는 경우)
## 원본 수동 노트     (있는 경우)
```

### 외부용 (`_외부용.md`)
```markdown
# {회의명} (외부용)

## 기본 정보

## 회의 개요
## 주요 합의 사항
## 공동 액션 아이템
## 다음 단계
```

---

## 10. Drive 관련 (`tools/drive.py`)

### `find_meet_transcript`
```python
def find_meet_transcript(creds, meeting_title, ended_after=None) -> dict | None:
    # 1) "Meet Recordings" 루트 폴더 탐색
    # 2) "{meeting_title}" 서브폴더 탐색 (NFD/NFC 이중 시도)
    #    → 서브폴더 있음: 'Transcript' 또는 'Gemini가 작성한 회의록' 포함 파일
    #                     + ended_after 필터 적용 (UTC RFC 3339 포맷)
    #    → 서브폴더 없음: 루트에서 name contains '{meeting_title}' 검색
    #                     ended_after 필터 미적용 (Gemini 회의록은 미팅 시작 시 생성)
    # 3) modifiedTime 내림차순 → 가장 최신 파일 반환
    #
    # ⚠️ 날짜 포맷 주의: ended_after는 반드시 UTC 변환 후 RFC 3339 포맷 사용
    #   올바른 예: ended_after.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    #   잘못된 예: ended_after.isoformat() + "Z"  → "+09:00Z" 형식 → Drive API 400 오류
```

### `save_minutes`
```python
def save_minutes(creds, minutes_folder_id, filename, content) -> str:
    # _write_file 래퍼: 동일 파일명 있으면 업데이트, 없으면 신규 생성
    # Returns: file_id
```

### `list_minutes`
```python
def list_minutes(creds, minutes_folder_id) -> list[dict]:
    # Minutes 폴더 파일 목록 최신순 반환
    # Returns: [{"id", "name", "modifiedTime"}]
```

---

## 11. Docs 관련 (`tools/docs.py`)

```python
def read_document(creds, doc_id) -> str:
    # Google Docs API로 문서 전문 텍스트 추출
    # paragraph.elements[].textRun.content 이어붙임

def _extract_text(document) -> str:
    # Docs 구조체 → 순수 텍스트
```

---

## 12. Calendar 관련 (`tools/calendar.py`)

### `get_recently_ended_meetings`
```python
def get_recently_ended_meetings(creds, min_minutes_ago=10, max_minutes_ago=90) -> list[dict]:
    # 종료 후 min~max분 사이 미팅 반환 (트랜스크립트 폴링용)
    # parse_event() 결과 + "end_time" 필드 추가
```

---

## 13. 사용자별 격리

### `_get_creds_and_config(user_id)`
```python
creds = user_store.get_credentials(user_id)
user  = user_store.get_user(user_id)
return creds, user["minutes_folder_id"]
```

### DB 스키마 (users 테이블)
| 컬럼 | 설명 |
|------|------|
| `minutes_folder_id` | 사용자별 Minutes Drive 폴더 ID |

> 기존 DB 마이그레이션: `init_db()` 호출 시 컬럼 없으면 `ALTER TABLE`로 자동 추가

---

## 14. 에러 처리

| 오류 | 처리 |
|------|------|
| Gemini 429 (할당량 초과) | Claude 폴백 |
| Google Docs API 오류 | 예외 전파 → Slack 오류 메시지 발송 |
| Drive 저장 실패 | 경고 로그 + Slack 링크 메시지에 "Drive 저장 실패" 표시 |
| 트랜스크립트 없음 | 로그만 기록, 사용자 알림 없음 |
| 수동 세션 없이 `/메모` | 경고 메시지 발송 |
| 수동 세션 중복 시작 | 기존 세션 유지 + 경고 메시지 발송 |
| LLM 내부용 생성 실패 | 오류 메시지 포함 fallback 저장 |
| LLM 외부용 생성 실패 | 오류 메시지 포함 fallback 저장 |

---

## 15. 미구현 / 개선 예정

| 항목 | 현황 |
|------|------|
| 서버 재시작 시 수동 세션 복구 | ✅ `.sessions/` 파일로 자동 복구 구현 완료 |
| `/미팅종료` 즉시 회의록 생성 | ✅ 트랜스크립트 1회 확인 후 결과 무관 즉시 생성 (`_generate_from_session_end`) |
| Drive API `modifiedTime` 날짜 포맷 버그 | ✅ UTC 변환 후 RFC 3339 포맷으로 수정 완료 (`tools/drive.py`) |
| 처리 완료 이벤트 영구 기록 | ✅ `processed_{user_id}.json`으로 영속화. 서버 재시작 후에도 중복 처리 방지 |
| 트랜스크립트 없을 때 사용자 알림 | 현재 로그만 기록 |
| 회의록 내 특정 참석자 액션아이템 추출 | ✅ After Agent 구현 완료 (`agents/after.py`) |
| 참석자 이메일 발송 (외부용) | ✅ After Agent 구현 완료 (Gmail API) |
