# Meeting Agent

Slack 기반 AI 미팅 어시스턴트. 미팅의 전(Before) · 중(During) · 후(After) 전 단계를 자동화합니다.

```
Before Agent  →  During Agent  →  After Agent
업체/인물 리서치    트랜스크립트 수집    회의록 외부 발송
브리핑 생성         수동 노트 세션       액션아이템 알림
미팅 생성/예약      내부·외부 회의록     Contacts 자동 갱신
```

---

## 주요 기능

| 단계 | 기능 |
|------|------|
| **Before** | 외부 미팅 자동 감지 → 미팅 헤더 즉시 발송 → 업체·인물 리서치 백그라운드 순차 발송 |
| **Before** | 자연어 미팅 생성 (`내일 오전 10시 KISA 미팅 잡아줘`) + 스레드 답글로 업체명·장소 등 수정 |
| **Before** | 명함 DM 업로드 → Claude Vision OCR → Contacts 자동 등록 |
| **During** | Google Meet 트랜스크립트 자동 폴링 (10분 주기) |
| **During** | Slack 수동 노트 세션 (`/미팅시작`, `/메모`, `/미팅종료`) |
| **During** | 음성 파일 업로드 → Deepgram STT → 메모 자동 등록 |
| **After** | 내부용·외부용 회의록 초안 검토/편집 후 Drive 저장 |
| **After** | 외부용 회의록 Gmail 발송 (사용자 승인 후) |
| **After** | 액션아이템 추출 → 담당자 DM → 매일 08:00 리마인더 |

---

## 기술 스택

| 항목 | 내용 |
|------|------|
| **LLM** | Gemini `gemini-2.0-flash` (기본) + Claude `claude-haiku-4-5` (폴백 / 명함 OCR) |
| **STT** | Deepgram REST API (`nova-2` 모델, 한국어) |
| **인터페이스** | Slack Bolt (Socket Mode) |
| **Google 연동** | Calendar · Drive · Gmail · Docs · Meet API |
| **스케줄러** | APScheduler (브리핑 09:00, 트랜스크립트 폴링 10분, 리마인더 08:00) |
| **저장소** | SQLite + Fernet 암호화 (사용자 토큰), Google Drive (Contacts · 회의록) |

---

## 빠른 시작

### 1. 의존성 설치

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 환경 변수 설정

`.env` 파일 생성:

```env
# Slack
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...

# LLM
GOOGLE_API_KEY=...
ANTHROPIC_API_KEY=...

# STT
DEEPGRAM_API_KEY=...

# Google OAuth
OAUTH_CALLBACK_URL=https://your-domain.com/oauth/callback

# 암호화 키 (Fernet)
ENCRYPTION_KEY=...

# 내부 도메인 (쉼표 구분)
INTERNAL_DOMAINS=parametacorp.com,iconloop.com

# ParaScope 연동 (선택)
PARASCOPE_BOT_ID=...
PARASCOPE_BOT_APP_ID=...
PARASCOPE_CHANNEL_ID=...
```

### 3. 서버 실행

```bash
bash start.sh
```

> `start.sh`는 기존 프로세스를 종료하고 단일 인스턴스로 실행합니다.

### 4. Slack에서 등록

```
/등록
```

Google OAuth 인증 후 Drive 폴더 자동 생성.

---

## 키 및 토큰 발급 가이드

### Slack Bot Token & App Token

1. [api.slack.com/apps](https://api.slack.com/apps) 접속 → **Create New App** → **From scratch**
2. App Name 입력, 워크스페이스 선택 후 생성

**Bot Token (`SLACK_BOT_TOKEN`)**

3. 좌측 메뉴 **OAuth & Permissions** 이동
4. **Bot Token Scopes** 에서 아래 권한 추가:
   ```
   app_mentions:read
   channels:history
   chat:write
   commands
   files:read
   groups:history
   im:history
   im:read
   im:write
   users:read
   ```
5. **Install to Workspace** → 설치 완료 후 `Bot User OAuth Token` 복사 → `SLACK_BOT_TOKEN`

**App Token (`SLACK_APP_TOKEN`)**

6. 좌측 메뉴 **Basic Information** → **App-Level Tokens** → **Generate Token**
7. Token Name 입력, Scope에 `connections:write` 추가 → 생성
8. 생성된 `xapp-...` 토큰 복사 → `SLACK_APP_TOKEN`

**Socket Mode 활성화**

9. 좌측 메뉴 **Socket Mode** → **Enable Socket Mode** 켜기

**Slash Commands 등록**

10. 좌측 메뉴 **Slash Commands** → **Create New Command** 로 아래 커맨드 등록:
    `/등록`, `/재등록`, `/브리핑`, `/brief`, `/미팅추가`, `/meet`, `/기업`, `/company`, `/인물`, `/person`, `/미팅시작`, `/메모`, `/미팅종료`, `/회의록`, `/업데이트`, `/update`, `/도움말`, `/help`, `/드림플러스설정`, `/dreamplus`

---

### ngrok — 개발용 Public URL

Google OAuth 콜백을 로컬에서 받으려면 외부에서 접근 가능한 URL이 필요합니다.

```bash
# ngrok 설치 (macOS)
brew install ngrok

# 인증 (https://ngrok.com 에서 토큰 발급)
ngrok config add-authtoken <your-authtoken>

# 포트 8000 포워딩 (OAuth 서버 기본 포트)
ngrok http 8000
```

ngrok 실행 후 출력되는 `https://xxxx-xxx.ngrok-free.app` 주소를 `.env`에 설정합니다:

```env
OAUTH_CALLBACK_URL=https://xxxx-xxx.ngrok-free.app/oauth/callback
```

> 무료 플랜은 세션 종료 시 URL이 변경됩니다. 재실행 후 `.env`와 Google Cloud Console의 리다이렉트 URI를 함께 업데이트해야 합니다.

---

### Google API Key (`GOOGLE_API_KEY`)

Gemini LLM 호출용 키입니다.

1. [Google AI Studio](https://aistudio.google.com/app/apikey) 접속
2. **Create API key** 클릭 → 프로젝트 선택 또는 신규 생성
3. 생성된 키 복사 → `GOOGLE_API_KEY`

---

### Google OAuth 클라이언트 설정

Calendar / Drive / Gmail / Docs / Meet API 연동에 사용되는 OAuth 2.0 자격 증명입니다.

1. [Google Cloud Console](https://console.cloud.google.com) 접속 → 프로젝트 선택/생성
2. **APIs & Services → Library** 에서 아래 API 활성화:
   - Google Calendar API
   - Google Drive API
   - Gmail API
   - Google Docs API
   - Google Meet API (Google Workspace Meeting API)
   - People API
3. **APIs & Services → OAuth consent screen** 설정:
   - User Type: **Internal** (조직 내부용) 또는 External
   - 앱 이름, 지원 이메일 입력
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Web application**
   - Authorized redirect URIs에 `OAUTH_CALLBACK_URL` 값 추가
5. 생성된 **Client ID** · **Client Secret** → `server/oauth.py`의 `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` 환경변수로 설정

```env
GOOGLE_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=GOCSPX-...
```

---

### Fernet 암호화 키 (`ENCRYPTION_KEY`)

사용자 Google OAuth 토큰을 SQLite에 저장할 때 암호화에 사용됩니다.

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

출력된 값을 `.env`에 설정합니다:

```env
ENCRYPTION_KEY=abc123...==
```

> 이 키를 분실하면 저장된 모든 사용자 토큰을 복호화할 수 없습니다. 안전한 곳에 백업하세요.

---

### Deepgram API Key (`DEEPGRAM_API_KEY`)

음성 파일 STT(Speech-to-Text) 변환에 사용됩니다.

1. [console.deepgram.com](https://console.deepgram.com) 접속 → 회원가입 또는 로그인
2. 좌측 메뉴 **API Keys → Create a New API Key**
3. Key Name 입력, Permissions: **Member** 선택 → 생성
4. 생성된 키 복사 → `DEEPGRAM_API_KEY`

**사용 모델**: `nova-2` (한국어 지원, `smart_format`, `punctuate` 옵션 활성화)

**지원 포맷**: MP3, MP4, M4A, WAV, OGG, WebM, AAC 등 (최대 500MB)

> 사내 방화벽 환경에서 SSL 인증서 오류 발생 시 `tools/stt.py`의 `verify=False` 설정으로 우회합니다.

---

## 슬래시 커맨드

| 커맨드 | 설명 |
|--------|------|
| `/등록` / `/register` | Google 계정 연동 |
| `/재등록` / `/reregister` | Google 계정 재인증 (스코프 변경 시 활용) |
| `/브리핑` / `/brief` | 오늘 미팅 브리핑 수동 실행 |
| `/미팅추가` / `/meet` | 자연어로 미팅 생성 |
| `/기업` / `/company` | 기업 정보 강제 리서치 |
| `/인물` / `/person` | 인물 정보 강제 리서치 |
| `/미팅시작` | 수동 노트 세션 시작 |
| `/메모` | 미팅 중 노트 추가 (세션 없으면 자동 시작) |
| `/미팅종료` | 세션 종료 + 회의록 즉시 생성 + 검토 단계 |
| `/회의록` | 저장된 회의록 목록 조회 |
| `/업데이트` / `/update` | company_knowledge.md 갱신 |
| `/도움말` / `/help` | 사용 가능한 커맨드 및 자연어 명령어 안내 |
| `/드림플러스설정` / `/dreamplus` | 드림플러스 계정 등록/변경 |

자연어 DM도 지원합니다 (`브리핑 해줘`, `내일 3시 KISA 미팅 잡아줘` 등).

---

## 브리핑 동작 방식

외부 미팅 브리핑은 **2단계 비동기** 방식으로 동작합니다. 리서치 대기 없이 미팅 정보를 즉시 수신하고, 리서치 결과는 완료되는 순서대로 스레드에 추가됩니다.

```
1단계 (즉시)     미팅 헤더 발송
                  ┌────────────────────────────────┐
                  │ 📋 업체명 — 오후 3:00 (Google Meet) │
                  │ [어젠다 내용 또는 스레드 답글 안내]   │
                  └────────────────────────────────┘

2단계 (백그라운드, 업체별 순차)
  → 업체 리서치 완료  🏢 업체명 리서치 결과 (ParaScope / 업체 동향 / 서비스 연결점)
  → 담당자 완료       👤 담당자 정보
  → 맥락 조회 완료    📌 이전 미팅 맥락 + 📧 이메일 맥락
```

> 다중 업체 브리핑 시에도 단일 스레드에서 순차 처리하므로 두 업체의 결과가 섞이지 않습니다.

---

## 프롬프트 템플릿 커스터마이징

LLM에 전달되는 주요 프롬프트는 `prompts/templates/` 폴더의 마크다운 파일로 관리됩니다.
**파일을 수정한 뒤 서버를 재실행하면** 변경 내용이 즉시 반영됩니다 (코드 수정 불필요).

```
prompts/templates/
├── minutes_internal.md     # 내부용 회의록 생성 프롬프트
├── minutes_external.md     # 외부용 회의록 생성 프롬프트
├── company_news.md         # 업체 최근 동향 검색 프롬프트
├── person_info.md          # 인물 정보 검색 프롬프트
└── service_connection.md   # 서비스 연결점 분석 프롬프트
```

템플릿 내 변수는 `{{변수명}}` 형식으로 작성합니다:

| 템플릿 파일 | 사용 변수 |
|------------|----------|
| `minutes_internal.md` | `{{title}}`, `{{date}}`, `{{attendees}}`, `{{sources}}` |
| `minutes_external.md` | `{{title}}`, `{{date}}`, `{{attendees}}`, `{{internal_minutes}}` |
| `company_news.md` | `{{today}}`, `{{company_name}}` |
| `person_info.md` | `{{person_name}}`, `{{company_name}}` |
| `service_connection.md` | `{{knowledge}}`, `{{company_info}}` |

> 인라인으로 관리되는 프롬프트(미팅 파싱, 인텐트 분류, 액션아이템 추출 등)는 `prompts/briefing.py`에서 직접 수정합니다.

---

## Drive 폴더 구조

```
MeetingAgent/
├── Contacts/
│   ├── Companies/          # {업체명}.md
│   └── People/             # {이름}.md
├── Minutes/                # {날짜}_{제목}_내부용.md / _외부용.md
└── company_knowledge.md    # 자사 서비스 요약
```

---

## 프로젝트 구조

```
meeting-agent/
├── main.py                 # Slack Bolt + APScheduler 진입점
├── start.sh                # 단일 인스턴스 실행 스크립트
├── agents/
│   ├── before.py           # Before 에이전트 (브리핑, 리서치, 미팅 생성)
│   ├── during.py           # During 에이전트 (트랜스크립트, 노트, 회의록 검토)
│   ├── after.py            # After 에이전트 (회의록 발송, 액션아이템)
│   └── card.py             # 명함 OCR 에이전트
├── tools/
│   ├── calendar.py         # Google Calendar API
│   ├── docs.py             # Google Docs API (트랜스크립트 읽기)
│   ├── drive.py            # Google Drive API
│   ├── gmail.py            # Gmail API
│   ├── stt.py              # Deepgram STT API
│   └── slack_tools.py      # Slack Block Kit 메시지 빌더
├── prompts/
│   ├── briefing.py         # LLM 프롬프트 함수 (템플릿 로더 포함)
│   └── templates/          # 외부 프롬프트 템플릿 (서버 재실행으로 반영)
│       ├── minutes_internal.md
│       ├── minutes_external.md
│       ├── company_news.md
│       ├── person_info.md
│       └── service_connection.md
├── store/
│   └── user_store.py       # SQLite + Fernet 사용자 토큰 관리
├── server/
│   └── oauth.py            # FastAPI Google OAuth 콜백 서버
├── tests/                  # 단위 테스트
├── requirements.txt
└── docs/                   # 설계 문서
```

---

## 문서

| 문서 | 내용 |
|------|------|
| [요구사항](docs/requirements.md) | 전체 시스템 요구사항 및 구현 현황 |
| [Before Agent 설계](docs/before-agent-design.md) | 브리핑, 리서치, 미팅 생성 상세 설계 |
| [During Agent 설계](docs/during-agent-design.md) | 트랜스크립트 수집, 수동 노트 세션 설계 |
| [After Agent 설계](docs/after-agent-design.md) | 회의록 발송, 액션아이템, Contacts 갱신 설계 |
| [LLM 사용 현황](docs/llm-usage.md) | LLM 호출 함수·프롬프트 목록 |
| [테스트 가이드](docs/test-guide.md) | 테스트 실행 방법 및 구성 |

---

## 테스트

```bash
pytest tests/
pytest tests/ -v             # 상세 출력
pytest tests/test_before.py  # 특정 파일만
```

---

## Google OAuth 권한 범위

| Scope | 용도 |
|-------|------|
| `calendar` | 미팅 조회·생성·설명 업데이트·extendedProperties 패치 |
| `drive` | Contacts·회의록·company_knowledge.md 읽기/쓰기 |
| `gmail.readonly` | 이메일 맥락 검색, 이메일 주소 추출 |
| `gmail.send` | 외부용 회의록 이메일 발송 |
| `documents.readonly` | Google Meet 트랜스크립트 읽기 |
| `contacts.readonly` | Google 주소록 참석자 이메일 조회 (신규 등록자부터 적용, 기존 사용자는 `/재등록` 필요) |
| `meetings.space.created` | Google Meet 트랜스크립션 자동 활성화 |
