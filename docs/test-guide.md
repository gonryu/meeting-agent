# 테스트 가이드

> 최종 갱신: 2026-04-02
> 총 테스트 수: 142개 (전체 통과) — 업체명 자동추출 테스트 제거, 회의록 초안 검토 흐름 반영, Claude Sonnet mock 적용

> ⚠️ 주요 미추가 테스트 항목:
> - `test_stt.py`: Deepgram API 호출 (mocking), is_audio() MIME 판별
> - `test_during.py` 추가: `finalize_minutes()`, `cancel_minutes()`, `handle_minutes_edit_reply()`
> - `test_before.py` 추가: `update_meeting_from_text()`, `_register_briefing_draft()`

---

## 1. 테스트 실행

```bash
# 전체 실행
pytest tests/

# 특정 파일만
pytest tests/test_during.py

# 특정 클래스만
pytest tests/test_during.py::TestEndSession

# 특정 케이스만
pytest tests/test_during.py::TestEndSession::test_deferred_to_poller_when_event_id_known

# 결과 상세 출력
pytest tests/ -v

# 실패 시 즉시 중단
pytest tests/ -x
```

---

## 2. 테스트 구성 개요

| 파일 | 대상 모듈 | 테스트 클래스 수 | 테스트 수 |
|------|-----------|----------------|---------|
| `test_calendar.py` | `tools/calendar.py` | 2 | 13 |
| `test_slack_tools.py` | `tools/slack_tools.py` | 2 | 17 |
| `test_gmail.py` | `tools/gmail.py` (`_decode_body`) | 1 | 8 |
| `test_gmail_parse.py` | `tools/gmail.py` (`parse_address_header`) | 1 | 8 |
| `test_docs.py` | `tools/docs.py` | 2 | 8 |
| `test_drive_minutes.py` | `tools/drive.py` (Minutes/Transcript) | 3 | 8 |
| `test_oauth.py` | `server/oauth.py` | 2 | 9 |
| `test_user_store.py` | `store/user_store.py` | 5 | 12 |
| `test_before.py` | `agents/before.py` | 3 | 13 |
| `test_during.py` | `agents/during.py` | 8 | 45 |
| **합계** | | **29** | **142** |

---

## 3. 공통 테스트 패턴

### 3.1 환경변수 및 외부 클라이언트 차단

실제 Google API, Gemini, Claude를 호출하지 않도록 `import` 전에 패치합니다.

```python
import os
os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")

with patch("google.genai.Client"), \
     patch("anthropic.Anthropic"), \
     patch("tools.calendar._service"), \
     patch("tools.drive._service"):
    import agents.during as during
```

### 3.2 Slack 클라이언트 Mock

```python
def _slack():
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "111.222"}
    return client
```

### 3.3 user_store Mock

```python
def _mock_store():
    mock = MagicMock()
    mock.get_credentials.return_value = _MOCK_CREDS
    mock.get_user.return_value = _MOCK_USER
    mock.all_users.return_value = [_MOCK_USER]
    return patch("agents.during.user_store", mock)
```

### 3.4 세션 디렉토리 격리 (autouse 픽스처)

`test_during.py`는 `autouse` 픽스처로 실제 `.sessions/` 폴더를 오염시키지 않습니다.

```python
@pytest.fixture(autouse=True)
def isolated_sessions_dir(tmp_path):
    sessions_dir = tmp_path / ".sessions"
    with patch.object(during, "_SESSIONS_DIR", sessions_dir):
        yield sessions_dir
```

### 3.5 threading.Thread 패치 패턴

`end_session()`이 `event_id` 있는 세션을 종료할 때 백그라운드 스레드를 생성합니다.
테스트에서 실제 API 호출이 일어나지 않도록 반드시 `threading.Thread`를 패치해야 합니다.

```python
with patch("agents.during.threading.Thread") as mock_thread:
    end_session(slack, user_id)

# 스레드가 올바른 인자로 생성되었는지 검증
mock_thread.assert_called_once()
call_kwargs = mock_thread.call_args[1]["kwargs"]
assert call_kwargs["event_id"] == "evt123"
assert call_kwargs["title"] == "카카오 미팅"
```

> **적용 대상 테스트**: `event_id`가 있는 세션 종료 시나리오 — `test_session_removed_after_end_with_event`,
> `test_deferred_sends_wait_message`, `test_background_thread_when_event_id_known` 등

### 3.5 DB 격리 (autouse 픽스처)

`test_user_store.py`는 테스트마다 임시 SQLite DB를 사용합니다.

```python
@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_users.db")
    monkeypatch.setattr(user_store, "_DB_PATH", db_path)
    user_store.init_db()
```

---

## 4. 파일별 테스트 상세

---

### 4.1 test_calendar.py — Calendar 이벤트 파싱 및 분류

**대상**: `tools/calendar.py`의 `parse_event()`, `classify_meeting()`

#### TestParseEvent (6개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_full_event` | 전체 필드(id, summary, start_time, location, meet_link, description, attendees) 정상 파싱 |
| `test_self_attendee_excluded` | `self=True` 참석자는 attendees 목록에서 제외 |
| `test_all_day_event` | 종일 이벤트 (`start.date`만 있을 때) → `start_time`에 날짜 문자열 반환 |
| `test_no_summary` | 제목 없는 이벤트 → 기본값 `"(제목 없음)"` |
| `test_no_attendees` | 참석자 없는 이벤트 → 빈 리스트 |
| `test_missing_optional_fields` | location/meet_link/description 없을 때 빈 문자열 반환 |

#### TestClassifyMeeting (7개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_external_attendee_domain` | 외부 참석자 도메인 → `"external"` |
| `test_internal_attendee_only` | 내부 도메인 참석자만 → `"internal"` |
| `test_title_keyword_match` | 제목에 known_companies 업체명 포함 → `"external"` |
| `test_no_attendees_no_company` | 참석자 없고 업체명 없음 → `"internal"` |
| `test_case_insensitive_title_match` | 제목 매칭은 대소문자 무관 |
| `test_mixed_internal_external_attendees` | 내부+외부 혼합 → 외부 1명이라도 있으면 `"external"` |
| `test_self_true_excluded_from_domain_check` | `self=True` 참석자는 도메인 체크에서 제외 |

---

### 4.2 test_slack_tools.py — 시간 포맷 및 브리핑 메시지 빌더

**대상**: `tools/slack_tools.py`의 `format_time()`, `build_briefing_message()`

#### TestFormatTime (8개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_afternoon` | 오후 시간 → `"오후 H:MM"` 형식 |
| `test_morning` | 오전 시간 → `"오전 H:MM"` 형식 |
| `test_noon` | 정오(12:00) → `"오후 12:00"` |
| `test_midnight` | 자정(00:00) → `"오전 0:00"` |
| `test_all_day_event` | 날짜만 있으면 `"M/D 종일"` 반환 |
| `test_empty_string` | 빈 문자열 → 빈 문자열 반환 |
| `test_utc_z_notation` | UTC `"Z"` 표기도 처리 |
| `test_minute_zero_padding` | 분 두 자리 패딩 (`2:05` 형식) |

#### TestBuildBriefingMessage (9개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_returns_list_of_blocks` | Slack Block Kit 리스트 반환, 첫 블록 타입 `"section"` |
| `test_news_limited_to_3` | 뉴스 5개 전달해도 최대 3개만 표시 |
| `test_no_persons_section_hidden` | 담당자 없으면 담당자 섹션 미표시 |
| `test_persons_with_linkedin` | 담당자에 LinkedIn URL 포함 |
| `test_no_previous_context` | 이전 맥락 없으면 `"이전 미팅 기록 없음"` |
| `test_email_context_shown` | 이메일 맥락 있으면 snippet 표시 |
| `test_location_shown` | 미팅 장소 있으면 표시 |
| `test_no_service_connections` | 서비스 연결점 없으면 `"분석 정보 없음"` |
| `test_no_news_shows_placeholder` | 뉴스 없으면 `"최근 동향 정보 없음"` |
| `test_meeting_title_in_header` | 미팅 제목과 업체명이 헤더에 포함 |

---

### 4.3 test_gmail.py — 이메일 본문 디코딩

**대상**: `tools/gmail.py`의 `_decode_body()`

#### TestDecodeBody (8개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_direct_body_data` | `payload.body.data` 직접 base64 디코딩 |
| `test_multipart_text_plain` | multipart 중 `text/plain` 파트 선택 |
| `test_html_tags_stripped` | HTML 태그 제거 후 텍스트 반환 |
| `test_truncated_to_500_chars` | 500자 초과 텍스트는 500자로 잘림 |
| `test_exactly_500_chars` | 500자 이하는 그대로 반환 |
| `test_empty_payload` | 빈 payload → 빈 문자열 |
| `test_no_text_plain_in_parts` | `text/plain` 없는 multipart → 빈 문자열 |
| `test_multipart_first_text_plain_wins` | 여러 `text/plain` 중 첫 번째만 사용 |

---

### 4.4 test_gmail_parse.py — 이메일 주소 헤더 파싱

**대상**: `tools/gmail.py`의 `parse_address_header()`

#### TestParseAddressHeader (8개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_name_and_email` | `"이름 <email>"` 형식 파싱 |
| `test_email_only` | 이메일만 있는 경우 name은 빈 문자열 |
| `test_multiple_addresses` | 쉼표 구분 복수 주소 파싱 |
| `test_mixed_format` | 이름+이메일 혼합, 이메일만 혼합 |
| `test_empty_string` | 빈 문자열 → 빈 리스트 |
| `test_no_at_sign` | `@` 없는 문자열 → 무시 |
| `test_whitespace_trimmed` | 앞뒤 공백 제거 |
| `test_quoted_name` | 영문 이름도 정상 파싱 |

---

### 4.5 test_docs.py — Google Docs 텍스트 추출

**대상**: `tools/docs.py`의 `_extract_text()`, `read_document()`

#### TestExtractText (6개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_single_paragraph` | 단일 paragraph 텍스트 추출 |
| `test_multiple_paragraphs` | 여러 paragraph 이어붙임 |
| `test_empty_body` | 빈 body → 빈 문자열 |
| `test_no_body` | body 키 없음 → 빈 문자열 |
| `test_element_without_text_run` | `sectionBreak` 등 `textRun` 없는 element 무시 |
| `test_multiple_runs_in_paragraph` | 하나의 paragraph 내 여러 `textRun` 이어붙임 |

#### TestReadDocument (2개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_success` | 정상 문서 읽기 |
| `test_api_error_raises` | API 오류 시 예외 전파 |

---

### 4.6 test_drive_minutes.py — Drive 회의록 저장 및 트랜스크립트 탐색

**대상**: `tools/drive.py`의 `find_meet_transcript()`, `save_minutes()`, `list_minutes()`

#### TestFindMeetTranscript (4개)

> **참고**: 현재 테스트는 서브폴더 경로(`'Transcript'` 포함 파일)만 검증.
> 루트 경로 (Gemini 회의록) 및 `'Gemini가 작성한 회의록'` 파일명 매칭은 미테스트 (추가 필요).

| 테스트 | 검증 내용 |
|--------|---------|
| `test_transcript_found` | `Meet Recordings` → 회의 서브폴더 → Transcript 파일 3단계 탐색 성공 |
| `test_no_recordings_folder` | `Meet Recordings` 폴더 없으면 `None` 반환 |
| `test_no_transcript_file` | 서브폴더는 있지만 Transcript 없으면 `None` 반환 |
| `test_returns_most_recent_transcript` | 여러 Transcript 파일 중 `modifiedTime` 기준 최신 파일 반환 |

#### TestSaveMinutes (2개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_creates_new_file` | 신규 회의록 파일 생성 |
| `test_updates_existing_file` | 같은 파일명 이미 있으면 업데이트 (중복 방지) |

#### TestListMinutes (2개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_returns_sorted_files` | 회의록 목록 최신순 반환 |
| `test_empty_folder` | 폴더에 파일 없으면 빈 리스트 |

---

### 4.7 test_oauth.py — OAuth 인증 흐름

**대상**: `server/oauth.py`의 `build_auth_url()`, `/oauth/callback` 엔드포인트

#### TestBuildAuthUrl (3개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_state_contains_user_id` | 생성된 URL의 state가 `user_id|uuid` 형식 포함 |
| `test_unique_state_per_call` | 같은 user_id로 두 번 호출해도 state가 다름 (Slack retry 중복 방지) |
| `test_pending_flows_stored_by_state` | `_pending_flows`에 state 키로 flow 저장 |

#### TestOAuthCallback (6개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_missing_code_returns_400` | code 없으면 400 |
| `test_missing_state_returns_400` | state 없으면 400 |
| `test_unknown_state_returns_400` | `_pending_flows`에 없는 state → 400 + 재등록 안내 |
| `test_valid_state_completes_oauth` | 유효한 state → 토큰 저장 + Drive 셋업 트리거, 200 응답 |
| `test_state_removed_after_use` | 콜백 처리 후 `_pending_flows`에서 state 삭제 (재사용 방지) |
| `test_user_id_extracted_from_state` | `state = "U005|uuid"` → `slack_user_id="U005"`로 register 호출 |

---

### 4.8 test_user_store.py — 사용자 DB 관리

**대상**: `store/user_store.py`

#### TestInitDb (2개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_minutes_folder_id_column_exists` | `minutes_folder_id` 컬럼이 `users` 테이블에 존재 |
| `test_init_db_idempotent` | `init_db()` 중복 호출해도 오류 없음 |

#### TestRegisterAndGet (5개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_register_new_user` | 신규 사용자 등록 |
| `test_not_registered` | 미등록 user_id → `is_registered()` False |
| `test_register_overwrites_existing` | 재등록 시 토큰 갱신 (ON CONFLICT UPDATE) |
| `test_get_user_returns_dict` | `get_user()` → dict 반환 |
| `test_get_unregistered_raises` | 미등록 user_id → ValueError 발생 |

#### TestUpdateDriveConfig (2개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_update_with_minutes_folder` | `contacts_folder_id`, `knowledge_file_id`, `minutes_folder_id` 함께 업데이트 |
| `test_update_without_minutes_folder` | `minutes_folder_id` 없이 업데이트 시 기본값 `None` |

#### TestUpdateMinutesFolder (1개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_update_minutes_folder_id` | `minutes_folder_id` 단독 업데이트 |

#### TestAllUsers (2개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_returns_all_registered` | 전체 등록 사용자 반환 |
| `test_empty_db` | DB 비어있으면 빈 리스트 |

---

### 4.9 test_before.py — Before Agent (브리핑 및 미팅 생성)

**대상**: `agents/before.py`

#### TestExtractCompanyName (7개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_external_attendee_domain` | 외부 참석자 이메일 도메인에서 업체명 추출 (`user@kakao.com` → `"kakao"`) |
| `test_internal_domain_skipped` | 내부 도메인만 있으면 `None` |
| `test_known_company_in_title` | 제목에 known_companies 업체명 포함 → 반환 |
| `test_gemini_fallback_called` | 참석자/known_companies 없으면 LLM 호출 |
| `test_gemini_returns_null` | LLM이 `"null"` 반환 → `None` |
| `test_gemini_long_response_rejected` | LLM 응답이 30자 초과 → `None` (비정상 응답 방지) |
| `test_attendee_domain_priority_over_title` | 참석자 도메인이 known_companies 제목 매칭보다 우선 |

#### TestHandleAgendaReply (4개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_registered_thread_updates_calendar` | 등록된 thread_ts → Calendar 이벤트 설명 업데이트 |
| `test_registered_thread_deleted_after_update` | 업데이트 후 `_pending_agenda`에서 삭제 |
| `test_unregistered_thread_does_nothing` | 등록되지 않은 thread_ts → 아무 동작 없음 |
| `test_calendar_error_sends_error_message` | Calendar 업데이트 실패 시 에러 메시지 발송 |

#### TestFindEmail (5개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_found_in_slack_by_real_name` | Slack `real_name` 매칭으로 이메일 반환 |
| `test_found_in_slack_by_display_name` | Slack `display_name` 매칭으로 이메일 반환 |
| `test_slack_fails_fallback_to_drive` | Slack 실패 시 Drive Contacts에서 이메일 검색 |
| `test_not_found_returns_none` | Slack/Drive 모두 없으면 `None` |
| `test_drive_email_case_insensitive_key` | Drive Contacts에서 `"email:"` 소문자 키도 인식 |

#### TestRunBriefing (4개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_no_meetings_sends_empty_message` | 오늘 미팅 없으면 `"없습니다"` 메시지 |
| `test_returns_thread_ts_list` | 이벤트 수만큼 thread_ts 반환 |
| `test_external_meeting_sends_full_briefing` | 업체명 추출 성공 → blocks 포함 풀 브리핑 발송 |
| `test_internal_meeting_sends_simple_briefing` | 업체명 없음 → 간단 브리핑(blocks) 발송 |

---

### 4.10 test_during.py — During Agent (회의록 생성 전 과정)

**대상**: `agents/during.py`
**특이사항**: `autouse` 픽스처로 `.sessions/` 파일 I/O 격리

#### TestStartSession (5개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_creates_session` | 세션 생성 후 `_active_sessions`에 등록 |
| `test_default_title` | 제목 없으면 `"미팅"` 기본값 |
| `test_duplicate_session_rejected` | 이미 세션 진행 중이면 거부 + 기존 세션 유지 |
| `test_sends_confirmation_message` | 세션 시작 확인 메시지에 제목 + `/메모` 안내 포함 |
| `test_calendar_event_matched` | 진행 중인 캘린더 이벤트와 `event_id` 자동 매칭 |

#### TestAddNote (6개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_note_added_to_session` | 노트가 세션에 추가됨 |
| `test_multiple_notes_accumulated` | 여러 노트 누적 |
| `test_note_has_timestamp` | 노트에 `"HH:MM"` 형식 타임스탬프 포함 |
| `test_no_session_sends_warning` | 세션 없으면 경고 메시지 |
| `test_empty_note_rejected` | 빈 노트(공백만) 거부 |
| `test_confirmation_shows_note_count` | 확인 메시지에 노트 번호(`#1`) 포함 |

#### TestEndSession (12개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_session_removed_after_end_no_event` | event_id 없는 세션 종료 후 `_active_sessions`에서 삭제 |
| `test_session_removed_after_end_with_event` | event_id 있는 세션 종료 후 `_active_sessions`에서 삭제, `_generate_from_session_end` 백그라운드 스레드 실행 |
| `test_immediate_generation_with_event_id_no_transcript` | event_id 있고 트랜스크립트 없으면 노트만으로 즉시 LLM 생성 |
| `test_immediate_generation_with_event_id_with_transcript` | event_id 있고 트랜스크립트 있으면 노트+트랜스크립트 결합 후 즉시 LLM 생성 |
| `test_immediate_generation_when_no_event_id` | event_id 없으면 즉시 내부용+외부용 LLM 생성 (2회 이상 호출) |
| `test_no_session_sends_warning` | 세션 없으면 경고 메시지 |
| `test_internal_and_external_saved_to_drive` | 내부용·외부용 2개 파일 Drive 저장 |
| `test_minutes_filename_contains_title_and_date` | 파일명에 `YYYY-MM-DD_제목` 형식 포함 |
| `test_internal_and_external_posted_to_slack` | 내부용·외부용 회의록 모두 Slack 발송 |
| `test_empty_notes_handled` | 노트 없이 종료해도 오류 없음 |
| `test_llm_failure_still_saves_to_drive` | LLM 생성 실패해도 Drive 저장 호출됨 (무손실 보장) |
| `test_llm_failure_raw_notes_in_saved_content` | LLM 실패 시 저장 내용에 원본 노트 포함 |

#### TestCheckTranscripts (5개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_transcript_found_generates_minutes` | 트랜스크립트 발견 시 내부용·외부용 회의록 생성 |
| `test_no_transcript_skipped` | 트랜스크립트 없으면 LLM/Drive 호출 없음 |
| `test_completed_notes_combined_with_transcript` | `_completed_notes`의 수동 노트가 트랜스크립트와 결합 (내부용 프롬프트에 양쪽 포함) |
| `test_duplicate_event_skipped` | 이미 처리된 event_id는 재처리 안 함 |
| `test_expired_notes_flushed` | 90분 초과 노트 → fallback으로 노트만 회의록 생성, `_completed_notes` 제거 |

#### TestGetMinutesList (4개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_shows_file_list` | Drive 파일 목록 Slack 발송 |
| `test_empty_list_message` | 회의록 없으면 `"없습니다"` 안내 메시지 |
| `test_no_minutes_folder_warns` | `minutes_folder_id` 없으면 `"재등록"` 경고 |
| `test_limited_to_10_files` | 10개 초과 시 10개만 표시 + 나머지 개수 표시 |

#### TestGenerateFallback (2개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_claude_called_on_gemini_failure` | Gemini 실패(429 등) 시 Claude 폴백 호출 |
| `test_gemini_success_no_claude` | Gemini 성공 시 Claude 미호출 |

#### TestUtilFunctions (4개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_format_notes_empty` | 빈 노트 리스트 → 빈 문자열 |
| `test_format_notes_with_items` | 노트 리스트 → `[HH:MM] 텍스트` 형식 |
| `test_parse_meeting_meta_extracts_fields` | meeting dict에서 date, time_range, attendees 추출 |
| `test_parse_meeting_meta_missing_fields` | 필드 누락 시 기본값 처리 (date → 오늘, attendees → `"정보 없음"`) |

#### TestSessionPersistence (6개)

| 테스트 | 검증 내용 |
|--------|---------|
| `test_active_session_saved_to_file` | `start_session()` 호출 시 `active_{user_id}.json` 파일 생성 |
| `test_note_updates_session_file` | `add_note()` 호출 시 세션 파일 업데이트 |
| `test_active_session_file_deleted_on_end_no_event` | event_id 없는 세션 종료 시 active 파일 삭제 |
| `test_active_session_file_deleted_on_end_with_event` | event_id 있는 세션 종료 시 active 파일 삭제 (`completed` 파일 생성 없음) |
| `test_completed_note_file_deleted_after_transcript_processing` | 폴러가 트랜스크립트 처리 완료 후 completed 파일 삭제 (레거시 파일 처리) |
| `test_load_sessions_recovers_active_and_completed` | 서버 재시작 시 `_load_sessions()`이 파일에서 메모리 상태 복구 |

> **미테스트 (추가 필요)**: `processed_{user_id}.json` 생성/복구 — 트랜스크립트 처리 완료 후 파일 저장, 재시작 후 `_processed_events` 복구 검증

---

## 5. 커버리지 현황

| 영역 | 테스트 있음 | 비고 |
|------|------------|------|
| Calendar 파싱/분류 | ✅ | `parse_event`, `classify_meeting` |
| Slack 메시지 빌더 | ✅ | `format_time`, `build_briefing_message` |
| Gmail 디코딩/파싱 | ✅ | `_decode_body`, `parse_address_header` |
| Gmail 검색 (`search_recent_emails`) | ❌ | 실제 API 의존, 미테스트 |
| Google Docs 추출 | ✅ | `_extract_text`, `read_document` |
| Drive Minutes/Transcript | ✅ | `find_meet_transcript`, `save_minutes`, `list_minutes` |
| Drive Contacts (`get_company_info` 등) | ❌ | 미테스트 |
| OAuth 인증 흐름 | ✅ | `build_auth_url`, `/oauth/callback` |
| User Store (SQLite) | ✅ | 등록, 조회, Drive 설정 업데이트 |
| Before Agent (브리핑) | ✅ | 주요 함수 커버 |
| Before Agent (미팅 생성) | ❌ | `create_meeting_from_text` 미테스트 |
| During Agent (세션/노트/회의록) | ✅ | 전체 흐름 커버 |
| During Agent (세션 파일 영속성) | ✅ | 파일 생성/삭제/복구 전체 커버 |
| During Agent (`processed_` 이벤트 영속성) | ❌ | `_save_processed_events`, 복구 미테스트 |
| `find_meet_transcript` (Gemini 회의록) | ❌ | 루트 경로·한국어 파일명 매칭 미테스트 |
| After Agent (`agents/after.py`) | ❌ | 구현 완료이나 테스트 미작성 (추가 필요) |

---

## 6. 미테스트 영역 및 향후 계획

### 6.1 현재 미테스트 (Before/During Agent)

| 함수 | 이유 |
|------|------|
| `before.create_meeting_from_text()` | LLM + Calendar 생성 복합 흐름, 통합 테스트 필요 |
| `before.research_company()`, `research_person()` | LLM 검색 결과 의존, 외부 I/O 많음 |
| `drive.get_company_info()`, `save_person_info()` | Drive API mock 복잡도 높음 |
| `gmail.search_recent_emails()` | Gmail API 의존 |
| `drive.find_meet_transcript()` (Gemini 경로) | 루트 폴더 탐색·`'Gemini가 작성한 회의록'` 파일명 매칭 미테스트 |
| `during._save_processed_events()` + 복구 | `processed_{user_id}.json` 저장/로드 미테스트 |

### 6.2 After Agent 테스트 추가 필요

After Agent(`agents/after.py`)는 구현 완료되었으나 테스트가 없음. 아래 테스트 클래스 추가 필요:

| 테스트 클래스 | 검증 내용 |
|-------------|---------|
| `TestResolveAttendeeEmails` | Calendar API / Drive / 문자열 기반 이메일 해석 우선순위 |
| `TestExtractActionItems` | LLM 액션아이템 추출, JSON 파싱 실패 처리 |
| `TestSendDraftToSlack` | Block Kit 버튼 메시지 발송, `pending_drafts` DB 저장 |
| `TestHandleSendDraft` | `"발송하기"` 버튼 → Gmail 발송 → 상태 업데이트 |
| `TestHandleCancelDraft` | `"발송 안 함"` 버튼 → 상태 취소 처리 |
| `TestNotifyActionItems` | 담당자 Slack DM 발송, Slack 멤버 이름 매칭 실패 처리 |
| `TestActionItemReminder` | 기한 D-1/D-day 해당 항목 조회 및 DM 발송 |
| `TestUpdateContactsAfterMeeting` | 외부 참석자 `People/` 파일 `last_met` 업데이트 |
| `TestActionItemsDB` | `action_items` 테이블 CRUD |
| `TestPendingDraftsDB` | `pending_drafts` 테이블 CRUD |
