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

### 라이브 서버 (자동 배포)

main 브랜치에 푸시하면 GitHub Actions가 라이브 서버의 `/deploy` 웹훅을 호출하여 자동으로 `git pull` + `pip install` + `systemctl restart`를 수행합니다. 라이브 서버는 systemd 서비스(`meeting-agent.service`)로 관리됩니다.

```bash
# 라이브 서버 로그 확인
sudo journalctl -u meeting-agent -f
```

## 테스트

```bash
pytest tests/                                                        # 전체
pytest tests/test_before.py                                          # 파일 단위
pytest tests/test_during.py::TestEndSession                          # 클래스 단위
pytest tests/test_during.py::TestEndSession::test_specific_case      # 케이스 단위
pytest tests/ -x                                                     # 첫 실패 시 중단
```

테스트는 `ANTHROPIC_API_KEY`, `ENCRYPTION_KEY` 등 환경변수를 직접 `os.environ.setdefault`로 설정하고, `anthropic.Anthropic` / Google API 서비스들을 모두 `unittest.mock.patch`로 차단한 뒤 import합니다. 새 테스트 파일도 동일 패턴을 따라야 합니다.

---

## 요구사항 및 설계

- docs 폴더 아래의 requirement와 design 문서를 참고합니다.
- 드림플러스 회의실 관련 기능은 lib/dreamplus-apis.md 파일을 참고합니다.

## 아키텍처 개요

### 진입점 — `main.py`

Slack 이벤트·슬래시 커맨드·버튼 액션 핸들러를 모두 등록하는 단일 파일입니다. APScheduler로 5개의 정기 작업을 관리합니다:

- `scheduled_briefing()` — 매일 09:00 KST, 전체 사용자 브리핑
- `check_transcripts()` — 10분 주기, Drive 트랜스크립트 폴링
- `action_item_reminder()` — 매일 08:00 KST, 오픈 액션아이템 DM
- `scheduled_feedback_digest()` — 매일 22:00 KST, 사용자 피드백 다이제스트 관리자 발송
- `scheduled_trello_weekly()` — 매주 금요일 21:00 KST, Trello 주간 보고서 (Google Docs 저장 + Slack 요약)

### 에이전트 레이어 — `agents/`


| 파일             | 역할                                                                                           |
| -------------- | -------------------------------------------------------------------------------------------- |
| `before.py`    | 브리핑·리서치·미팅 생성. 브리핑 시 Trello 카드 컨텍스트 포함. 모듈 수준 상태 보유                                  |
| `during.py`    | 트랜스크립트 수집·수동 노트·음성 STT·문서 업로드·회의록 초안 생성. `_pending_minutes` 상태 관리. 트랜스크립트 늦게 도착 시 자동 보강 |
| `after.py`     | 회의록 발송(Gmail)·액션아이템 추출·Contacts 갱신·Trello 등록 제안. During Agent 완료 후 백그라운드 스레드로 실행       |
| `card.py`      | 명함 이미지 OCR (Claude Haiku Vision)                                                             |
| `room.py`      | 드림플러스 회의실 예약·조회·취소·크레딧 관리 (Slack Modal + LLM 파싱)                                        |
| `dreamplus.py` | 드림플러스 API 래퍼 (JWT 인증)                                                                           |
| `proposal.py`  | 제안서 개요·초안 생성, 스레드에서 수정 지원                                                       |
| `feedback.py`  | 사용자 피드백(기능 요청·개선·버그) 수집·분류·저장, 매일 22:00 관리자 다이제스트 발송                         |
| `trello_report.py` | Trello 워크스페이스 주간 보고서 — Google Docs로 상세본 저장 + Slack 요약 발송. 매주 금 21:00 KST         |


### 상태 관리 패턴

에이전트들은 세 종류의 상태를 사용합니다:

- **메모리 딕셔너리**: `_pending_agenda`, `_meeting_drafts`, `_pending_minutes` — 재시작 시 소멸
- **파일 영속화**: `data/pending_agenda.json`, `.sessions/processed_{user_id}.json` — 재시작 후에도 유지
- **SQLite** (`store/users.db`): 사용자 토큰, 액션아이템, 드래프트, Trello 토큰, 피드백, 회의록 인덱스(`meeting_index`)

### LLM 호출 — `agents/before.py`

```python
_search(prompt)    # Claude + web_search (web-search 베타 툴)
_generate(prompt)  # Claude (검색 없음)
generate_text()    # _generate의 public 래퍼 (main.py의 인텐트 분류에서 사용)
```

전 모듈이 Claude `claude-haiku-4-5`를 사용합니다. 회의록 생성·수정(`_generate_minutes`)과 제안서는 Claude `claude-sonnet-4-5`를 직접 사용합니다 (고품질 요구). Gemini는 완전히 제거되었습니다.

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

사용자가 DM으로 기능 요청·개선 제안·버그 리포트를 자연어로 보내면 LLM이 분류·요약하여 DB에 저장하고, 매일 저녁 22:00 KST에 관리자 채널로 다이제스트를 발송합니다.

**흐름:**
1. 사용자 DM → 인텐트 분류(`feedback`) → `feedback.handle_feedback()` 호출
2. LLM이 피드백 유형 분류 (`feature_request` / `improvement` / `bug_report`) 및 요약
3. `feedback` 테이블에 저장 + 사용자에게 접수 확인 DM
4. 매일 22:00 `send_feedback_digest()` → 미전송 건을 카테고리별로 그룹핑하여 관리자 채널 발송 (`original` 전체 텍스트 포함)
5. 발송 완료 후 `notified = 1` 처리

**아키텍처:**
- `agents/feedback.py` — 피드백 분류·저장 (`handle_feedback`), 다이제스트 발송 (`send_feedback_digest`)
- `store/user_store.py` — `feedback` 테이블, `save_feedback()`, `get_pending_feedback()`, `mark_feedback_notified()`
- `main.py` — 인텐트 프롬프트에 `feedback` 추가, 라우팅, 스케줄러 등록

**환경변수:**
- `FEEDBACK_CHANNEL` — 다이제스트 발송 대상 Slack 채널 ID 또는 관리자 사용자 ID (`.env`)

### Trello 주간 보고서

CorpDev_BS 워크스페이스(설정 가능)의 모든 보드를 통합하여 지난 1주간의 변경 내역 중 **신규 카드**, **코멘트**, **체크리스트 항목 완료**, **다음주 기한 항목**(카드 due + 체크리스트 항목 due)만 수집하여:

1. 상세 본문을 Google Docs로 Drive에 저장 (폴더: `Trello 주간 보고서`)
2. Slack 채널에 카드별 요약 + Docs 링크 발송

**흐름:**
- 매주 금요일 21:00 KST `scheduled_trello_weekly()` → `trello_report_agent.send_weekly_report(slack_client)`
- 수동 트리거: `/트렐로주간보고` 또는 `/trello-weekly` (DM/채널 어디서든). 인자로 일수 지정 가능 (예: `/트렐로주간보고 14`)

**아키텍처:**
- `agents/trello_report.py` — Trello REST API로 워크스페이스 전 보드 액션·체크리스트 수집, 원본 Markdown/Slack mrkdwn 분리 포맷
  - `generate_report(user_id, days, workspace)` — 수집 + 원본 Markdown 생성 (Drive/Slack 없음)
  - `send_weekly_report(slack_client, ...)` — 생성 + Drive 저장 + Slack 발송
- `tools/drive.create_draft_doc()` — Markdown → Google Docs 변환 저장
- Drive 폴더는 사용자의 `contacts_folder_id` 부모 위치에 `Trello 주간 보고서` 폴더 자동 생성

**토큰 사용자 선택 규칙:**
- `TRELLO_REPORT_USER_ID` 설정 시 해당 사용자의 Trello 토큰 + Google Drive 자격증명 사용
- 미설정 시 토큰 보유자(`trello_token_enc IS NOT NULL`) 중 첫 번째 자동 선택

**환경변수:**
- `TRELLO_WORKSPACE` — 대상 워크스페이스 이름/displayName (기본 `CorpDev_BS`)
- `TRELLO_REPORT_CHANNEL` — 보고서 발송 Slack 채널 (미설정 시 `FEEDBACK_CHANNEL` 폴백)
- `TRELLO_REPORT_USER_ID` — Trello·Drive 토큰 사용 대상 Slack user_id (선택)
- `TRELLO_REPORT_FOLDER_NAME` — Drive 저장 폴더명 (기본 `Trello 주간 보고서`)

### 자동 배포 — GitHub Actions 웹훅

SSH 대신 웹훅 방식으로 배포합니다 (서버 22 포트 오픈 불필요).

**흐름:** main 푸시 → GitHub Actions → `POST /deploy` (HMAC 시그니처 검증) → `git pull` + `pip install` + 관리자 채널 알림 + `systemctl restart`

**배포 알림:** `_notify_deploy()`가 `FEEDBACK_CHANNEL`에 버전(short hash)과 커밋 메시지 목록을 발송. git pull 전후 커밋 해시를 비교하여 변경 커밋만 추출 (`--no-merges`).

**관련 파일:**
- `.github/workflows/deploy.yml` — GitHub Actions 워크플로우
- `server/oauth.py` — `/deploy` 엔드포인트 (HMAC-SHA256 검증) + `_notify_deploy()` 배포 알림

**환경변수:**
- `DEPLOY_SECRET` — HMAC 시그니처 검증용 비밀 키 (`.env` + GitHub Secrets 동일 값)

**GitHub Secrets:**
- `DEPLOY_URL` — 라이브 서버 URL (예: `https://meeting.yourdomain.com`)
- `DEPLOY_SECRET` — 서버 `.env`와 동일한 값

**다이제스트 출력 순서:** 버그 리포트 → 기능 요청 → 개선 요청 (긴급도 순)

### 관리자 페이지

백엔드 FastAPI가 JSON API와 프론트엔드 정적 파일을 하나의 프로세스로 동시에 서빙합니다. 별도 프론트엔드 서버 불필요. 배포 URL은 `https://meeting.parametacorp.com/admin/`.

**백엔드 엔드포인트** (`server/admin.py`):
- `GET /admin/api/dashboard` — 집계(users/meetings/feedback/action_items) + 최근 피드백 5건
- `GET /admin/api/users` — 사용자 목록 (Slack 프로필 이름·이메일 포함, 민감 필드 제외)
- `GET /admin/api/feedback?filter=all|pending|notified` — 피드백 목록

**정적 프론트엔드:** `server/oauth.py`에서 `app.mount("/admin", StaticFiles(directory="frontend", html=True))`. APIRouter(prefix `/admin/api`)가 먼저 등록되므로 라우팅 충돌 없음.

**인증:** HTTP Basic Auth — `ADMIN_PASSWORD` 환경변수와 `secrets.compare_digest` 비교. SPA가 직접 관리하도록 `auto_error=False` + `WWW-Authenticate` 헤더 미전송으로 브라우저 기본 프롬프트 차단. 환경변수 미설정 시 503.

**Slack 프로필 조회:** `_lookup_profile()`이 `slack_client.users_info()`로 `profile.display_name`/`real_name`/`email` 조회. 프로세스 수명 in-memory 캐시(`_profile_cache`)로 매 요청마다 API 호출하지 않음. 갱신 필요 시 서버 재시작.

**CORS:** 로컬 standalone dev(:3030)에서 운영 백엔드로 붙을 때만 필요. 프로덕션은 same-origin이라 CORS 불필요. 기본 허용: `localhost:3030/3000`. 추가 오리진은 `ADMIN_FRONTEND_ORIGINS` (쉼표 구분).

**프론트엔드** (`frontend/`, 빌드 도구 없음):
- `index.html` — SPA 셸
- `config.js` — `window.BACKEND_URL` 자동 감지 (port `3030`이면 `http://localhost:8000`, 그 외 same-origin)
- `app.js` — 해시 라우팅(`#/dashboard|#/users|#/feedback`), Basic Auth를 `sessionStorage`에 저장하여 매 요청에 첨부
- `style.css` — 스타일
- `serve.sh` — 로컬 standalone dev용 런처 (`python3 -m http.server 3030`)

**로컬 테스트 두 가지 방식:**
```bash
# 방식 A — 백엔드가 프론트도 함께 서빙 (프로덕션과 동일 구조)
bash start.sh                       # http://localhost:8000/admin/

# 방식 B — 프론트엔드만 따로 띄우고 운영 API에 붙임
cd frontend && ./serve.sh           # http://localhost:3030 → config.js의 BACKEND_URL
```

**자동배포:** `git pull`이 `frontend/` 파일까지 함께 가져오므로 추가 빌드 스텝 없음. `.github/workflows/deploy.yml`이 배포 후 `/health`·`/admin/`·`/admin/api/dashboard`(인증 401) 3단계 검증.

**환경변수:**
- `ADMIN_PASSWORD` — Basic Auth 비밀번호 (`.env` + 라이브 서버 `.env` 양쪽 필요)
- `ADMIN_FRONTEND_ORIGINS` — 추가 CORS 오리진 (선택, 쉼표 구분)

### 회의록 검색

`/회의록 [검색어]` 또는 자연어(`지난 목요일 회의록`, `카카오 회의록 찾아줘`)로 회의록을 검색합니다. Drive Minutes 폴더의 파일명(`{YYYY-MM-DD}_{제목}_내부용.md`)을 기반으로 필터링합니다.

**지원 날짜 표현:**
- 정확한 날짜: `2026-04-13`, `4/13`, `4월 13일`
- 요일: `지난 월요일`, `지난 목요일`
- 상대 기간: `어제`, `오늘`, `지난주`, `이번주`, `지난달`, `이번달`
- 범위: `2026-04-01 ~ 2026-04-13`, `2026-04`

**인텐트 통합:** `get_minutes` 인텐트 하나로 처리. `params.query`가 있으면 검색, 없으면 전체 목록 조회.

### 업체 메모

자연어(`카카오 메모 — PoC 예산 확보`)로 업체 Wiki 파일의 `## 내부 메모` 섹션에 타임스탬프와 함께 기록합니다. 업체 파일이 없으면 새로 생성합니다.

**업체 리서치 시 내부 메모 보존:** `research_company()`는 `## 최근 동향`, `## 이메일 맥락`, `## 파라메타 서비스 연결점`, `## ParaScope` 섹션만 갱신하고, `## 내부 메모` 등 리서치 대상이 아닌 섹션은 그대로 유지합니다.

### 제안서 워크플로우

`agents/proposal.py`가 회의 기반 제안서 개요·초안을 생성합니다. 개요/초안 각각 Slack 스레드에서 수정 가능합니다. 프롬프트 템플릿: `prompts/templates/proposal_intake.md`, `proposal_generate.md`.

### 브리핑 업체명 추론

`_infer_company_from_title()`은 LLM에 업체명 추론을 요청할 때, 추론 불가 시 `NONE`만 반환하도록 프롬프트에 명시. 설명문 반환 방지.

### 회의록 생성 — 소스 & 플로우

**입력 소스 (4가지):**

| 소스 | 입력 방식 | 처리 |
|------|----------|------|
| Google Meet 트랜스크립트 | 자동 (Drive에 생성) | 폴링으로 수집 → `transcript_text` |
| 수동 텍스트 노트 | 세션 스레드에 직접 타이핑 | `input_type="note"` → `notes_text` |
| 음성 파일 STT | DM에 오디오 파일 첨부 | Deepgram → `input_type="audio"` → `notes_text` |
| 텍스트 문서 업로드 | DM에 텍스트/문서 파일 첨부 | 텍스트 추출 → `input_type="document"` → `notes_text` |

수동 노트·음성 STT·문서 업로드는 모두 세션 노트로 합쳐져 `notes_text`가 되고, 트랜스크립트는 별도 `transcript_text`로 회의록 생성에 들어감.

**생성 경로 (4가지):**

- **경로 A — `/미팅종료`**: `end_session()` → **소스 선택 블록**(`🎙️ 트랜스크립트 / 📝 노트만 / 🕐 트랜스크립트 대기 / ❌ 취소`) → 사용자 선택 → `handle_minutes_source_select()` → `_generate_from_session_end(source=...)`. `transcript`면 1회 탐색 후 없으면 경로 D 등록. `notes`면 즉시 노트만으로 생성. `wait`면 경로 D만 등록하고 즉시 생성 안 함.
- **경로 B — 자동 폴링**: `check_transcripts()` → `_check_transcripts_for_user()`. 10분 주기로 최근 종료(10~90분) 미팅의 트랜스크립트 탐색. 트랜스크립트(필수) + 노트(있으면).
- **경로 C — 노트 fallback**: `_flush_expired_notes()`. 90분 경과 후 트랜스크립트 없이 노트만으로 생성.
- **경로 D — 트랜스크립트 늦게 도착 보강**: `_check_awaiting_transcripts()`. 경로 A에서 트랜스크립트 없이 생성 후, `_awaiting_transcript` 딕셔너리에 등록. 10분 주기 폴링으로 90분간 트랜스크립트 도착 체크. 도착 시 트랜스크립트 + 기존 노트로 보강 회의록 재생성 → 사용자 검토.

모든 경로 → `_generate_and_post_minutes()` → 내부용·외부용 회의록 생성 (Claude Sonnet) → Slack 초안 발송 (✅저장/✏️수정/❌취소) → 사용자 확인 후 Drive 저장 + After Agent 트리거.

**채널/스레드 유지 (B2):** 세션이 채널에서 시작된 경우(`session_channel`/`session_thread_ts`), 종료 알림·소스 선택 블록·회의록 초안·최종 링크 메시지가 모두 같은 채널(+스레드)에 발송됨. DM에서 시작된 세션은 DM으로 유지. `_generate_and_post_minutes`·`_post_minutes_draft`·`_post_combined_minutes`·`_check_awaiting_transcripts`가 `post_channel`/`post_thread_ts`를 전파.

**스레드 메모 대상 확정 (B3):** 회의록 초안 스레드에 수정 요청을 달면 `find_draft_by_thread_ts(user_id, thread_ts)`로 해당 스레드의 초안을 정확히 식별해 수정. 복수 초안이 있어도 엉뚱한 회의록에 반영되지 않음.

**복수 후보 미팅 모호성 해소 (B1):** `/미팅시작`에 현재 시각 진행 중인 이벤트가 2개 이상이면 자동 선택 대신 선택 UI를 발송. 해당 클릭 흐름은 `_prompt_event_selection` → `handle_event_selection` (기존 헬퍼 재사용).

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
- main 브랜치에서 작업이 시작되면 브랜치를 만들어 작업함. 이미 브랜치에 있으면 그 브랜치에서 계속 작업
- 명시적인 커밋과 푸시 명령이 있을때에만 커밋과 푸시를 수행함
- `tools/calendar.py`의 `create_event()`는 `location` 파라미터를 지원함 (자연어 미팅 생성 시 장소 설정)
- 일정 드래프트 스레드에서 업체명은 참석자와 동일한 누적 패턴: "업체 추가해줘 X" → 기존 유지 + 추가, "업체는 X야" → 대체. `draft["company"]`는 쉼표 구분 문자열, LLM 호출 전 `company_candidates` 배열로 동기화됨

