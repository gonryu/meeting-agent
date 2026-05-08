# 핸드오프 — 스레드 @멘션으로 부모 메시지를 Trello에 등록

다른 Slack 봇에 동일 기능을 이식하려는 엔지니어용 가이드. ParaMee 봇의 PR #30, #31 (commit `b7b9139`, `653dd17`) 구현을 베이스로 합니다.

## 1. 기능 정의

**시나리오**
- A가 채널에 회의록 텍스트를 올림
- B가 그 메시지에 *스레드 답글*로 `@봇 위 회의록 트렐로에 등록해줘` 호출
- 봇이 부모 메시지 본문을 읽어 액션아이템 추출 → 업체 추론 → Trello 카드에 체크리스트 등록 제안 (스레드 답글 형태로 카드 선택 UI 발송)
- B가 카드 선택 → 같은 스레드에서 등록 완료 회신

**왜 굳이 만드는가**
- 회의록은 보통 미팅이 끝나고 한참 뒤에, 봇 세션 없이 채널에 자유 형식으로 적힘
- 봇이 자동 회의록 생성 워크플로에 합류 못한 케이스 (외부 회의·구두 회의·다른 도구로 작성한 노트 등)도 액션아이템 추적 가능
- "위 회의록" 자연스러운 한국어 표현으로 트리거

## 2. Slack 앱 설정 (가장 흔히 빠뜨리는 부분)

**필수 OAuth Bot Scopes**
- `app_mentions:read` — @멘션 이벤트 수신
- `channels:history` — *공개* 채널의 부모 메시지 본문 읽기 ← **빠뜨리면 missing_scope**
- `groups:history` — *비공개* 채널의 부모 메시지 본문 읽기 ← 동일
- `chat:write` — 카드 선택 UI·결과 메시지 발송
- `im:write` (선택) — DM fallback 시 필요

**필수 Bot Events**
- `app_mention`

**적용 절차**
1. api.slack.com/apps → 앱 선택 → App Manifest 또는 OAuth & Permissions
2. 위 스코프 추가 후 *Save Changes*
3. **반드시** *Reinstall to Workspace* 클릭 → 새 권한 동의
4. 봇 토큰이 갱신되면 `.env`/Secret Manager 업데이트 + 재시작

> 매니페스트는 *덮어쓰기* 방식이라 누락 항목은 삭제됨. 기존 스코프·커맨드 모두 포함한 통합 매니페스트로 한 번에 적용 권장. 본 레포 [`slack-manifest.yml`](../../slack-manifest.yml) 참고.

## 3. 구현 패턴 (5단계)

### 3-1. `app_mention` 핸들러에서 부모 메시지 본문 가져오기

스레드 답글이면 `event.thread_ts`가 부모 메시지 ts. `conversations.replies` API로 본문 조회. ParaMee 구현: [main.py:414-440](../../main.py#L414-L440)

```python
parent_text = ""
parent_fetch_error = ""
parent_ts = event.get("thread_ts")
if parent_ts:
    try:
        resp = client.conversations_replies(
            channel=channel, ts=parent_ts, limit=1, inclusive=True
        )
        msgs = resp.get("messages", [])
        if msgs:
            parent_text = (msgs[0].get("text") or "").strip()
            # text 비어있으면 blocks 트리에서 평탄화 추출 (rich text 메시지 대응)
            if not parent_text:
                parent_text = _extract_text_from_blocks(msgs[0].get("blocks") or [])
    except SlackApiError as e:
        parent_fetch_error = e.response.get("error", "")
```

**핵심 함정 3가지**
1. `text` 필드가 비어있고 `blocks` 트리에만 컨텐츠가 있는 *rich text* 메시지가 흔함 → 헬퍼로 평탄화. 헬퍼 구현: [main.py:368-393](../../main.py#L368-L393)
2. 봇이 채널 멤버가 아니면 `not_in_channel` 에러 → 사용자에게 채널 초대 안내
3. 스코프 누락 시 `missing_scope` → 관리자 안내

### 3-2. 인텐트 분류기에 신규 인텐트 추가

기존 자연어 라우터(LLM 기반이든 키워드 기반이든)에 *등록·올리기·만들기* 의도를 새 인텐트로 분리. ParaMee의 LLM 프롬프트 변경: [main.py:715-717](../../main.py#L715-L717)

```
- trello_register_from_thread: 스레드 답장에서 부모 메시지를 Trello 카드에 등록
  요청 (예: "위 회의록 트렐로에 등록해줘", "이 회의록 트렐로 등록", "트렐로 카드로 만들어줘")
  * 주의: "조회/검색"이 아니라 *등록·올리기·만들기* 의도일 때만
  * params: { "company": "업체명 힌트(없으면 빈 문자열)" }
```

`trello_search`(조회)와 `trello_register_from_thread`(등록)를 *명확히 구분*해야 LLM이 헷갈리지 않음.

### 3-3. 라우터에 핸들러 연결 + 빈 본문 가드

부모 본문이 비면 사용자에게 *원인별* 메시지 노출 (UX 큰 차이). ParaMee 구현: [main.py:1419-1448](../../main.py#L1419-L1448)

원인별 분기:
- `parent_fetch_error == "not_in_channel"` → 봇 채널 초대 안내
- `parent_fetch_error == "missing_scope"` → 관리자 문의 안내
- 기타 에러 코드 → 그 코드 그대로 노출 (디버깅용)
- 스레드 답글이 아님 → 답글로 호출 안내
- 본문이 진짜 비어있음 → 본문 있는 메시지에 답글 요청

### 3-4. 액션아이템 추출 + 합성 event_id로 DB 저장

회의록 자동 생성 경로의 추출기(`extract_and_enrich`)를 *재사용*. 핵심 변경: 캘린더 `event_id`가 없으니 `thread_{channel}_{thread_ts}` 같은 합성 ID 발급 → 기존 액션아이템 테이블 그대로 사용 → 후속 핸들러는 변경 없이 호환됨. ParaMee 구현: [agents/after.py:709-815](../../agents/after.py#L709-L815) (commit b7b9139)

업체명 추론 fallback 체인:
1. 사용자 메시지에 업체명 힌트가 있으면 그것 사용
2. 부모 메시지 첫 줄을 LLM에 넣어 추론 (`_infer_company_from_title`)
3. 둘 다 실패 시 사용자에게 업체명 명시 요청

### 3-5. 카드 선택 UI를 *호출 위치에 회신* (핵심 UX 결정)

기존 자동 등록 플로우는 DM에 카드 선택 UI를 띄우지만, 이 진입점은 채널 스레드에서 호출됐으니 *같은 스레드*에 회신해야 자연스러움. 다른 사용자도 결과를 볼 수 있어 협업 가시성 ↑.

구현 패턴: 기존 `_propose_trello_registration`·`_register_to_card` 등에 `channel`/`thread_ts` *optional* 파라미터 추가 + 버튼 페이로드 JSON에도 동봉 → 후속 클릭 시 같은 위치에 회신. 미지정 시 기존 DM 동작 유지 (하위 호환). ParaMee 구현: [agents/after.py:817-1106](../../agents/after.py#L817-L1106)

> 모든 후속 버튼 핸들러(`select_card`, `new_card`, `confirm_new_card`, `cancel_new_card`, `skip`, `register_to_card`)에 일관되게 적용해야 사용자가 어디서 클릭하든 같은 스레드에서 결과 받음.

## 4. 어떤 것은 그대로 가져오면 안 되는가

다른 봇에 이식할 때 *적응*이 필요한 부분:

- **인텐트 분류기 형태** — ParaMee는 Claude Haiku LLM 분류. 키워드 매칭·규칙 기반이면 트리거 단어 명시적으로 (`등록`, `올려`, `카드로`) 추가
- **액션아이템 추출 파이프라인** — ParaMee는 3단계 오케스트레이터. 다른 봇이라면 단일 LLM 호출로 폴백 가능
- **업체 추론 함수** — 도메인 특화. 다른 봇은 "프로젝트", "고객사" 등 다른 개념일 수 있음
- **Trello 카드 매핑 정책** — 우리는 "업체 1개 = 카드 1개", 카드명 = 업체명. 보드 구조가 다르면 매핑 룰 재정의 필요
- **DB 스키마** — `action_items` 테이블이 `event_id` 키로 동작. 합성 ID 방식이 그대로 가능한지 확인

## 5. 테스트 시나리오

1. **정상 경로** — 다른 사람이 채널에 회의록 게시 → 답글로 봇 호출 → 카드 선택 UI가 *스레드*에 뜸 → 카드 선택 → 등록 완료 메시지가 *같은 스레드*에 회신
2. **봇이 채널 멤버 아님** → `not_in_channel` 안내가 뜸 → 채널 초대 후 재시도
3. **스코프 누락** → `missing_scope` 안내 → 관리자가 매니페스트 적용 + Reinstall → 재시도
4. **rich text 메시지** (목록·헤더 등 블록 포맷) → blocks 평탄화로 정상 처리
5. **답글 아닌 일반 메시지로 호출** → "답글로 호출해주세요" 안내
6. **부모 메시지 본문 비어있음** (이미지·파일만 있는 메시지) → 본문 있는 메시지 요청
7. **Trello 미연동 사용자 호출** → 연동 링크 DM/스레드 안내
8. **기존 자동 등록 경로(미팅 종료 → DM)** → 변경 없이 그대로 동작 (회귀 테스트)

## 6. 함정 & 교훈

| 증상 | 원인 | 대응 |
|------|------|------|
| `명령을 정확히 이해하지 못했어요` | 인텐트 분류기에 신규 인텐트 미추가 | 분류 프롬프트·룰에 추가 |
| `등록할 회의록 텍스트를 찾지 못했어요` | `parent_text`가 빈 문자열 | 원인별 진단 메시지로 분기 (3-3 참고) |
| `missing_scope` (가장 흔함) | Slack 매니페스트 저장만 하고 *Reinstall* 안 누름 | Reinstall 필수, 새 토큰 반영 |
| 카드 선택 UI가 DM에만 뜸 | 후속 핸들러에 channel/thread_ts 전파 안 됨 | 모든 핸들러 + 버튼 페이로드에 동봉 |
| 부모 메시지 텍스트 비어있음 | rich text 메시지 (text 필드 빈 채로 blocks만 있음) | blocks 트리 평탄화 헬퍼 추가 |

## 7. 참고 PR

- [#30](https://github.com/gonryu/meeting-agent/pull/30) — feat: 스레드에서 회의록 텍스트 Trello 등록 (메인 구현)
- [#31](https://github.com/gonryu/meeting-agent/pull/31) — fix: 부모 메시지 조회 진단 로그·에러 안내 강화 (운영 중 발견된 missing_scope 케이스 대응)
