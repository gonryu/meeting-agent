# 관리자 메시지 관측(Observability) 설계

- **작성일**: 2026-06-20
- **상태**: 설계 확정 대기 (사용자 리뷰 전)
- **브랜치**: `feature/admin-message-observability`

## 1. 목적

관리자가 **봇이 실제로 어떤 메시지를 누구에게 보냈는지 사후에 확인**할 수 있게 한다. 1차 목적은 **운영·품질 점검**이다 — 아침 브리핑이 어떻게 나갔는지, 특정 사용자에게 무엇이 전달됐는지, 발송이 실패하진 않았는지를 관리자 페이지에서 들여다본다.

부차적으로 "사용 현황"(누가 얼마나 쓰는지, 어떤 기능이 많이 나가는지)을 대시보드 집계로 함께 제공한다.

## 2. 배경 — 현재 상태

검수(2026-06-20)에서 확인한 사실:

- 발송된 Slack 메시지를 **어디에도 저장하지 않는다.** 코드 전체에 메시지 로그/audit 테이블이 0건이며, 218개 발송 지점(`chat_postMessage` 201 / `chat_update` 16 / `chat_postEphemeral` 1)이 Slack으로 전송 후 아무 기록도 남기지 않는다.
- 이미 저장돼 조회 가능한 것: `users`, `meeting_index`, `action_items`, `feedback`, `todos` 등. 관리자 페이지(`/admin/`)에 대시보드·사용자·피드백·프롬프트 편집이 이미 존재.
- 모든 모듈이 동일한 `app.client`(slack_sdk `WebClient`) 인스턴스를 `slack_client` 인자로 전달받아 사용한다 (`main.py:96` `app = App(...)`). 모듈별 `_post` 헬퍼(before/during/card/room/dreamplus)가 있으나 직접 `chat_postMessage` 호출도 많다.

**결론**: 메시지 이력은 "지금부터 쌓는" 형태가 된다. 과거분은 (Drive에 저장된 회의록 외엔) 소급 불가.

## 3. 확정된 결정사항

| 항목 | 결정 |
|---|---|
| 1차 목적 | 운영·품질 점검 (메시지 원문 열람 중심) |
| 기록 범위 | **전부** — 모든 발송을 중앙에서 가로채 기록 |
| 조회 방식 | **둘 다** — 글로벌 피드(필터) + 사용자별 상세 |
| 포착 메커니즘 | WebClient 프록시 (접근법 A) |

## 4. 아키텍처 / 접근

### 4.1 포착 메커니즘 — WebClient 프록시

시작 시 `app.client`를 얇은 프록시로 감싸 `chat_postMessage` / `chat_update` / `chat_postEphemeral` 세 메서드만 가로챈다. 218개 호출부는 **무수정** — 모두 동일 인스턴스를 공유하므로 한 곳만 감싸면 전부 포착된다.

**대안 검토:**
- B. 메서드 몽키패치(`client.chat_postMessage = wrapped`) — 효과 동일하나 덜 명시적, 테스트 까다로움. 탈락.
- C. `_post` 헬퍼/Bolt 미들웨어 — 헬퍼 경유 발송만 잡혀 직접 호출 200건 누락. 탈락.

### 4.2 안전 원칙 (필수)

**로깅 실패가 실제 발송을 절대 막거나 지연시키지 않는다.**
- 로깅 로직 전체를 `try/except`로 감싸고, 예외는 `log.warning`으로만 남긴다.
- 발송 메서드의 반환값/예외는 원본 그대로 전파한다(프록시는 결과를 관찰만 한다).
- DB 쓰기는 동기로 하되 방어적으로 — 현재 규모에서 SQLite 단건 INSERT는 충분히 빠르다. 큐/비동기는 YAGNI.

## 5. 데이터 모델

`store/user_store.py`의 `init_db()`에 테이블 추가 (feedback/todos와 동일 패턴).

```sql
CREATE TABLE IF NOT EXISTS message_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,          -- ISO8601, KST
    method          TEXT NOT NULL,          -- post | update | ephemeral
    channel         TEXT,                   -- 발송 시 넘긴 channel (U… / D… / C…)
    recipient_user_id TEXT,                 -- DM이면 U…, 채널이면 NULL
    recipient_kind  TEXT,                   -- dm | channel
    thread_ts       TEXT,
    text            TEXT,
    blocks_json     TEXT,                   -- JSON 직렬화, ~20KB 상한
    category        TEXT,                   -- 휴리스틱 분류 (아래)
    ok              INTEGER NOT NULL,        -- 1 | 0
    error           TEXT                    -- Slack 에러 문자열 (실패 시)
);
CREATE INDEX IF NOT EXISTS idx_msglog_ts        ON message_log(ts);
CREATE INDEX IF NOT EXISTS idx_msglog_recipient ON message_log(recipient_user_id);
CREATE INDEX IF NOT EXISTS idx_msglog_category  ON message_log(category);
CREATE INDEX IF NOT EXISTS idx_msglog_ok        ON message_log(ok);
```

**수신자 이름은 저장하지 않는다.** 조회 시점에 기존 `admin.py::_lookup_profile` 캐시로 해석 → 발송 핫패스에 Slack API 호출 0 추가. 채널(C…) 이름도 조회 시점에 해석(캐시).

**`recipient_kind` 판정**: `channel`이 `U`로 시작 → `dm`(+ `recipient_user_id=channel`), `D`로 시작 → `dm`(채널 ID만 있는 DM, user_id 미상 → NULL 허용), `C`로 시작 → `channel`.

**`category` 휴리스틱** (best-effort, blocks/text 마커 기반):
`briefing | minutes | action_item | meeting_alarm | room | proposal | feedback | other`
→ 추정 불가 시 `other`. "아침 브리핑만 보기" 같은 필터를 가능케 한다. 정확도는 보조 수준이며, 실패해도 기능에 지장 없음.

**저장 함수** (user_store.py): `log_message(...)`, `list_messages(*, user_id=None, category=None, ok=None, date_from=None, date_to=None, q=None, limit, offset)`, `get_message(id)`, `prune_messages(before_date)`, `message_stats(date_from, date_to)`.

## 6. 포착 모듈 — `tools/slack_logger.py` (신규)

격리된 단위. 외부 의존: `store.user_store.log_message`만.

```python
def wrap_client(client):
    """WebClient를 감싼 프록시 반환. send 3종을 가로채 로깅 후 원본 위임."""
```

- `main.py`에서 `app = App(...)` 직후 한 줄로 교체. **권장 방식**: slack_bolt의 `App.client`는 `self._client`를 반환하므로 `app._client = wrap_client(app.client)`로 치환하면 이후 모든 `app.client` 접근·Bolt 리스너 주입·스케줄러 잡이 프록시를 공유한다. (Bolt 리스너 인자 주입이 `self._client`를 쓰는지 구현 단계에서 1회 확인 후 확정.)
- 각 send: 원본 호출 → `try` 성공 시 `ok=True`, `SlackApiError` 시 `ok=False, error=e.response["error"]` 로깅 후 **예외 재전파**.
- `category` 추정·`recipient_kind` 판정은 이 모듈의 순수 함수로 분리(단위 테스트 용이).

## 7. 관리자 API (`server/admin.py` 확장, Basic Auth 그대로)

- `GET /admin/api/messages` — 쿼리: `user, category, ok, date_from, date_to, q(본문 검색), limit, offset`. 페이지네이션 피드 반환(수신자명 주입).
- `GET /admin/api/messages/{id}` — 원문 전체(text + blocks_json 파싱).
- `GET /admin/api/users/{uid}/messages` — 사용자별(내부적으로 `list_messages(user_id=uid)` 재사용).
- `GET /admin/api/dashboard` 확장 — 기존 counts에 `messages_today`, `failures_today`, `active_users_today`, `by_category` 추가.

## 8. 프론트엔드 (`frontend/`, 빌드 도구 없음 — 기존 패턴 재사용)

기존 해시 라우팅·`escapeHtml`·`sessionStorage` Basic Auth 그대로 사용.

- **새 탭 `#/messages`**: 표(시각 · 수신자명 · 유형(category) · ✓/✗ · 본문 미리보기) + 상단 필터바(사용자/카테고리/성공여부/날짜/본문검색). 행 클릭 → 드로어(또는 모달)에 원문 전체(text + 렌더된 블록 또는 raw JSON). 페이지네이션.
- **사용자 상세 `#/users/{uid}`**: 기존 사용자 목록 행 클릭 → 프로필 + 연결상태 + 그 사용자가 받은 메시지(피드 컴포넌트를 `user` 필터로 재사용).
- **대시보드**: stats 스트립 추가(오늘 발송수 / 실패수 / 활성 사용자수 / 카테고리별 막대 또는 숫자).

## 9. 보존 · 프라이버시

- DM 본문 전체가 누적되므로 민감 정보 포함 가능. 접근은 **Basic Auth 관리자 전용**으로 제한(기존과 동일).
- **기본 90일 보존** — `MESSAGE_LOG_RETENTION_DAYS` env(기본 90). APScheduler에 **일일 prune 작업 1개 추가**(기존 7잡 → 8잡). `prune_messages(now - N일)`.
- `blocks_json`은 ~20KB 상한으로 절단(초과 시 표시용 잘림 마커).

## 10. 테스트

기존 테스트 패턴 준수: `anthropic.Anthropic`·Google API mock, 환경변수 `os.environ.setdefault`. **단, Fernet 키는 유효한 32바이트 키 사용**(검수에서 발견한 30바이트 픽스처 버그 답습 금지).

- **래퍼**(`tools/slack_logger.py`): ① 성공 발송 시 `ok=1` 로깅 + 원본 응답 그대로 반환 ② `SlackApiError` 시 `ok=0, error=...` 로깅 + 예외 재전파 ③ **로깅 내부 예외가 발송 결과를 바꾸지 않음**(log_message가 던져도 send는 정상) ④ `category`/`recipient_kind` 순수 함수 경계값.
- **스토어**: `log_message` → `list_messages` 필터(user/category/ok/date/q) 조합, `get_message`, `prune_messages` 경계.
- **API**: 인증(401/503), 필터·페이지네이션, `messages/{id}` 404, 대시보드 stats.

## 11. 범위 외 (v1 제외 · 추후)

- 발송 **실패 시 관리자 채널 자동 알림** — v1은 "실패 필터 + 대시보드 카운트"로 수동 점검. 알림은 추후.
- 수신 메시지(사용자→봇) 로깅 — 현재 목적(발송 점검) 밖.
- 메시지 내용 기반 분석/LLM 요약 — 추후.
- 과거 메시지 소급 복원 — 불가(설계 전제).

## 12. 가정 · 미해결

- `category` 휴리스틱의 정확도는 보조 수준으로 충분하다고 가정(정밀 분류가 필요해지면 호출부에서 명시 태그를 contextvar로 주입하는 방식으로 확장 가능 — v1 범위 외).
- 단일 인스턴스 운영 전제(동시성 락 불필요). 현 운영 구조와 일치.
- 프록시 치환의 정확한 지점(`app._client` vs 전역 `slack_client` 변수)은 `main.py`의 client 전달 흐름을 구현 단계에서 한 번 더 확인해 확정한다.
