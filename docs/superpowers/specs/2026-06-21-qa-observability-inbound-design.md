# Q&A 관측 — 인바운드 캡처 + 대화 타임라인 (설계)

> 선행: `2026-06-20-admin-message-observability` (아웃바운드 관측 — 배포 완료).
> 본 문서는 그 후속(Phase 2)으로 **인바운드(사용자 입력)** 포착과 **사용자별 대화 타임라인**을 추가한다.

## 1. 배경 / 목표

관리자가 **"누가 무엇을 묻고, 봇이 무엇을 답했는지"**를 대화 흐름으로 확인할 수 있게 한다.

- **답변(아웃바운드)**: 이미 관측됨 — `message_log` + `tools/slack_logger.py`(send 3종 래핑) + 관리자 "메시지" 탭. 배포 완료.
- **질문(인바운드)**: 현재 미포착. 본 설계로 추가.

### 비목표 (Out of scope)

- 질문↔답 **1:1 페어링/상관관계 추적** — 시간순 인터리브 타임라인으로 충분(봇 답은 비동기·여러 통·무관 발송 섞임이라 엄밀 페어링은 깨지기 쉬움).
- 버튼 클릭(`action`) 등 상호작용 로깅 — "질문"이 아님.
- 인바운드 콘텐츠 LLM 분석/요약/인텐트 재분류.

## 2. 현재 구조 (코드 근거)

- 테이블 `message_log` — `store/user_store.py:224`. 컬럼: `ts/method/channel/recipient_user_id/recipient_kind/thread_ts/text/blocks_json/category/ok/error`. **`direction` 없음.**
- 함수: `log_message(...)` (`user_store.py:930`), `list_messages(...)` (`:948`, `user_id`은 `recipient_user_id`로 필터, `ORDER BY id DESC`), `get_message` (`:979`), `prune_messages` (`:987`), `message_stats` (`:994`).
- 아웃바운드 포착: `tools/slack_logger.py` — `install_logging()`이 client `chat_postMessage/update/postEphemeral`를 in-place 래핑, `_record()` → `log_message()`. 헬퍼: `_redact_secrets()`(`state/code/token` 값 마스킹), `_infer_category()`, `_recipient_kind()`, `_truncate_blocks()`.
- 미들웨어: `main.py:103` `_install_message_logging` (리스너 주입 client도 래핑, idempotent).
- 인바운드 진입점:
  - `@app.event("app_mention")` → `handle_mention` (`main.py:425`) — 채널 멘션.
  - `@app.event("message")` → `handle_message` (`:513`) — DM + 파일 업로드(`subtype=="file_share"`).
  - `@app.command(...)` → 슬래시 핸들러 ~35개 (`:1718`~`:3104`).
- 관리자: `GET /messages`·`/messages/{id}`·`/users/{uid}/messages` (`server/admin.py:180-210`), `_enrich_messages()`(`:87`). 프론트: `renderMessages`·user 상세 (`frontend/app.js:222,456,571`).

## 3. 설계

### 3.1 데이터 모델 — `direction` 컬럼 추가

`message_log`에 `direction TEXT NOT NULL DEFAULT 'outbound'` 추가.

- `init_db()`에서 `ALTER TABLE message_log ADD COLUMN direction ...` (기존 `users`/`feedback` ALTER와 동일 try 패턴). **기존 행은 전부 `'outbound'`** — 모두 발송 기록이었으므로 정확.
- 인덱스 `idx_msglog_direction` 추가(선택, 필터 성능).

**인바운드 행의 컬럼 의미:**

| 컬럼 | 인바운드 값 |
|---|---|
| `direction` | `'inbound'` |
| `method` | `'message'` \| `'app_mention'` \| `'command'` |
| `recipient_user_id` | **발신 사용자 id** (= 대화의 사람 쪽) |
| `recipient_kind` | `'dm'` \| `'channel'` |
| `channel` | 이벤트/커맨드의 channel |
| `thread_ts` | 있으면 |
| `text` | 사용자 입력. 커맨드면 `"/브리핑 다음주"`, 파일이면 `"[파일 업로드: name (mime)]"`. **`_redact_secrets` 적용** |
| `blocks_json` | `null` |
| `category` | `_infer_category(text, None)` 재사용 (필터 일관) |
| `ok` / `error` | `1` / `null` (인바운드는 발송 상태 없음) |

> `recipient_user_id`는 이름상 "수신자"지만 인바운드에선 **발신자**를 담는다 — 컬럼 의미를 "이 로그가 관계된 사용자"로 확장한다(코드 주석 명시). 리네이밍은 기존 쿼리/인덱스 호환을 위해 하지 않는다. 이 한 컬럼에 양방향을 담기에 per-user 타임라인이 추가 조인 없이 그대로 동작한다.

### 3.2 인바운드 포착 — 미들웨어 단일 지점

`tools/slack_logger.py`에 `record_inbound(body)` 추가하고, `main.py`에 `@app.middleware`(기존 `_install_message_logging` 옆)를 추가해 모든 요청에서 호출한다. **best-effort**(try/except로 감싸고 항상 `next()` 호출 — 로깅이 이벤트 처리를 절대 막지 않음).

핸들러 35개에 일일이 거는 대신 미들웨어 단일 지점을 쓰는 이유: 누락 불가, 신규 핸들러 자동 포착, 기존 아웃바운드 미들웨어와 동일 패턴.

**`record_inbound(body)` 분기:**

- `body.get("event")` 존재 & `type ∈ {message, app_mention}`:
  - `event.get("bot_id")` 있으면 **skip**(봇 메시지).
  - `subtype`: `None`(일반 텍스트)·`"file_share"`만 기록. `message_changed`/`message_deleted` 등은 **skip**.
  - `file_share`: `text = "[파일 업로드: {name} ({mimetype})]"` (files[0] 기준, 복수면 개수 표기).
  - `app_mention`: 멘션 토큰(`<@…>`) 제거한 본문(선택, handle_mention과 동일 정리).
  - `recipient_user_id = event["user"]`, `channel = event["channel"]`, `thread_ts = event.get("thread_ts")`, `method = event["type"]`(`message`/`app_mention`).
- `body.get("command")` 존재(슬래시):
  - `method = 'command'`, `text = f"{body['command']} {body.get('text','')}".strip()`, `recipient_user_id = body["user_id"]`, `channel = body.get("channel_id")`, `recipient_kind` = channel 접두사로 판정.
- `body.get("actions")`(버튼) → **skip**(비목표).
- 그 외 → skip.

중복 방지: 미들웨어는 요청당 1회 실행. 이벤트/커맨드만 기록하므로 핸들러 내부 재라우팅과 무관(인바운드 1요청 = 인바운드 1행).

### 3.3 관리자 뷰 — 대화 타임라인

- `GET /users/{uid}/messages`: `list_messages(user_id=uid)`가 이미 `recipient_user_id=uid`로 필터 → 인바운드+아웃바운드 자연 인터리브. **정렬만 시간 오름차순(대화체)으로** 전환 — 엔드포인트에서 reverse 하거나 `list_messages`에 `order` 파라미터 추가(전역 `/messages` 피드는 기존 `DESC` 유지).
- 응답에 `direction` 포함(`SELECT *` 자동). `_enrich_messages`는 그대로(이름 주입).
- 프론트 user 상세: 행을 **direction별로 구분 표시** — inbound `👤 {이름}`, outbound `🤖 봇` + category·ok/error. 좌우 정렬 또는 라벨+색(채팅 말풍선 느낌).
- (선택) 전역 `/messages`에 direction 필터 칩(`전체 / 질문 / 답변`): `list_messages`에 `direction` 파라미터 + `app.js` 필터 칩 1개 추가.

### 3.4 통계 보정

`message_stats`의 발송 지표(`total`/`failures`/`by_category`)는 **아웃바운드 기준**이어야 정확하다 → 쿼리 조건에 `direction='outbound'` 추가. (선택) `inbound` 카운트 별도 반환. 대시보드 "오늘 발송" 정확성 유지.

### 3.5 프라이버시 · 보안

- 인바운드도 **평문 저장 → 관리자 페이지 평문 노출**. 기존 아웃바운드와 동일 정책(Basic Auth 엄격 관리). `CLAUDE.md` "메시지 관측" 절에 인바운드 추가를 명시.
- **`_redact_secrets`를 인바운드에도 반드시 적용** — 사용자가 토큰/코드를 DM·스레드에 붙이는 사례가 실재(예: Trello 토큰 폴백 붙여넣기, 온톨로지 MCP 토큰). 단, 자유 텍스트의 임의 비밀까지 전부 거르지는 못함 — **잔여 위험을 문서화**한다.
- `prune_messages`는 `ts` 기준 삭제 → 인바운드도 동일 보존정책(`MESSAGE_LOG_RETENTION_DAYS`, 03:00 KST 잡)에 자동 적용.

### 3.6 위험 · 롤아웃

순수 additive 관측. **사용자 체감 동작 변경 0**, best-effort라 로깅 실패가 이벤트 처리를 막지 않음 → **게이팅 불필요, 일반 배포**. 검증: pytest + 배포 후 관리자 타임라인 육안 확인.

## 4. 테스트 (pytest — 기존 mock 패턴: Anthropic/Google/Slack patch, in-memory DB)

- 마이그레이션: `direction` 컬럼 추가 + 기존 행 `'outbound'` 기본값.
- `log_message(direction='inbound')` 저장·조회; per-user 조회가 인바운드+아웃바운드를 시간순 인터리브.
- `record_inbound`: message / app_mention / command body → 올바른 행(direction·method·`recipient_user_id`=발신자·`_redact_secrets` 적용·category).
- `record_inbound` 무시: `bot_id` 있음 / `subtype=message_changed` / `actions`(버튼) → 행 없음.
- `record_inbound` best-effort: 깨진/빈 body → 예외 안 남.
- `message_stats`: 발송 지표가 outbound만 카운트(인바운드 섞여도 불변).
- `GET /users/{uid}/messages`: 응답에 `direction` 포함 + 시간 오름차순.

## 5. 파일 변경 요약

| 파일 | 변경 |
|---|---|
| `store/user_store.py` | `message_log` DDL/ALTER에 `direction`; `log_message`에 `direction` 인자(기본 `'outbound'`); `list_messages`에 `direction` 필터 + `order` 옵션; `message_stats` outbound 한정 |
| `tools/slack_logger.py` | `record_inbound(body)` 추가(분기·redact·best-effort) |
| `main.py` | `@app.middleware`로 `record_inbound` 호출 |
| `server/admin.py` | `/users/{uid}/messages` 시간 오름차순; (선택) `/messages` `direction` 필터 |
| `frontend/app.js`, `style.css` | user 상세 대화 타임라인(direction 구분); (선택) 필터 칩 |
| `CLAUDE.md` | "메시지 관측" 절에 인바운드 캡처 반영 |
| `tests/` | §4 테스트 |
