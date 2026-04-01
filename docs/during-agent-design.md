# During 에이전트 설계 문서

> 최종 갱신: 2026-04-01 | 회의록 검토/편집 단계 추가, Deepgram STT, `/메모` 자동 세션 시작, `/미팅시작` 캘린더 매칭 우선순위 개선, `_processed_events` 버그 수정

---

## 1. 개요

During 에이전트는 미팅 진행 중 회의록을 자동 수집 또는 수동으로 작성하고, 종료 후 LLM으로 구조화된 회의록을 생성합니다.

- **파일 경로**: `agents/during.py`
- **LLM**: Gemini `gemini-2.0-flash` (우선) → Claude `claude-haiku-4-5` (폴백)
- **STT**: Deepgram REST API (`nova-2` 모델, `tools/stt.py`)
- **입력 방식**: Google Meet 트랜스크립트 자동 수집 (방식 A) + 수동 Slack 노트 (방식 B) + 음성 파일 STT (방식 C)
  - 방식 A·B는 독립적으로 동작하며, 동일 미팅이면 **결합하여** 회의록 생성
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
| **검토 후 저장** | 회의록 생성 후 Drive 저장 전 검토 단계를 거침 (초안 확인/편집/취소) |
| **90분 fallback** | 폴러가 처리하는 미팅(수동 세션 없이 Calendar 이벤트만 있는 경우) 트랜스크립트 미수집 시 노트만으로 회의록 생성 |

---

## 3. 트리거

| 유형 | 발동 조건 | 동작 |
|------|-----------|------|
| **서버 시작** | 서버 프로세스 기동 시 | 즉시 1회 트랜스크립트 폴링 실행 (`next_run_time=now()`) |
| **자동 폴링** | APScheduler, 10분 주기 | 최근 종료 미팅의 트랜스크립트 탐색 |
| **수동 시작** | `/미팅시작 {제목}` | 수동 노트 세션 시작 |
| **노트 추가** | `/메모 {내용}` | 진행 중 세션에 노트 저장 (세션 없으면 자동 시작) |
| **음성 업로드** | DM에 오디오 파일 첨부 | Deepgram STT → 노트로 등록 (세션 없으면 자동 시작) |
| **수동 종료** | `/미팅종료` | 백그라운드 스레드로 트랜스크립트 1회 확인 → 있으면 노트 결합, 없으면 노트만으로 즉시 회의록 생성 → 초안 검토 |
| **목록 조회** | `/회의록` | Drive Minutes 파일 목록 조회 |

---

## 4. 상태 저장소

```python
# 진행 중인 수동 노트 세션
_active_sessions: dict[str, dict]
# 예: { "U123": { "title": "KISA 미팅", "started_at": "2026-04-01 14:00",
#                 "notes": [...], "event_id": "evt1",
#                 "event_summary": "KISA DID 미팅", "event_time_str": "오후 2:00 ~ 오후 3:00" } }

# 폴러 대기 중인 완료 노트 (레거시)
_completed_notes: dict[str, dict]  # event_id → data

# 트랜스크립트 처리 완료 이벤트 (폴러 중복 방지)
# /미팅종료 명시 호출 시에는 discard() 후 재생성 허용
_processed_events: dict[str, set]  # user_id → set(event_id)

# 회의록 검토 대기 중인 초안
_pending_minutes: dict[str, dict]
# 예: { "U123": { "title", "date_str", "time_range", "attendees",
#                 "internal_body", "external_body", "draft_doc_id",
#                 "draft_ts", "minutes_folder_id", "creds", ... } }
```

### 세션 파일 영속성 (`.sessions/`)

```
.sessions/
├── active_{user_id}.json       ← 진행 중인 세션
├── completed_{event_id}.json   ← 레거시 (신규 생성 안 함)
└── processed_{user_id}.json    ← 처리 완료된 event_id 목록 (폴러 중복 방지용)
```

> **주의**: `_processed_events`는 **폴러(스케줄러) 중복 방지 전용**입니다.
> `/미팅종료` 명시 호출 시에는 `discard(event_id)` 후 재생성을 허용하므로
> 동일 캘린더 이벤트에 대해 여러 번 `/미팅종료`를 해도 항상 회의록이 생성됩니다.

---

## 5. 통합 흐름도

```
[수동: /미팅시작]             [자동: APScheduler 10분]
      │                              │
      ▼                              ▼
_active_sessions[user_id]   check_transcripts(slack)
      │                              │
[/메모 내용]                         ▼
  └─ notes.append()         recently_ended_meetings
  └─ 세션 없으면 자동 시작              │
      │                     event_id ∈ _processed_events? → skip
[음성 파일 업로드]                    │
  └─ Deepgram STT           find_meet_transcript()
  └─ add_note() 호출                 │
  └─ 세션 없으면 자동 시작        없음 → skip
      │                             │
[/미팅종료]                    있음 ↓
      │                     read_document(transcript)
      ├─ event_id 있음               │
      │    └─ _processed_events  _generate_and_post_minutes(
      │       .discard(event_id)   transcript + notes
      │       threading.Thread()  )
      │       _generate_from_         │
      │       session_end()           ▼
      │         │              [초안 검토 단계]
      │         ├─ find_transcript   (동일 흐름)
      │         ├─ cal 이벤트 조회
      │         └─ _generate_and_
      │            post_minutes()
      │                │
      └─ event_id 없음  │
           └─ 즉시 회의록 생성
                        │
                        ▼
              _pending_minutes[user_id] 저장
              Google Docs 초안 생성 (직접 편집용)
              Slack 초안 메시지 발송 (버튼 4개)
```

---

## 6. 방식 A: Google Meet 트랜스크립트 자동 수집

### 전제 조건
- Google Workspace 유료 계정
- Google Meet 트랜스크립션 자동 활성화: 미팅 생성 시 Google Meet API v2 `spaces.patch`로 `transcriptionConfig.state: ON` 설정 (`tools/calendar.py` → `enable_meet_transcription()`)

### 트랜스크립트 탐색 경로

**구형 Meet (영문 Transcript)**
```
Drive 루트
└── Meet Recordings/
    └── {회의명}/
        └── {회의명} - Transcript  (Google Docs)
```

**Gemini 회의록 (한국어, 루트 저장)**
```
Drive 루트
└── Meet Recordings/
    └── {회의명} - YYYY/MM/DD HH:MM KST - Gemini가 작성한 회의록  (Google Docs)
```

---

## 7. 방식 B: 수동 Slack 노트

### `/미팅시작` 캘린더 매칭 우선순위

```
1순위: 현재 진행 중 (start_dt <= now <= end_dt)  → 즉시 확정
2순위: 30분 내 시작 예정 (now < start_dt <= now + 30분)
3순위: 제목 일치 (title_to_use.lower() in event_summary.lower())
```

매칭된 이벤트의 `event_id`, `event_summary`, `event_time_str`을 세션에 저장합니다.

### `/메모` 자동 세션 시작

세션이 없는 상태에서 `/메모`를 호출하면 `"메모 세션"` 타이틀로 자동 시작합니다.

```python
if user_id not in _active_sessions:
    start_session(slack_client, user_id, session_title)  # "메모 세션"
```

---

## 8. 방식 C: 음성 파일 STT

### 지원 형식
`audio/mpeg`, `audio/mp3`, `audio/mp4`, `audio/m4a`, `audio/x-m4a`, `audio/wav`, `audio/ogg`, `audio/webm`, `audio/aac`, `video/mp4`, `video/quicktime`, `video/webm` (500MB 이하)

### STT 엔진: Deepgram REST API

```python
# tools/stt.py
MODEL    = "nova-2"
LANGUAGE = "ko"
OPTIONS  = "smart_format=true&punctuate=true"
```

- API Key: 환경변수 `DEEPGRAM_API_KEY`
- 사내 방화벽 대응: `verify=False` (InsecureRequestWarning 경고 비활성화)
- Slack 파일 다운로드 → 메모리에서 직접 Deepgram API로 전송 (로컬 저장 불필요)

### 처리 흐름

```
DM 오디오 파일 업로드
      │
      ▼
_handle_audio_upload() (백그라운드 스레드)
      │
      ├─ "🎙️ {filename} 음성 변환 중..." 발송
      ├─ Slack 파일 다운로드 (Bearer 토큰 인증)
      ├─ Deepgram API 호출 → transcript 텍스트
      └─ add_note(note_text=transcript, session_title="음성 메모 세션")
           └─ 세션 없으면 자동 시작 후 노트 등록
```

---

## 9. 회의록 생성 흐름 (`_generate_and_post_minutes`)

### LLM 2회 호출 후 초안 저장

```python
# 1단계: 내부용 생성
internal_body = _generate(minutes_internal_prompt(..., transcript[:40000], notes_text))

# 2단계: 외부용 생성
external_body = _generate(minutes_external_prompt(..., internal_body))

# 3단계: 초안 저장 (Drive 저장/발송 보류)
_pending_minutes[user_id] = { ...모든 메타데이터... }
_post_minutes_draft(slack_client, user_id=user_id)
```

> 트랜스크립트 제한: `40,000자` (한국어 약 2시간 분량)
> 유추·추론 금지: 트랜스크립트와 메모 내용에만 기반하여 작성

---

## 10. 회의록 초안 검토 단계

### 초안 메시지 (Block Kit 버튼)

```
📋 회의록 초안 검토: {title}
─────────────────────────────
내부용 회의록 미리보기 (최대 2500자)
...
─────────────────────────────
[ ✅ 저장 및 완료 ]  [ 📝 직접 편집 ]  [ ✏️ 수정 요청 ]  [ ❌ 취소 ]

📝 직접 편집 후 저장 및 완료를 누르면 편집된 내용으로 최종 저장됩니다.
```

### Google Docs 직접 편집

초안 메시지 발송 시 `drive.create_draft_doc()` 으로 Google Docs 파일을 임시 생성합니다.
`[📝 직접 편집]` 버튼 클릭 시 `https://docs.google.com/document/d/{doc_id}/edit` 로 이동.

`[✅ 저장 및 완료]` 클릭 시:
1. `docs.read_document(creds, draft_doc_id)` 로 현재 Doc 내용 읽기
2. 원본과 다르면 편집 내용 반영 + 외부용 재생성
3. Drive에 내부용·외부용 `.md` 저장
4. Slack 회의록 링크 발송
5. 편집용 초안 Doc 삭제 (`drive.delete_file()`)
6. After Agent 백그라운드 실행

### LLM 재생성을 통한 수정 요청

`[✏️ 수정 요청]` 클릭 → 초안 스레드에 안내 메시지 발송 → 사용자 스레드 답글 감지 → `handle_minutes_edit_reply()` 호출:

```python
edit_prompt = f"[기존 회의록]\n{internal_body}\n\n[수정 요청]\n{edit_text}\n\n수정된 전체 회의록을 반환해줘."
new_internal = _generate(edit_prompt)
new_external = _generate(minutes_external_prompt(..., new_internal))
# → 새 초안 메시지 재발송
```

### 버튼 액션 핸들러 (`main.py`)

| action_id | 동작 |
|-----------|------|
| `minutes_confirm` | `finalize_minutes()` 백그라운드 실행 |
| `minutes_open_doc` | URL 버튼 — ack() 만 처리, 브라우저에서 열림 |
| `minutes_edit_request` | `request_minutes_edit()` — 스레드 안내 발송 |
| `minutes_cancel` | `cancel_minutes()` — 초안 삭제 |

---

## 11. Drive 관련 (`tools/drive.py`)

| 함수 | 설명 |
|------|------|
| `find_meet_transcript()` | Meet Recordings 폴더에서 트랜스크립트 탐색 |
| `save_minutes()` | Minutes 폴더에 `.md` 파일 저장 |
| `create_draft_doc()` | 편집 가능한 Google Docs 임시 초안 생성 |
| `delete_file()` | Drive 파일 휴지통 이동 (초안 정리용) |
| `list_minutes()` | Minutes 폴더 파일 목록 최신순 반환 |

---

## 12. 에러 처리

| 오류 | 처리 |
|------|------|
| Gemini 429 (할당량 초과) | Claude 폴백 |
| Drive 저장 실패 | 경고 로그 + "Drive 저장 실패" 표시 |
| 트랜스크립트 없음 | 노트만으로 즉시 회의록 생성 |
| STT 실패 (Deepgram) | 오류 메시지 Slack 발송, 세션 미시작 |
| Google Docs 초안 읽기 실패 | 원본 `internal_body` 사용, 로그 기록 |
| `_processed_events` 중복 | `/미팅종료`: `discard()` 후 재생성 / 폴러: `continue` skip |
| 세션 없이 `/메모` | 자동 세션 시작 후 노트 등록 |
| LLM 생성 실패 | fallback 텍스트로 저장 |
