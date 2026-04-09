# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## 서버 실행 및 운영

```bash
# 서버 시작 (기존 프로세스 종료 후 단일 인스턴스 실행)
bash start.sh

# 로그 확인
tail -f server.log

# 서버 종료
kill $(cat server.pid)
```

서버는 Slack Bolt (Socket Mode) + FastAPI (OAuth 콜백) 두 프로세스가 동일 `main.py`에서 병렬 실행됩니다. `start.sh` 없이 직접 `python3 main.py`를 실행하면 중복 인스턴스가 생길 수 있으니 항상 `start.sh`를 사용하세요.

개발용으로 서버를 실행할때는 공개IP를 확보해야해서, ngrok를 먼저 실행해야 합니다.

## 테스트

```bash
pytest tests/                                                        # 전체
pytest tests/test_before.py                                          # 파일 단위
pytest tests/test_during.py::TestEndSession                          # 클래스 단위
pytest tests/test_during.py::TestEndSession::test_specific_case      # 케이스 단위
pytest tests/ -x                                                     # 첫 실패 시 중단
```

테스트는 `GOOGLE_API_KEY`, `ENCRYPTION_KEY` 등 환경변수를 직접 `os.environ.setdefault`로 설정하고, `google.genai.Client` / `anthropic.Anthropic` / Google API 서비스들을 모두 `unittest.mock.patch`로 차단한 뒤 import합니다. 새 테스트 파일도 동일 패턴을 따라야 합니다.

---

## 요구사항 및 설계

- docs 폴더 아래의 requirement와 design 문서를 참고합니다.
- 드림플러스 회의실 관련 기능은 lib/dreamplus-apis.md 파일을 참고합니다.

## 아키텍처 개요

### 진입점 — `main.py`

Slack 이벤트·슬래시 커맨드·버튼 액션 핸들러를 모두 등록하는 단일 파일입니다. APScheduler로 4개의 정기 작업을 관리합니다:

- `scheduled_briefing()` — 매일 09:00 KST, 전체 사용자 브리핑
- `check_transcripts()` — 10분 주기, Drive 트랜스크립트 폴링
- `action_item_reminder()` — 매일 08:00 KST, 오픈 액션아이템 DM
- `scheduled_feedback_digest()` — 매일 08:00 KST, 사용자 피드백 다이제스트 관리자 발송

### 에이전트 레이어 — `agents/`


| 파일             | 역할                                                                                           |
| -------------- | -------------------------------------------------------------------------------------------- |
| `before.py`    | 브리핑·리서치·미팅 생성. 브리핑 시 Trello 카드 컨텍스트 포함. 모듈 수준 상태 보유                                  |
| `during.py`    | 트랜스크립트 수집·수동 노트·회의록 초안 생성. `_pending_minutes` 상태 관리                                          |
| `after.py`     | 회의록 발송(Gmail)·액션아이템 추출·Contacts 갱신·Trello 등록 제안. During Agent 완료 후 백그라운드 스레드로 실행       |
| `card.py`      | 명함 이미지 OCR (Claude Haiku Vision)                                                             |
| `room.py`      | 드림플러스 회의실 예약·조회·취소·크레딧 관리 (Slack Modal + LLM 파싱)                                        |
| `dreamplus.py` | 드림플러스 API 래퍼 (JWT 인증)                                                                           |
| `feedback.py`  | 사용자 피드백(기능 요청·개선·버그) 수집·분류·저장, 매일 08:00 관리자 다이제스트 발송                         |


### 상태 관리 패턴

에이전트들은 세 종류의 상태를 사용합니다:

- **메모리 딕셔너리**: `_pending_agenda`, `_meeting_drafts`, `_pending_minutes` — 재시작 시 소멸
- **파일 영속화**: `data/pending_agenda.json`, `.sessions/processed_{user_id}.json` — 재시작 후에도 유지
- **SQLite** (`store/users.db`): 사용자 토큰, 액션아이템, 드래프트, Trello 토큰, 피드백

### LLM 호출 — `agents/before.py`

```python
_search(prompt)    # Gemini + GoogleSearch → 실패 시 Claude + web_search
_generate(prompt)  # Gemini generate → 실패 시 Claude (검색 없음)
generate_text()    # _generate의 public 래퍼 (main.py의 인텐트 분류에서 사용)
```

Gemini `gemini-2.0-flash`가 기본, 오류(429 등) 시 Claude `claude-haiku-4-5`로 자동 폴백합니다.

회의록 생성·수정(`_generate_minutes`)은 Claude `claude-sonnet-4-5`를 직접 사용합니다 (Gemini 폴백 없음).

### 브리핑 비동기 흐름

`run_briefing()`은 2단계로 동작합니다:

1. **즉시**: 모든 미팅의 헤더 블록을 순서대로 발송 (`_send_briefing` → `build_meeting_header_block`)
2. **백그라운드**: 단일 스레드에서 업체별 순차 처리 (`_run_all_briefing_research` → `_run_briefing_research`) — 다중 업체 결과 섞임 방지

**브리핑 기간 파싱**: 인텐트 분류 시 LLM이 자연어 기간을 `start_date`/`end_date` (YYYY-MM-DD) + `period_text` (표시용)로 자유롭게 변환합니다. 어떤 자연어 기간 표현이든 지원 (이번주, 다음주 월요일, 4월 셋째 주, 앞으로 2주 등). 기간 미지정 시 기본값은 향후 24시간 (`start_date`/`end_date`가 null → `days=1, from_now=True` 폴백). 주 기준은 일~토. "이번주"와 "일주일간"은 다른 범위.

**브리핑 헤더 표시 형식**: 관련 업체(`🏢`), 참석자(`👥`), 어젠다(`📝`) 등은 각각 별도 필드로 줄바꿈하여 표시. 업체명을 제목 괄호 안에 넣지 않음. 어젠다 내용은 `*어젠다*:` 다음 줄부터 출력.

**브리핑 인물 리서치**: `INTERNAL_DOMAINS` (parametacorp.com, iconloop.com) 이메일 참석자는 내부인으로 인물 리서치 대상에서 제외.

**브리핑 참석자 표시**: `_resolve_attendee_names()`가 이메일→이름 변환 (Calendar displayName → Slack 프로필 → Google 주소록 → 이메일 폴백). Slack email→name 캐시는 프로세스 수명 동안 유지.

**ParaScope 연동**: `_query_parascope()` 함수가 구현되어 있으나 호출부 비활성화 상태 (보류 중).

### Google OAuth — 스코프 주의사항

스코프는 두 파일에서 관리합니다:

- `server/oauth.py` — 실제 사용자 동의 화면 요청 스코프 (여기서 추가해야 동의 화면에 반영)
- `store/user_store.py` — 토큰 복원 시 사용 (기존 토큰 갱신 호환성 때문에 `contacts.readonly` 의도적 제외)

스코프를 추가할 경우 **두 파일 모두 확인**하고, 기존 사용자에게 `/재등록` 안내가 필요한지 판단해야 합니다.

### Trello 연동

파이프라인(업체) 카드 읽기/쓰기를 통해 브리핑과 액션아이템을 Trello와 연동합니다.

**인증 흐름** (사용자별 OAuth):
1. `/trello` 커맨드 → Slack에 인증 링크 발송
2. 링크 클릭 → 서버 리다이렉트(`/trello/auth`) → Trello 승인 페이지
3. 승인 → Trello가 `/trello/callback#token=xxx`로 리다이렉트
4. JS가 토큰 추출 → `POST /trello/save` → DB에 Fernet 암호화 저장 → Slack DM 알림
5. 폴백: 승인 후 토큰을 DM에 직접 붙여넣기도 지원

**아키텍처:**
- `tools/trello.py` — Trello REST API 래퍼 (py-trello). 사용자별 클라이언트 캐시 (`_client_for_user(user_id)`)
- `server/oauth.py` — `build_trello_auth_url()`, `/trello/auth` (302 리다이렉트), `/trello/callback`, `/trello/save`
- `store/user_store.py` — `trello_token_enc` 컬럼, `save/get/clear_trello_token()`

**환경변수:**
- `TRELLO_API_KEY` — Power-Up API Key (앱 공통, `.env`)
- `TRELLO_BOARD_ID` — 대상 보드 ID (`.env`)
- Token — 사용자별로 DB 저장 (`.env`에 없음)

**Before Agent 연동** (`agents/before.py`):
- `get_previous_context()` → `trello.get_card_context(user_id, company_name)` 호출
- 브리핑에 미완료 체크리스트 항목 표시

**After Agent 연동** (`agents/after.py`):
- 회의록 완료 후 `_propose_trello_registration()` → Slack 등록/건너뜀 버튼
- `_infer_company_name()` — LLM으로 회의 제목에서 업체명 추론
- `handle_trello_register()` — 카드에 액션아이템 체크리스트 추가 (카드 없으면 자동 생성)

**규칙:**
- 업체 1개 = 카드 1개 (카드명 = 업체명)
- 체크리스트 항목 포맷: `[담당자] 작업 제목 (기한: YYYY-MM-DD)`
- 카드 이동/삭제/체크리스트 완료 처리 금지
- `DRY_RUN_TRELLO=true` 환경변수로 API 호출 없이 테스트 가능

**Slack URL 주의사항:**
- Slack은 `&`가 포함된 긴 URL의 쿼리 파라미터를 깨뜨릴 수 있음
- 해결: 서버 리다이렉트 방식 사용 — Slack에는 짧은 URL(`/trello/auth?state=xxx`) 전송, 서버에서 302로 전체 Trello URL로 리다이렉트

### 피드백 수집·다이제스트

사용자가 DM으로 기능 요청·개선 제안·버그 리포트를 자연어로 보내면 LLM이 분류·요약하여 DB에 저장하고, 매일 아침 08:00 KST에 관리자 채널로 다이제스트를 발송합니다.

**흐름:**
1. 사용자 DM → 인텐트 분류(`feedback`) → `feedback.handle_feedback()` 호출
2. LLM이 피드백 유형 분류 (`feature_request` / `improvement` / `bug_report`) 및 요약
3. `feedback` 테이블에 저장 + 사용자에게 접수 확인 DM
4. 매일 08:00 `send_feedback_digest()` → 미전송 건을 카테고리별로 그룹핑하여 관리자 채널 발송
5. 발송 완료 후 `notified = 1` 처리

**아키텍처:**
- `agents/feedback.py` — 피드백 분류·저장 (`handle_feedback`), 다이제스트 발송 (`send_feedback_digest`)
- `store/user_store.py` — `feedback` 테이블, `save_feedback()`, `get_pending_feedback()`, `mark_feedback_notified()`
- `main.py` — 인텐트 프롬프트에 `feedback` 추가, 라우팅, 스케줄러 등록

**환경변수:**
- `FEEDBACK_CHANNEL` — 다이제스트 발송 대상 Slack 채널 ID 또는 관리자 사용자 ID (`.env`)

**다이제스트 출력 순서:** 버그 리포트 → 기능 요청 → 개선 요청 (긴급도 순)

---

## 프롬프트 템플릿

`prompts/templates/*.md` 파일을 수정하면 **서버 재실행 시** 즉시 반영됩니다 (코드 수정 불필요). 변수는 `{{변수명}}` 형식을 사용합니다.

인라인으로 관리되는 프롬프트(미팅 파싱, 인텐트 분류, 액션아이템 추출 등)는 `prompts/briefing.py`에서 직접 수정합니다.

---

## 슬랙봇 동작

- 슬랙봇은 DM을 통해 슬래시 커맨드 형태로도 명령어를 받을 수 있지만, 자연어 명령어도 받을 수 있습니다.
- 채널에서 '@' 멘션을 통한 자연어 명령어에 대한 답변은 기본적으로 쓰레드로 보내집니다.

## 코드 규칙

- 주석·로그 메시지는 한국어 사용
- 로그는 `log.info()` / `log.warning()` / `log.exception()` 사용 (`print` 금지)
- 백그라운드 작업은 `threading.Thread(target=..., daemon=True).start()` 패턴
- Drive 파일명 검색 시 macOS NFD/NFC 유니코드 정규화 이슈 주의 (`tools/drive.py`의 `_find_file` 참고)
- SSL 검증: 사내 방화벽 환경 대응으로 일부 외부 API 호출에 `verify=False` 사용 중
- 작업 시 main 브랜치에서 직접 커밋하지 않고, 기능/수정별 브랜치를 생성하여 작업함
- 명시적인 커밋과 푸시 명령이 있을때에만 커밋과 푸시를 수행함
- `tools/calendar.py`의 `create_event()`는 `location` 파라미터를 지원함 (자연어 미팅 생성 시 장소 설정)
- 일정 드래프트 스레드에서 업체명은 참석자와 동일한 누적 패턴: "업체 추가해줘 X" → 기존 유지 + 추가, "업체는 X야" → 대체. `draft["company"]`는 쉼표 구분 문자열, LLM 호출 전 `company_candidates` 배열로 동기화됨

