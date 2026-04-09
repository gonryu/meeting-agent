# Meeting Agent — 시스템 요구사항 문서

> 최종 갱신: 2026-04-02 (업체명 자동추출 제거, 통합 스레드 업데이트, 회의록 Claude Sonnet, 브리핑 unfurl 비활성화)
> 대상: ICONLOOP / Parametacorp 내부 사용
> 목적: Slack 기반 AI 미팅 어시스턴트 시스템

**구현 상태 표기**

- ✅ 구현 완료
- ⚠️ 부분 구현
- ❌ 미구현 (계획)

---

## 1. 개요

Meeting Agent는 Slack을 인터페이스로 사용하는 AI 미팅 어시스턴트입니다.
미팅의 전(Before) · 중(During) · 후(After) 전 단계를 자동화합니다.

```
Before Agent   →   During Agent   →   After Agent
(미팅 준비)        (미팅 중 지원)       (미팅 사후 관리)
브리핑 생성         회의록 작성          요약 발송
업체/인물 리서치    액션아이템 추적       Contacts 갱신
미팅 생성          실시간 메모           후속 일정 관리
```

**현재 구현 상태**: Before Agent ✅ 완료 / During Agent ✅ 완료 / After Agent ✅ 완료

> 최종 갱신: 2026-03-25

---

## 2. 사용자 등록

### 2.1 등록 플로우 ✅

1. Slack에서 `/register` 또는 `/등록` 실행
2. Google OAuth 인증 링크 DM 수신
3. Google 계정 로그인 및 권한 동의
4. OAuth 콜백 → 토큰 암호화 저장 (Fernet + SQLite)
5. Google Drive 전용 폴더 구조 자동 생성
6. 등록 완료 DM 수신

### 2.2 재인증 ✅

- `/재등록` 또는 `/reregister` 실행 → 이미 등록된 사용자도 OAuth 재실행
- 스코프 추가 등 권한 변경 시 활용
- Slack retry로 인한 세션 충돌 방지: state에 uuid 포함하여 각 인증 요청 독립 관리

### 2.3 Google 권한 범위 ✅


| Scope                | 용도                                        | 적용 범위 |
| -------------------- | ----------------------------------------- | ------- |
| `calendar`           | 미팅 조회, 생성, 설명 업데이트                        | 전체 사용자 |
| `drive`              | Contacts, 회의록, company_knowledge.md 읽기/쓰기 | 전체 사용자 |
| `gmail.readonly`     | 이메일 맥락 검색, 이메일 주소 추출                      | 전체 사용자 |
| `gmail.send`         | 외부용 회의록 이메일 발송 (After Agent)              | 전체 사용자 |
| `documents.readonly` | Google Meet 트랜스크립트(Google Docs) 읽기        | 전체 사용자 |
| `contacts.readonly`  | Google 주소록에서 참석자 이메일 조회 (People API)      | 신규 등록자부터 적용 — 기존 사용자는 `/재등록` 필요 |

> ⚠️ 스코프 변경 시 `server/oauth.py`와 `store/user_store.py` **두 파일 모두** 업데이트 필요.
> `server/oauth.py`가 실제 Google 동의 화면에서 요청할 스코프를 결정하므로 이 파일이 핵심.
> `contacts.readonly`는 기존 토큰 갱신 호환성을 위해 `store/user_store.py` SCOPES에는 **의도적으로 제외**됨.


### 2.4 보안 ✅

- OAuth 토큰 Fernet 대칭키 암호화 후 SQLite 저장
- 암호화 키는 환경변수 `ENCRYPTION_KEY` 관리
- 사용자 토큰 완전 격리

### 2.5 OAuth 토큰 자동 갱신 ❌

- 현재: 토큰 만료 시 `/재등록` 으로 재인증 필요
- 계획: Credentials refresh_token 자동 갱신 처리

### 2.6 사용자별 설정 ❌

- 계획: 자동 브리핑 시간 커스터마이징 (`/설정 브리핑시간 8시`)

---

## 3. Slack 인터페이스

### 3.1 슬래시 커맨드


| 커맨드                    | 기능                                  | 상태  |
| ---------------------- | ----------------------------------- | --- |
| `/register` / `/등록`    | Google 계정 연동                        | ✅   |
| `/재등록` / `/reregister` | Google 계정 재인증 (스코프 갱신)              | ✅   |
| `/brief` / `/브리핑`      | 오늘 미팅 브리핑 수동 실행                     | ✅   |
| `/update` / `/업데이트`    | company_knowledge.md 갱신             | ✅   |
| `/meet` / `/미팅추가`      | 자연어로 미팅 생성                          | ✅   |
| `/company` / `/기업`     | 기업정보 강제 리서치 및 저장                    | ✅   |
| `/person` / `/인물`      | 인물정보 강제 리서치 및 저장                    | ✅   |
| `/미팅시작`                | 수동 노트 세션 시작                         | ✅   |
| `/메모`                  | 미팅 중 노트 추가                          | ✅   |
| `/미팅종료`                | 세션 종료, 즉시 트랜스크립트 탐색(백그라운드) + 회의록 생성 | ✅   |
| `/회의록`                 | 저장된 회의록 목록 조회                       | ✅   |
| `/도움말` / `/help`       | 사용 가능한 커맨드 및 자연어 명령어 안내              | ✅   |
| `/드림플러스` / `/dreamplus` | 드림플러스 계정 등록/변경                   | ✅   |
| `/설정`                  | 사용자별 설정 변경                          | ❌   |


### 3.2 자연어 커맨드 (DM 또는 @멘션) ✅

**LLM 인텐트 분류 방식** (`_classify_intent` in `main.py`)

슬래시 커맨드 없이 자연어 메시지를 LLM(`generate_text()`)으로 아래 9개 인텐트 중 하나로 분류합니다:

| 인텐트 | 예시 메시지 |
|--------|------------|
| `briefing` | "브리핑 해줘", "오늘 미팅 알려줘" |
| `create_meeting` | "내일 오전 10시에 회의 잡아줘" |
| `start_session` | "미팅 시작", "KISA 미팅 시작할게" |
| `add_note` | "지금 논의 내용 메모해줘: ..." |
| `end_session` | "미팅 끝났어", "회의 종료" |
| `get_minutes` | "회의록 보여줘", "지난 회의록 목록" |
| `research_company` | "신한캐피탈 리서치해줘" |
| `research_person` | "김민환 인물 조회" |
| `update_knowledge` | "회사 지식 업데이트" |
| `help` | "도움말", "help", "사용법 알려줘" |

분류 실패 시 `/도움말` 안내 메시지 표시. 인텐트별 파라미터(title, note, company 등)도 함께 추출합니다.

**@멘션 지원** ✅

- 채널에서 `@봇이름 명령어` 형식으로 사용
- 응답은 해당 채널 스레드로 게시

### 3.3 파일 업로드 @멘션 ⚠️

**명함 업로드 → 인물 등록 ✅**

- DM에 명함 이미지(JPG, PNG 등) 업로드 → `agents/card.py`가 처리
- `subtype == "file_share"` + `channel_type == "im"` + 이미지 MIME 타입 조건 감지
- Claude Haiku Vision(`claude-haiku-4-5`)으로 OCR → 구조화된 인물 정보 추출
  - 추출 필드: name, company, title, department, phone, mobile, fax, email, address, website, sns
- Block Kit UI 발송: ✅저장 / ✏️수정 / ❌취소 버튼
- 저장 확인 시 `research_person(card_data=card_data)` 호출 → `People/{이름}.md` 자동 생성/갱신
- 수정 클릭 시 OCR 결과가 미리 채워진 편집 모달 표시 (값이 있는 항목만 `initial_value` 설정)

**녹음 파일 업로드 → STT → 메모/회의록 ✅**

- DM에 오디오 파일(MP3, MP4, M4A, WAV, OGG, WebM, AAC 등) 업로드
- **Deepgram API** (`nova-2` 모델, 한국어) 로 STT 변환 → 세션에 메모로 등록
- 진행 중인 세션이 없으면 "음성 메모 세션"을 자동 시작 후 등록
- Google Meet 트랜스크립트가 없는 오프라인 미팅, 대면 미팅에 활용
- 환경변수: `DEEPGRAM_API_KEY` / 사내 방화벽 대응: SSL 검증 비활성화(`verify=False`)

**텍스트 문서 업로드 → 회의록 생성**

- `@봇이름` 멘션과 함께 텍스트 문서(TXT, DOCX, PDF 등) 업로드
- 가장 최근에 시작했거나 종료한 캘린더 일정과 자동 매칭하여 해당 미팅의 노트/메모로 간주
- 문서 내용을 LLM으로 분석하여 내부용·외부용 회의록 생성 후 Drive 저장 + Slack 발송
- 회의 중 별도 문서 편집기에 작성한 노트, 수기 입력 텍스트 등 다양한 형식 지원
- 계획: Slack `file_shared` 이벤트 감지 → MIME 타입 판별 → 텍스트 추출 → 회의록 생성 흐름 실행

**이미지 업로드 → 명함 또는 손글씨 회의록**

- `@봇이름` 멘션과 함께 이미지 파일(JPG, PNG 등) 업로드
- LLM 비전 기능으로 이미지 내용을 분석하여 **명함인지 손글씨 노트인지 자동 판별**
  - **명함으로 판별 시**: 이름·소속·직책·이메일·전화번호 등 추출 → `People/{이름}.md` 자동 생성/갱신
  - **손글씨 노트로 판별 시**: 텍스트 인식(OCR) 후 내용을 분석하여 최근 캘린더 일정과 매칭, 내부용·외부용 회의록 생성
  - **판별 불가 시**: 사용자에게 명함인지 회의 노트인지 확인 메시지 발송
- 계획: Slack `file_shared` 이벤트 감지 → 이미지 유형 판별 → 분기 처리

---

## 4. Before Agent — 미팅 준비

### 4.1 브리핑 실행 방식


| 방식          | 조건                         | 상태  |
| ----------- | -------------------------- | --- |
| 자동 브리핑      | 매일 09:00 KST (APScheduler) | ✅   |
| 수동 브리핑      | `/brief` 또는 자연어 요청         | ✅   |
| 미팅 생성 후 자동  | 미팅 생성 완료 직후                | ❌ 제거됨 |
| 사용자별 브리핑 시간 | 사용자 지정 시간                  | ❌   |

**브리핑 인트로 메시지**: `event=None` 전체 브리핑 시 목록 출력 전 먼저 발송:
`"📅 {display_name}님의 향후 24시간 일정을 보여드리겠습니다."`

**2단계 비동기 발송** (2026-04-02): 리서치 대기 없이 미팅 정보를 즉시 발송하고, 업체 리서치 결과를 백그라운드에서 순차적으로 발송.
- 1단계(즉시): 미팅 헤더(제목·시간·링크·어젠다) 발송
- 2단계(백그라운드): 업체 리서치 → 인물 리서치 → 이전 맥락 순서로 완료 시마다 발송
- 다중 업체 브리핑 시 단일 스레드에서 순차 처리 → 결과 섞임 없음


### 4.2 외부 미팅 판단 기준 ✅

1. 참석자 이메일 도메인이 내부 도메인 이외인 경우 (단, 공개 이메일 서비스 제외)
2. 미팅 제목에 Contacts 등록 업체명 포함된 경우
3. LLM으로 제목에서 업체명 추출 시도

내부 도메인: `parametacorp.com`, `iconloop.com` (환경변수 `INTERNAL_DOMAINS`로 설정)

공개 이메일 서비스 도메인 (`_PUBLIC_EMAIL_DOMAINS`): gmail.com, naver.com, daum.net, hanmail.net, yahoo.com, hotmail.com, outlook.com, icloud.com, nate.com 등 — 업체명 추출 대상에서 제외

**업체명 결정 우선순위**: 외부 도메인이 감지되어도 미팅 제목에 Contacts 업체명이 있으면 **한국어 정식명 우선 반환**. Drive 회의록 파일명 및 Gmail 검색과의 일치를 위해 영문 도메인 기반 이름보다 정식명을 우선함.

### 4.3 외부 미팅 브리핑 구성 ✅

**2단계 비동기 발송** 방식으로 블록을 나눠 순차 발송합니다.

**Block 1 — 즉시 발송** (`build_meeting_header_block`)

| 항목 | 내용 | 출처 |
| ---- | ---- | ---- |
| 미팅 헤더 | 제목, 시간, Google Meet 링크, 장소 | Google Calendar |
| 관련 업체 | 🏢 별도 필드로 표시 (extendedProperties에서 가져옴) | Google Calendar |
| 참석자 | 👥 이름 목록 (displayName → Slack → 주소록 → 이메일 폴백) | Google Calendar + Slack |
| 어젠다 | 📝 Calendar 이벤트 description 표시, 내용은 다음 줄부터 출력 (없으면 스레드 답글 안내) | Google Calendar |

**Block 2 — 백그라운드 (업체 리서치)** (`build_company_research_block`)

| 항목 | 내용 | 출처 |
| ---- | ---- | ---- |
| ParaScope 브리핑 | ParaScope 리서치 결과 | Drive `{company}.md` → ParaScope 섹션 |
| 업체 최근 동향 📰 | 최근 동향 최대 3줄 (URL 텍스트 링크화, 프리앰블 제거) | Gemini 웹 검색 |
| 서비스 연결점 | 우리 서비스와 상대 업체의 시너지 2~3줄 | Gemini + company_knowledge.md |

**Block 3 — 백그라운드 (담당자)** (`build_persons_block`)

| 항목 | 내용 | 출처 |
| ---- | ---- | ---- |
| 담당자 정보 👤 | 소속, 직책, 이메일, 공개 정보 | Gmail + Gemini 웹 검색 |

**Block 4 — 백그라운드 (맥락)** (`build_context_block`)

| 항목 | 내용 | 출처 |
| ---- | ---- | ---- |
| 이전 미팅 맥락 📌 | Drive 회의록 최대 3개 (Drive 열기 링크 포함) | Drive Minutes 폴더 |
| 이메일 맥락 📧 | Gmail 최근 이메일 최대 1개 (별도 섹션) | Gmail |

**브리핑 시간 범위**: 현재 시각 기준 24시간 이내 미팅만 포함. 이미 시작된 과거 미팅 제외.
**종일 이벤트 제외**: 제목이 `집` 또는 `사무실`인 종일 이벤트는 브리핑 대상에서 제외.
**시간 표시 형식** (`format_time()` in `tools/slack_tools.py`):
- 오늘 이벤트: `오후 3:00`
- 오늘이 아닌 이벤트: `3/31(화) 오후 3:00` (날짜 + 요일 포함)


### 4.4 내부 미팅 브리핑 ✅

- 제목, 시간, 참석자 목록 표시
- Calendar 어젠다 있으면 표시, 없으면 스레드 답장 안내

### 4.5 어젠다 등록 ✅

- 브리핑 메시지 스레드에 답장 → 자동 감지
- Google Calendar 이벤트 설명란 자동 저장
- 이미 어젠다가 등록된 경우 브리핑에 자동 표시 (FR-B16 부분 구현)

### 4.6 브리핑 채널 공유 ❌

- 원본 요구사항(FR-B12): 특정 Slack 채널에도 브리핑 메시지 공유
- 현재: DM으로만 발송
- 계획: 사용자 설정으로 지정 채널에 브리핑 동시 공유

### 4.7 Calendar 이벤트 어젠다 브리핑 표시 ✅ (FR-B16)

- Calendar 이벤트 `description` 필드에 내용이 있으면 브리핑 메시지에 어젠다 섹션으로 표시
- 없을 경우에만 스레드 답장 등록 안내 표시

### 4.8 미팅 템플릿 ❌

- 원본 요구사항(FR-B14): 미팅 유형별 어젠다 템플릿 제공 (영업 미팅, 내부 리뷰, 고객사 온보딩 등)
- 계획: `/미팅추가 영업` 등 유형 지정 시 해당 어젠다 템플릿 자동 적용

### 4.9 미팅 시작 10분전 슬랙을 통해 알림❌

- 미팅 시작 10분전 슬랙을 통해 알림
- 슬랙 알림에서 미팅시작 여부 질의하고, 'Yes' 입력(선택) 시, '/미팅시작' 코맨드 실행
- 필요할 경우, 구글미트 온라인 회의 링크 생성하고, 트랜스크랩트 기록 시작으로 설정한다음, 링크를 통해서 구글미트 실행될 수 있도록 가이드함

### 4.9 정보 캐시 정책


| 대상    | 정책                        | 상태  |
| ----- | ------------------------- | --- |
| 업체 정보 | `last_searched` 기준 7일 캐시  | ✅   |
| 인물 정보 | 파일 존재 여부만 확인 (7일 캐시 미적용)  | ⚠️  |
| 강제 갱신 | `/company`, `/person` 커맨드 | ✅   |


---

## 5. Before Agent — 미팅 생성

### 5.1 자연어 파싱 ✅

LLM이 사용자 메시지에서 추출:

- 제목, 날짜(YYYY-MM-DD), 시간(HH:MM), 시간(분, 기본 60), 참석자 이름 목록, 어젠다

### 5.2 참석자 이메일 조회 순서 ✅

각 참석자에 대해 아래 소스에서 이메일 후보를 **모두 수집**하고 중복 제거 후 순서 유지:

1. LLM 인라인 이메일 → 후보 수집 없이 바로 사용
2. Slack 워크스페이스 멤버 이름 매칭
3. Gmail 헤더 검색 (`find_email_by_name` — metadata-only 쿼리)
4. Google 주소록 (`find_email_in_contacts` — `contacts.readonly` 스코프 필요, 기존 사용자는 `/재등록` 필요)
5. Drive `People/{이름}.md` 파일 이메일 파싱

후보 수에 따른 처리:

| 후보 수 | 동작 |
|---------|------|
| 인라인 이메일 있음 | 직접 사용 |
| 1개 | 자동 선택 |
| 2개 이상 | Block Kit 버튼 UI로 사용자 선택 요청 |
| 0개 | 경고 후 해당 참석자 제외 |

후보가 여러 개인 참석자가 있는 경우, 미팅 생성이 일시 중단되고 순서대로 선택 UI가 표시됩니다. 모든 선택이 완료되면 Calendar 이벤트가 생성됩니다.

### 5.3 대화형 미팅 생성 ✅

여러 메시지로 나눠서 미팅 정보를 제공할 수 있습니다.

- 첫 번째 메시지로 미팅 초안 생성 → `_meeting_drafts[user_id]`에 저장 (TTL 2시간)
- 이후 메시지가 업데이트인지 새 생성인지 LLM(`merge_meeting_prompt`)으로 판단
- **스레드 답글 전용**: 미팅 생성 메시지에 대한 업데이트는 **해당 스레드의 답글로만** 처리 (2026-04-02 변경)
  - 일반 DM은 새 의도로 취급, 스레드 외부 메시지는 미팅 드래프트 업데이트에 사용하지 않음
  - 판단 기준: `thread_ts`가 봇의 생성 응답 `reply_ts` 또는 원본 요청 `thread_ts`와 일치하는 경우만 업데이트
- 채널에서 `@봇`으로 미팅 생성 시, 해당 스레드에 답글만 달면 `@봇` 멘션 없이도 자동 업데이트
- **수정 가능 필드**: 제목, 참석자, 날짜·시간, 어젠다, **장소(location)**, **업체명(company)** 포함 (2026-04-02 추가)
  - 예: "장소는 로비야" → Google Calendar 이벤트 location 필드 업데이트
  - 예: "업체는 삼성전자야" → Calendar 이벤트 `extendedProperties.private.company` 업데이트
- 명시적으로 새 미팅 생성 요청("오늘 4시 회의 잡아줘")은 기존 초안과 무관하게 신규 생성

### 5.4 생성 결과 ✅

- Google Calendar 이벤트 생성 (KST 기준)
- Google Meet 회의 링크 자동 생성
- **Google Meet 트랜스크립션 자동 활성화**: Meet API v2 `spaces.patch`로 `transcriptionConfig.state: ON` 설정
- 참석자 초대 이메일 발송
- ~~생성 완료 후 해당 미팅 즉시 브리핑~~ → 제거됨 (2026-04-02). 생성 직후 자동 브리핑은 더 이상 실행되지 않음

### 5.4 미팅 생성 시 Contacts 갱신 ❌

- 계획: 미팅 생성 시 참석자 인물 정보 및 기업 정보 자동 갱신

---

## 6. During Agent — 미팅 중 지원 ✅

두 가지 입력 방식을 모두 지원합니다.

### 6.1 방식 A: Google Meet 트랜스크립트 자동 수집 ✅

- **조건**: Google Workspace 유료 계정 (트랜스크립트 자동 생성 기능 필요)
- **동작**: 서버 시작 시 즉시 1회 + 이후 10분 주기로 Drive `Meet Recordings/` 폴더 폴링
- **탐색 경로**: 두 가지 파일 형식 모두 지원
  - 구형 Meet: `Meet Recordings/{회의명}/` → `{회의명} - Transcript` Google Doc
  - Gemini 회의록: `Meet Recordings/` 루트 → `{회의명} - ... - Gemini가 작성한 회의록` Google Doc
- **처리**: Docs API로 전문 텍스트 추출 → LLM 회의록 생성 → Slack Drive 링크 발송 + Drive 저장
- **중복 방지**: 처리 완료된 이벤트 ID를 `processed_{user_id}.json`으로 영속화 → 서버 재시작 후에도 재처리 방지
- **수동 노트 결합**: 동일 이벤트의 수동 노트가 있으면 트랜스크립트와 합쳐서 회의록 생성

### 6.2 방식 B: 수동 Slack 노트 ✅


| 커맨드          | 동작                                                                                            |
| ------------ | --------------------------------------------------------------------------------------------- |
| `/미팅시작 {제목}` | 노트 세션 시작, 진행 중 캘린더 이벤트 자동 매칭 시도                                                               |
| `/메모 {내용}`   | 타임스탬프(HH:MM)와 함께 노트 추가                                                                        |
| `/미팅종료`      | 세션 종료, 백그라운드에서 트랜스크립트 1회 확인 → 있으면 노트 결합 / 없으면 노트만으로 즉시 회의록 생성 ✅ |


### 6.3 회의록 생성 공통 ✅

- **LLM 호출 분리**: 내부용은 초안 생성(`_generate_and_post_minutes()`) 시 1회 호출. 외부용은 `[저장 및 완료]` 확정 후 `finalize_minutes()`에서 생성 (합의 내용만, 공유 가능)
- **Drive 저장 경로**: `Minutes/{YYYY-MM-DD}_{회의명}_내부용.md` / `_외부용.md`
- **Slack 발송**: 전체 내용 대신 Drive 링크 2개(내부용·외부용) 발송
- **LLM 실패 시**: 원본 노트/트랜스크립트를 그대로 저장 (무손실)
- **90분 fallback**: 폴러 처리 미팅(수동 세션 없는 경우)에서 트랜스크립트 미수집 시 노트만으로 회의록 생성
- **회의록 구성**:


| 버전  | 섹션                                           |
| --- | -------------------------------------------- |
| 내부용 | 회의 요약 / 주요 결정 사항 / 액션 아이템 / 주요 논의 내용 / 내부 메모 |
| 외부용 | 회의 개요 / 주요 합의 사항 / 공동 액션 아이템 / 다음 단계         |


### 6.4 `/미팅종료` 시 즉시 회의록 생성 ✅

- **동작**: `/미팅종료` 커맨드 실행 시 `_generate_from_session_end()`를 백그라운드 스레드로 실행
  1. `drive.find_meet_transcript()` — 트랜스크립트 **1회** 탐색
  2. 트랜스크립트 있으면 수동 노트와 결합, 없으면 수동 노트만 사용
  3. `cal.get_recently_ended_meetings()` — Calendar 이벤트에서 참석자·날짜 조회
  4. `_generate_and_post_minutes()` — 회의록 초안 생성
- **90분 fallback**: 수동 세션 없이 Calendar 이벤트만 있는 미팅은 폴러가 기존 방식대로 처리
- **중복 방지**: `/미팅종료` 명시 호출은 `_processed_events.discard(event_id)` 후 항상 재생성 허용

### 6.5 회의록 검토/편집 단계 ✅

회의록 생성 후 Drive 저장 전 반드시 검토 단계를 거칩니다.

```
회의록 초안 생성 완료
      │
      ▼
Slack 초안 메시지 발송 (버튼 4개)
  [ ✅ 저장 및 완료 ]  [ 📝 직접 편집 ]  [ ✏️ 수정 요청 ]  [ ❌ 취소 ]
      │
      ├─ 저장 및 완료: Google Doc 편집 내용 반영 → 외부용 회의록 생성 → Drive 저장 → Slack 발송 → After Agent
      ├─ 직접 편집: Google Docs 링크 오픈 (URL 버튼) → 편집 후 저장 및 완료
      ├─ 수정 요청: 스레드에 수정 지시 입력 → LLM 재생성 → 새 초안
      └─ 취소: 초안 삭제
```

- 초안 생성 시 Google Docs 파일(`{date}_{title}_초안(편집용).gdoc`) 자동 생성
- 저장 및 완료 시 Google Doc 최신 내용을 읽어 편집 반영 후 초안 Doc 삭제

### 6.5 세션 및 처리 상태 영속성 ✅

- 진행 중 세션·노트를 `.sessions/` 폴더에 JSON 파일로 실시간 저장
- 처리 완료된 이벤트 ID도 `processed_{user_id}.json`으로 영속화 (재시작 후 중복 처리 방지)
- 서버 재시작 후 자동 복구 (`_load_sessions()` 모듈 로드 시 실행)
- 회의록 생성 완료 후 임시 파일 자동 삭제

### 6.6 회의록 목록 조회 ✅

- `/회의록` 커맨드 → Drive Minutes 폴더의 파일 목록 최신순 10개 표시 (파일명 + 날짜 + Drive 열기 링크)

### 6.7 구조화 액션아이템 ❌

- 원본 요구사항(FR-D05): 담당자 이름 태그 + 기한이 포함된 구조화된 액션아이템 추출
- 현재: 회의록 내 "액션 아이템" 섹션은 자유 텍스트로 생성 (담당자 구조화 없음)
- 계획: `- [ ] @이름 — 내용 (기한: YYYY-MM-DD)` 형식으로 추출, After Agent에서 리마인더 연동

### 6.8 어젠다 달성 체크 ❌

- 원본 요구사항(FR-D08): 회의록 생성 시 브리핑에 있던 어젠다가 논의되었는지 자동 체크
- 계획: 브리핑 어젠다 항목과 트랜스크립트/노트 내용을 비교하여 달성 여부 표시

### 6.9 이전 미팅 회의록 참조 ❌

- 원본 요구사항(FR-D10): 회의록 생성 시 동일 업체의 이전 회의록을 Drive에서 가져와 맥락으로 포함
- 현재: 이전 미팅 회의록 참조 없이 당일 트랜스크립트/노트만 사용
- 계획: `Minutes/` 폴더에서 동일 업체명 파일 검색 → 최근 1~2개 내용을 LLM 컨텍스트로 추가

### 6.10 미팅 중 Contacts 업데이트 ❌

- 원본 요구사항(FR-D09): 미팅 종료 시 참석자 정보를 Drive Contacts에 자동 반영
- 현재: 회의록 생성 후 Contacts 갱신 없음
- 계획: 회의록 생성 완료 후 참석자 People 파일의 `last_met` 날짜 및 최근 맥락 자동 업데이트

### 6.11 내부용 회의록 포맷 차이 ⚠️

- 원본 요구사항: "내부 메모" 섹션은 LLM이 채우지 않고 사용자가 수동 작성할 수 있도록 빈칸으로 남김
  - 의도: 민감한 전략 판단, 인상 등은 AI가 생성하지 않고 사람이 직접 기입
- 현재: LLM이 모든 섹션(내부 메모 포함) 자동 생성
- 개선 계획: 내부 메모 섹션은 AI 인사이트(요약) + 사용자 수기 입력란으로 구분

---

## 7. After Agent — 미팅 사후 관리 ✅

> `agents/after.py` 구현 완료. 회의록 생성 직후 백그라운드 스레드로 자동 실행.

### 7.1 회의록 외부 발송 (Draft 확인 후 발송) ✅

- `[저장 및 완료]` 확정 후 `finalize_minutes()`에서 외부용 회의록 생성 → After Agent 트리거
- 외부용 회의록 생성 완료 후 Slack Block Kit 버튼 발송 ("발송하기" / "발송 안 함")
- "발송하기" 클릭 → Gmail API로 외부 참석자 이메일 발송 → `pending_drafts.status='sent'`
- 발송 실패 시 `status='failed'` → 버튼 재클릭으로 재시도 가능
- "발송 안 함" 클릭 → `status='cancelled'`
- 발송 대상: Calendar API로 외부 참석자 조회 (내부 도메인 제외)

### 7.2 액션아이템 후속 관리 ✅

- 내부용 회의록에서 LLM으로 액션아이템 추출 → `action_items` 테이블 저장
- 담당자 연락처 조회 순서: Slack 계정 → Google 주소록(People API) → Gmail 이메일 헤더 → Contacts 폴더(Drive)
- Slack UID 있음 → 담당자에게 직접 DM 발송
- Slack UID 없음 + 이메일 있음 → 주최자에게 "이메일로 직접 전달 요청" 안내
- 모두 없음 → 주최자에게 "모든 소스 확인 후 연락처 없음" 명시하여 알림
- 매일 08:00 KST 리마인더: D-day/D-1 기한 미완료 항목 → 주최자 DM

### 7.3 후속 일정 자동 제안 ✅

- 회의록 내 "다음 미팅", "후속 일정" 등 키워드 감지 → "후속 일정 잡기" 버튼 발송

### 7.4 Contacts 자동 갱신 ✅

- 외부 참석자 `People/{이름}.md` 파일에 `last_met` 날짜 + 미팅 이력 1줄 자동 추가

### 7.5 Trello 연동 ✅

- 원본 요구사항(FR-B06-2, FR-A05, FR-A06): Trello 카드 생성·조회·업데이트
- 구현 완료:
  - 브리핑 시: 해당 업체 관련 Trello 카드의 미완료 체크리스트 항목을 맥락으로 포함 (FR-B06-2) ✅
  - 회의록 생성 후: 액션아이템을 Trello 카드 체크리스트에 등록 제안 → 사용자 승인 후 등록 (FR-A05) ✅
  - 카드 없는 업체는 Contact/Meeting 리스트에 자동 생성 ✅
- 사용자별 OAuth 인증: `/trello` 커맨드 → 브라우저 Trello 승인 → 토큰 자동 저장 ✅
- 환경변수: `TRELLO_API_KEY` (앱 공통), `TRELLO_BOARD_ID` — Token은 사용자별 DB 저장
- `DRY_RUN_TRELLO=true`로 API 호출 없이 테스트 가능

### 7.6 제안서 / 리서치 초안 생성 ❌

- 원본 요구사항(FR-A10, FR-A11): 미팅 내용을 바탕으로 제안서 초안 또는 추가 리서치 요청 초안을 LLM으로 자동 생성
- 계획: 회의록 생성 후 "제안서 초안 생성" 버튼 → LLM이 미팅 맥락 기반 초안 작성 → Drive 저장

---

## 8. Contacts 관리

### 8.1 Drive 폴더 구조 ✅

```
MeetingAgent/
├── Contacts/
│   ├── Companies/      ← 업체별 .md 파일
│   └── People/         ← 담당자별 .md 파일
├── Minutes/            ← 회의록 (.md 파일)
└── company_knowledge.md
```

### 8.2 업체 정보 파일 (Companies/{업체명}.md) ✅

```markdown
# {업체명}

## 최근 동향
- last_searched: YYYY-MM-DD
- [기사 제목] (https://출처URL)

## 이메일 맥락
- last_searched: YYYY-MM-DD
- 2026-03-20 | 제목 | snippet (줄바꿈 공백 치환)

## 파라메타 서비스 연결점
- [ICONLOOP DID] ↔ [관심사]: 설명

## ParaScope 브리핑
- last_searched: YYYY-MM-DD
- bullet line
```

섹션 순서: `최근 동향` → `이메일 맥락` → `파라메타 서비스 연결점` → `ParaScope 브리핑`
`last_searched` 라인은 저장되지만 브리핑 출력에는 표시하지 않음.

- 브리핑 시 자동 생성/갱신 (7일 캐시)
- `/company {업체명}` 커맨드로 강제 갱신
- Drive에서 직접 편집 가능
- NFD/NFC 파일명 인코딩 자동 대응, 중복 생성 방지

### 8.3 담당자 정보 파일 (People/{이름}.md) ✅

```markdown
# {이름}

## 기본 정보
- 소속: {회사명}
- last_searched: YYYY-MM-DD
- 이메일: (card_data 우선, 없으면 Gmail 헤더 자동 추출)

## 명함 정보    ← 명함 OCR 시에만 추가
- last_updated: YYYY-MM-DD
- 직책: 팀장
- 부서: AI혁신팀
- 전화: 02-xxx
- 휴대폰: 010-xxx
- (기타 명함 필드, 값이 있는 항목만)

## 이메일 맥락
- 날짜 | 제목 | 본문 요약

## 공개 정보
- (Gemini 웹 검색 결과)
```

- 브리핑 시 자동 생성
- `/person {이름} {회사명}` 커맨드로 강제 갱신
- 인물 갱신 시 연관 기업정보 자동 갱신 (7일 캐시 적용)
- Gmail From/To/CC 헤더에서 이메일 주소 자동 추출

### 8.4 Contacts 조회 커맨드 ❌

- 계획: `/contact {이름 또는 업체명}` → 저장된 정보 Slack으로 조회

### 8.5 파일명 규칙 ✅

- NFD(macOS 업로드)와 NFC(봇 생성) 인코딩 모두 자동 감지 (양방향 탐색)
- 중복 생성 방지: 저장 전 기존 파일 검색 후 있으면 업데이트

### 8.6 company_knowledge.md ✅

- 파라메타/ICONLOOP 서비스 정보, 영업 포인트 기술
- `/update` 커맨드로 LLM 자동 갱신
- 브리핑 시 서비스 연결점 분석에 활용
- 재등록 시 기존 파일 보존 (중복 생성 방지)

---

## 9. Gmail 연동

### 9.1 이메일 검색 ✅

- 인물+업체: `"{이름}" "{회사명}" after:{90일전}` (AND 조건)
- 업체만: `"{회사명}" after:{90일전}`
- 최대 10개 검색 → 상세 조회 5개

### 9.2 추출 정보 ✅


| 항목               | 용도                                       |
| ---------------- | ---------------------------------------- |
| 날짜, 제목, 본문(100자) | 이메일 맥락 섹션 저장                             |
| From/To/CC 헤더    | 인물 이메일 주소 자동 추출 (`parse_address_header`) |


### 9.3 이메일 본문 LLM 요약 ❌

- 현재: 본문 앞 100자 그대로 저장
- 계획: Gemini로 핵심 내용 요약 후 저장

---

## 10. LLM 연동

### 10.1 모델 구성 ✅

- **기본**: Gemini `gemini-2.0-flash` — Google Search 도구 포함
- **폴백**: Claude `claude-haiku-4-5` — Gemini 실패(429 등) 시 자동 전환
- **명함 OCR 전용**: Claude `claude-haiku-4-5` (Vision) — `agents/card.py`에서 직접 호출 (Gemini 미사용)

### 10.2 활용 영역


| 기능                      | 에이전트   | 상태  |
| ----------------------- | ------ | --- |
| 업체 뉴스 웹 검색              | Before | ✅   |
| 담당자 공개 정보 검색            | Before | ✅   |
| 서비스 연결점 분석              | Before | ✅   |
| 미팅 자연어 파싱               | Before | ✅   |
| 업체명 추출                  | Before | ✅   |
| company_knowledge.md 갱신 | Before | ✅   |
| 트랜스크립트 기반 회의록 생성        | During | ✅   |
| 수동 노트 기반 회의록 생성         | During | ✅   |
| 이메일 본문 요약               | Before | ❌   |
| 명함 OCR (Vision)           | card   | ✅   |
| 액션아이템 추출                | After  | ✅   |


---

## 11. 스케줄러


| 기능                    | 주기           | 상태  |
| --------------------- | ------------ | --- |
| 전체 사용자 자동 브리핑         | 매일 09:00 KST | ✅   |
| Google Meet 트랜스크립트 폴링 | 10분 주기       | ✅   |
| 사용자별 브리핑 시간 커스터마이징    | —            | ❌   |
| 액션아이템 리마인더            | 매일 08:00 KST | ✅   |


---

## 12. 환경 설정 (.env)


| 변수                    | 설명                            | 상태  |
| --------------------- | ----------------------------- | --- |
| `SLACK_BOT_TOKEN`     | Slack Bot Token (xoxb-...)    | ✅   |
| `SLACK_APP_TOKEN`     | Slack App Token (xapp-...)    | ✅   |
| `GOOGLE_API_KEY`      | Gemini API 키                  | ✅   |
| `ANTHROPIC_API_KEY`   | Claude API 키 (폴백)             | ✅   |
| `ENCRYPTION_KEY`      | Fernet 암호화 키                  | ✅   |
| `OAUTH_CALLBACK_URL`  | Google OAuth 콜백 URL (ngrok 등) | ✅   |
| `INTERNAL_DOMAINS`    | 내부 도메인 목록 (쉼표 구분)             | ✅   |
| `SLACK_ERROR_CHANNEL` | 에러 로깅용 Slack 채널 ID            | ❌   |
| `TRELLO_API_KEY`      | Trello Power-Up API 키 (앱 공통)  | ✅   |
| `TRELLO_BOARD_ID`     | 연동할 Trello 보드 ID              | ✅   |
| `DREAMPLUS_BASE_URL`  | Dreamplus API 기본 URL            | ✅   |


---

## 12.5 Dreamplus 회의실 예약 연동 ✅

`agents/dreamplus.py` + `tools/dreamplus.py` 구현 완료 (2026-04-02 대폭 개선).

### 슬래시 커맨드 / 인텐트

| 커맨드 / 인텐트 | 동작 |
|--------------|------|
| `/드림플러스` / `/dreamplus` | Dreamplus 계정 등록 모달 표시 |
| `/회의실예약` / `dreamplus_book` 인텐트 | 회의실 예약 (자연어 시간 파싱) |
| `/회의실조회` / `dreamplus_list` 인텐트 | 내 예약 목록 조회 |
| `/회의실취소` / `dreamplus_cancel` 인텐트 | 예약 취소 |
| `/크레딧조회` / `check_credits` 인텐트 | 잔여 크레딧 조회 (API 오류 시 보류) |

### `tools/dreamplus.py` API 클라이언트

- RSA 공개키 조회 + 비밀번호 RSA 암호화 → `/auth/login` → JWT + company_id 반환
- JWT 캐시: `(jwt, pub_key, member_id, company_id)` 4-field 캐시, TTL **30분** (이전 6시간 → 단축)
- 자동 재인증: `TokenExpiredError` (code=301) 또는 `RuntimeError` 발생 시 `force_refresh=True`로 재로그인 1회 후 즉시 재시도 — `book_room`, `auto_book_room`, `list_reservations`, `confirm_cancel` 전체에 적용
- 주요 함수: `login()`, `get_credits(jwt)`, `get_rooms(jwt, date)`, `reserve(jwt, ...)`, `cancel_reservation(jwt, ...)`

### `agents/dreamplus.py` 주요 동작

- **시간 파싱**: "5시" → 오후 5시(17:00)로 해석 (오전/오후 미지정 + 1~8시 → 오후 간주)
- **회의실 추천 네비게이션**: 추천 목록 3개씩 페이지 단위 표시, **[이전] [다음]** 버튼 모두 지원
  - `dreamplus_prev_rooms` / `dreamplus_next_rooms` Block Kit 액션 처리
- **자동 예약** (`auto_book_room`): 일정 생성 시 회의실 조건 매칭 → 1순위 자동 예약 시도
- **예약 취소**: 기본 DELETE 요청 실패 시 암호화(ek/ed 파라미터) DELETE로 자동 재시도

---

## 13. 공통 아키텍처 — 미구현 항목

### 13.1 에러 로깅 채널 ❌

- 원본 요구사항(CM-07): 봇 운영 중 발생하는 예외·에러를 지정된 Slack 채널(예: `#bot-errors`)로 자동 발송
- 현재: 서버 콘솔 로그에만 기록 (`logger.error`)
- 계획: 환경변수 `SLACK_ERROR_CHANNEL` 지정 → 예외 발생 시 해당 채널에 에러 메시지 자동 게시
- 환경변수 필요: `SLACK_ERROR_CHANNEL`

### 13.2 외부 발송 전 Draft 확인 흐름 ✅

- 원본 요구사항(CM-05): AI가 생성한 이메일·회의록 외부 발송 전 사용자 확인 단계 필수
- 구현: After Agent가 회의록 생성 직후 Slack Block Kit 버튼("발송하기" / "발송 안 함") 발송, 사용자 승인 후 Gmail API로 발송

### 13.3 미팅 단계 연결 ID ✅

- 원본 요구사항(CM-03): Before → During → After를 동일 Google Calendar 이벤트 ID로 추적
- 구현: Before/During은 `event_id` 기반으로 연결, After Agent는 `event_id`를 `action_items`·`pending_drafts` 테이블 키로 사용하여 세 단계 완전 연결

---

## 14. 파일 구조

```
meeting-agent/
├── .env
├── main.py                     # Slack Bolt + Scheduler 진입점
├── agents/
│   ├── before.py               # Before 에이전트
│   ├── during.py               # During 에이전트
│   ├── after.py                # After 에이전트
│   ├── card.py                 # 명함 OCR 에이전트 (Claude Haiku Vision)
│   └── room.py                 # Dreamplus 회의실 예약 에이전트
├── tools/
│   ├── calendar.py             # Google Calendar API 래퍼
│   ├── docs.py                 # Google Docs API 래퍼 (트랜스크립트 읽기)
│   ├── drive.py                # Google Drive API 래퍼
│   ├── gmail.py                # Gmail API 래퍼
│   ├── slack_tools.py          # 브리핑 메시지 빌더
│   └── dreamplus.py            # Dreamplus API 클라이언트
├── prompts/
│   ├── briefing.py             # LLM 프롬프트 로더 (템플릿 변수 치환)
│   └── templates/              # 외부 프롬프트 템플릿 파일 (2026-04-02 추가)
│       ├── minutes_internal.md
│       ├── minutes_external.md
│       ├── company_news.md
│       ├── person_info.md
│       ├── service_connection.md
│       └── briefing_summary.md
├── store/
│   └── user_store.py           # SQLite + Fernet 사용자 토큰 관리
├── server/
│   └── oauth.py                # FastAPI OAuth 콜백 서버
├── .sessions/                  # 세션 임시 파일 (gitignore, 서버 재시작 복구용)
│   ├── active_{user_id}.json       # 진행 중 세션
│   ├── completed_{event_id}.json   # 레거시 (신규 생성 안 함, 기존 파일 처리용)
│   └── processed_{user_id}.json    # 처리 완료 event_id 목록 (중복 방지)
├── tests/                      # 단위 테스트 (149개, 전체 통과)
└── docs/
    ├── requirements.md
    ├── before-agent-design.md
    ├── during-agent-design.md
    ├── after-agent-design.md
    ├── llm-usage.md
    └── test-guide.md
```

---

## 15. 구현 로드맵

### Phase 1 — Before Agent 핵심 기능 ✅ 완료

- 다중 사용자 OAuth 등록 / 재등록
- 자동/수동 브리핑
- 외부/내부 미팅 분류
- 업체/인물 리서치 (Gmail + 웹 검색)
- 미팅 생성 (자연어)
- 어젠다 등록

### Phase 2 — Before Agent 보완 ⚠️ 진행 중

- 기업/인물 강제 리서치 커맨드 (`/company`, `/person`) ✅
- Gmail AND 검색 쿼리 ✅
- 인물 이메일 자동 추출 ✅
- 공개 이메일 서비스 도메인 업체명 추출 제외 (`_PUBLIC_EMAIL_DOMAINS`) ✅
- 외부 도메인 감지 후 Contacts 정식명 우선 반환 ✅
- 이전 맥락에 Drive 회의록 검색 추가 (NFD/NFC 정규화 포함) ✅
- 브리핑에 어젠다 표시 (Calendar description 활용, FR-B16) ✅
- NFD/NFC 중복 파일 방지 ✅
- OAuth retry 세션 충돌 방지 ✅
- 인물 정보 7일 신선도 체크 ❌
- OAuth 토큰 자동 갱신 ❌
- 사용자별 브리핑 시간 설정 ❌
- 브리핑 채널 공유 (FR-B12) ❌
- 미팅 유형별 어젠다 템플릿 (FR-B14) ❌

### Phase 3 — During Agent ✅ 완료

- Google Meet 트랜스크립트 자동 수집 및 회의록 생성
- 수동 Slack 노트 (`/미팅시작`, `/메모`, `/미팅종료`)
- 트랜스크립트 + 수동 노트 결합 생성
- 내부용·외부용 2종 회의록 생성
- 회의록 Drive 저장 및 목록 조회
- `.sessions/` 파일 영속성 (서버 재시작 후 세션 자동 복구)

### Phase 3.5 — During Agent 고도화 ⚠️ 부분 완료

- `/미팅종료` 즉시 회의록 생성 (트랜스크립트 유무 무관) ✅
- 구조화 액션아이템 추출 (담당자 태그 + 기한, FR-D05) ❌
- 어젠다 달성 체크 (브리핑 어젠다 vs 트랜스크립트 비교, FR-D08)
- 이전 미팅 회의록 참조 (동일 업체 Drive 검색, FR-D10)
- 미팅 종료 시 Contacts 자동 업데이트 (FR-D09)
- 내부용 회의록 "내부 메모" 섹션을 AI 인사이트 + 수동 입력란으로 분리
- Slack 에러 로깅 채널 (CM-07)

### Phase 4 — After Agent ✅ 완료

- 외부용 회의록 Draft 확인 후 이메일 발송 (CM-05, FR-A01) ✅
- 담당자별 액션아이템 Slack DM 발송 + 리마인더 (FR-A03, FR-A04) ✅
- 후속 일정 자동 제안 (FR-A08) ✅
- Contacts 자동 갱신 (FR-A09) ✅
- 제안서 / 리서치 초안 생성 (FR-A10, FR-A11) ❌ (미구현)

### Phase 4.5 — Trello 연동 ✅

- 브리핑 시 관련 Trello 카드 미완료 체크리스트 포함 (FR-B06-2) ✅
- 회의록 생성 후 액션아이템 → Trello 카드 체크리스트 등록 제안 (FR-A05) ✅
- 사용자별 OAuth 인증 (`/trello` 커맨드) ✅
- 구현 파일: `tools/trello.py`, `server/oauth.py`, `agents/before.py`, `agents/after.py`

### Phase 5 — 파일 업로드 확장 ❌ 계획

- **오디오 업로드 → 회의록 생성**: `@봇` + 오디오(MP3/M4A/WAV 등) → STT 변환 → 최근 일정 매칭 → 내부용·외부용 회의록 생성
- **텍스트 문서 업로드 → 회의록 생성**: `@봇` + 텍스트(TXT/DOCX/PDF 등) → 최근 일정 매칭 → LLM 분석 → 내부용·외부용 회의록 생성
- **이미지 업로드 → 자동 분기**: `@봇` + 이미지(JPG/PNG 등) → LLM 비전으로 명함/손글씨 자동 판별
  - 명함 → People Contacts 자동 등록
  - 손글씨 노트 → OCR 후 최근 일정 매칭 → 회의록 생성
  - 판별 불가 → 사용자 확인 요청

