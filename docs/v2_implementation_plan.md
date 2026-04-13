# v2 구현 계획

2026-04-13 | 요구사항: `requirements/meeting_agent_integrated_v2.md`

---

## 현재 상태 vs 요구사항 갭


| 영역         | 현재                     | 요구사항                       |
| ---------- | ---------------------- | -------------------------- |
| 동시성        | Lock 없음, user_id 키     | Lock 적용, event_id 키 전환     |
| SQLite     | 기본 journal 모드          | WAL 모드                     |
| 회의록 초안     | 메모리 위주 (일부 파일 백업)      | 완전한 파일 영속화                 |
| 브리핑 업체 인식  | extendedProperties만    | 5단계 폴백 추론                  |
| 회의록 품질 검증  | 없음                     | 필수항목 검증 + 자동 재생성           |
| 회의록 검색     | 최근 10개 파일명만            | SQLite 인덱스 + 업체/기간 검색      |
| Trello 코멘트 | add_comment() 존재하나 미활용 | 회의 요약 코멘트 자동 추가            |
| 제안서        | 미구현                    | 키워드 감지 → 개요 확인 → 생성 워크플로우  |
| Wiki 구조    | 플랫 파일                  | `[[링크]]` 상호참조 + 출처 태깅      |
| 기업 메모      | 경로 없음                  | 자연어 입력 → Wiki 내부 메모 append |


---

## Phase 2.1 — 안정화 + 즉시 효과 (1~2주)

기존 코드 수정 위주. 새 기능 없이 안정성과 품질 향상.

### 1. 브리핑 업체명 추론 폴백 (FR-B13, FR-B14)

- **파일**: `agents/before.py`
- **작업**:
  - `_infer_company_from_title()` 함수 추가
  - LLM 추론 시 Drive 기존 업체 목록을 프롬프트에 포함
  - `extendedProperties` 없을 때 자동 폴백 체인 적용
- **폴백 순서**:
  1. `extendedProperties.private.company`
  2. LLM 제목 추론 (기존 업체 목록 후보 제공)
  3. Drive 기존 업체 목록과 퍼지 매칭
  4. (Phase 2.2) 참석자 이메일 도메인 역추론
  5. (Phase 2.3) 인물 파일에서 소속 회사 조회

### 2. 동시성 Lock 적용 (INF-07)

- **파일**: `agents/during.py`, `agents/before.py`
- **작업**:
  - `during.py`: `_sessions_lock`, `_minutes_lock`, `_inputs_lock` 추가
  - `before.py`: `_agenda_lock`, `_drafts_lock` 추가
  - 모든 공유 딕셔너리 접근부에 `with lock:` 적용

### 3. SQLite WAL 모드 (INF-08)

- **파일**: `store/user_store.py`
- **작업**: DB 연결 시 `PRAGMA journal_mode=WAL` 추가

### 4. _pending_minutes 키 event_id 전환 (FR-D14)

- **파일**: `agents/during.py`, `main.py`
- **작업**:
  - `_pending_minutes` 키를 `user_id` → `event_id`로 변경
  - Slack 버튼 value에 event_id 포함하여 매핑
  - user_id로 해당 사용자의 pending minutes 조회하는 헬퍼 함수 추가

### 5. 회의록 초안 파일 영속화 (INF-09)

- **파일**: `agents/during.py`
- **작업**:
  - `_pending_minutes` 변경 시마다 `.sessions/pending_minutes.json`에 저장
  - 서버 시작 시 파일에서 복원
  - credentials 등 직렬화 불가 객체 제외 처리

### 6. 회의록 품질 검증 (FR-D09, FR-D10)

- **파일**: `agents/during.py`
- **작업**:
  - `validate_minutes(body, minute_type)` 함수 추가
  - 내부용 필수 섹션: 회의 요약, 액션 아이템(테이블), 주요 결정 사항, 주요 논의 내용
  - 외부용 필수 섹션: 회의 개요, 주요 합의 사항, 공동 액션 아이템
  - 외부용 금지 키워드: "내부 메모", "협상", "전략"
  - 누락 시 LLM 재생성 루프 (최대 2회)
  - `_generate_minutes()` 호출부에 검증 루프 삽입

### 7. Trello 코멘트 활용 (FR-A15)

- **파일**: `agents/after.py`, `tools/trello.py`
- **작업**:
  - `handle_trello_register()` 내 체크리스트 등록 성공 후 `trello.add_comment()` 호출
  - 코멘트 양식: 날짜 + 3줄 요약 + Drive 링크 + 참석자 + 주요 결정

---

## Phase 2.2 — 검색 + 기업 메모 (1~2주)

SQLite 인덱스 추가 + 검색 명령어 구현.

### 1. meeting_index 테이블 (INF-10)

- **파일**: `store/user_store.py`
- **작업**:
  - `meeting_index` 테이블 + 인덱스 생성 (company_name, date, user_id)
  - `save_meeting_index()`, `search_meetings()` 함수 추가
  - 회의록 Drive 저장 성공 후 자동 INSERT

### 2. 회의록 검색 명령어 (FR-D11, FR-D12)

- **파일**: `main.py`, `agents/during.py`
- **작업**:
  - `/회의록 {업체명}` 업체 기반 검색
  - `/회의록 {YYYY-MM}` 기간 기반 검색
  - `/회의록 {업체명} {기간}` 복합 검색
  - 검색 결과 Slack 블록 포맷팅

### 3. 기존 회의록 마이그레이션 (INF-11)

- **파일**: 신규 스크립트 `scripts/migrate_meeting_index.py`
- **작업**: Drive Minutes/ 폴더 스캔 → 파일명 파싱 → meeting_index 일괄 INSERT

### 4. 기업 메모 자연어 입력 (CM-11)

- **파일**: `main.py`, `tools/drive.py`
- **작업**:
  - `main.py` 인텐트 분류에 `company_memo` 추가
  - LLM이 업체명 + 메모 내용 추출
  - Drive 기업 파일의 `## 내부 메모` 섹션에 날짜와 함께 append

### 5. Trello 카드 생성 확인 (FR-A16)

- **파일**: `agents/after.py`
- **작업**:
  - `create_if_missing=True` 무조건 생성 → Slack 버튼 확인 후 생성으로 변경
  - "카드가 없습니다. 새로 만들까요?" [생성 후 등록] [건너뛰기]

### 6. 추론 업체명 extendedProperties 저장 (FR-B15)

- **파일**: `agents/before.py`, `tools/calendar.py`
- **작업**: 추론 성공 시 `cal.update_event_property(creds, event_id, "company", inferred_company)` 호출

---

## Phase 2.3 — Wiki 구조 전환 (2~3주)

Drive 파일 구조 변경. 기존 파일 마이그레이션 포함.

### 1. `[[링크]]` 상호 참조 삽입 (CM-07)

- **파일**: `tools/drive.py`
- **작업**:
  - `save_minutes()` 시 하단에 관련 자료 링크 블록 추가
  - `save_company_info()` 시 관련 인물 `[[링크]]` 삽입
  - `save_person_info()` 시 소속 기업 `[[링크]]` 삽입

### 2. 미팅 히스토리 자동 갱신 (CM-08)

- **파일**: `tools/drive.py`
- **작업**: 회의록 저장 시 해당 업체/인물 파일의 `## 미팅 히스토리` 테이블에 행 추가

### 3. 출처 태그 부착 (CM-09)

- **파일**: `agents/before.py`, `tools/drive.py`
- **작업**: `research_company()`에서 정보 수집 시 `[출처: {type}]` 인라인 태그 부착

### 4. Sources/ 원본 보관 (CM-10)

- **파일**: `tools/drive.py`, `agents/during.py`, `agents/before.py`
- **작업**:
  - `Sources/Transcripts/` — 트랜스크립트 원문 저장
  - `Sources/Emails/` — 이메일 스레드 원문 저장
  - `Sources/Research/` — 웹 검색 결과 원문 저장

### 5. 자연어 회의록 검색 (FR-D13)

- **파일**: `main.py`
- **작업**: "카카오 지난달 회의록 찾아줘" 같은 자연어 → 인텐트 분류 + 검색 파라미터 추출

### 6. 복수 미팅 대기열 (FR-D15)

- **파일**: `agents/during.py`
- **작업**:
  - 동일 사용자 진행 중 작업 감지
  - 대기열 관리 + 순차 처리 + 완료 알림

---

## Phase 2.4 — 제안서 워크플로우 (2~3주)

신규 기능. v1 FR-A06, FR-A07 구현.

### 1. 제안서 트리거 감지 + 제안 (FR-A11)

- **파일**: `agents/after.py`, `main.py`
- **작업**:
  - 회의록 확정 후 본문에서 트리거 키워드 감지
  - 키워드: 협업, 제안, MOU, PoC, 파일럿, 공동개발, 제휴, 투자, 계약, 도입, 검토, 다음 단계
  - Slack 제안 버튼: [📝 제안서 작성] [건너뛰기]

### 2. intake 자동 추출 + 개요 제시 (FR-A12)

- **파일**: `agents/after.py` (또는 신규 `agents/proposal.py`)
- **작업**:
  - 회의록에서 목적/대상/범위/배경 자동 추출
  - `company_knowledge.md` 기반 우리 강점 포함
  - 개요를 Slack에 제시: [✅ 진행] [✏️ 개요 수정] [❌ 취소]

### 3. 개요 확인 → 생성 → 수정 루프 (FR-A13)

- **파일**: `agents/after.py`, `main.py`
- **작업**:
  - 개요 수정 대화 루프
  - 확정 후 제안서 본문 생성 (회의록 + 기업 Wiki + 이전 맥락 기반)
  - 수정 요청 처리

### 4. Google Docs 공유 + 편집 (FR-A14)

- **파일**: `tools/drive.py`
- **작업**:
  - `Proposals/` 폴더에 Google Docs 생성
  - Drive 링크 Slack 발송
  - 직접 편집 가능한 형태

### 5. 참석자 기반 업체 역추론 (FR-B16)

- **파일**: `agents/before.py`
- **작업**:
  - 참석자 이메일 도메인 → 기업명 역추론
  - `People/` 파일에서 소속 회사 조회

---

## 수정 대상 파일 요약


| 파일                    | Phase              | 수정 사항                                        |
| --------------------- | ------------------ | -------------------------------------------- |
| `agents/before.py`    | 2.1, 2.2, 2.3, 2.4 | 업체명 추론 폴백, Lock, 출처 태그, 참석자 역추론              |
| `agents/during.py`    | 2.1, 2.3           | 품질검증, event_id 키 전환, Lock, 영속화, 대기열          |
| `agents/after.py`     | 2.1, 2.2, 2.4      | Trello 코멘트, 카드 생성 확인, 제안서 워크플로우              |
| `tools/drive.py`      | 2.2, 2.3, 2.4      | Wiki 구조, `[[링크]]`, Sources/, 기업 메모, 제안서 Docs |
| `tools/trello.py`     | 2.1                | add_comment() 프로덕션 연결                        |
| `store/user_store.py` | 2.1, 2.2           | WAL 모드, meeting_index 테이블                    |
| `main.py`             | 2.1, 2.2, 2.3, 2.4 | 검색 명령어, company_memo 인텐트, 제안서 버튼 핸들러         |


