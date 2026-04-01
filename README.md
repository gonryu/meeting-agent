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
| **Before** | 외부 미팅 자동 감지 → 업체·인물 리서치 → 브리핑 Slack 발송 |
| **Before** | 자연어 미팅 생성 (`내일 오전 10시 KISA 미팅 잡아줘`) |
| **Before** | 명함 DM 업로드 → Claude Vision OCR → Contacts 자동 등록 |
| **Before** | Dreamplus 회의실 검색 / 예약 / 크레딧 조회 |
| **During** | Google Meet 트랜스크립트 자동 폴링 (10분 주기) |
| **During** | Slack 수동 노트 세션 (`/미팅시작`, `/메모`, `/미팅종료`) |
| **After** | 내부용·외부용 회의록 자동 생성 → Drive 저장 |
| **After** | 외부용 회의록 Gmail 발송 (사용자 승인 후) |
| **After** | 액션아이템 추출 → 담당자 DM → 매일 08:00 리마인더 |

---

## 기술 스택

| 항목 | 내용 |
|------|------|
| **LLM** | Gemini `gemini-2.0-flash` (기본) + Claude `claude-haiku-4-5` (폴백 / 명함 OCR) |
| **인터페이스** | Slack Bolt (Socket Mode) |
| **Google 연동** | Calendar · Drive · Gmail · Docs API |
| **스케줄러** | APScheduler (브리핑 09:00, 트랜스크립트 폴링 10분, 리마인더 08:00) |
| **저장소** | SQLite + Fernet 암호화 (사용자 토큰), Google Drive (Contacts · 회의록) |
| **외부 서비스** | ParaScope 봇, Dreamplus API |

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
nohup .venv/bin/python3 main.py >> server.log 2>&1 &
```

### 4. Slack에서 등록

```
/등록
```

Google OAuth 인증 후 Drive 폴더 자동 생성.

---

## 슬래시 커맨드

| 커맨드 | 설명 |
|--------|------|
| `/등록` / `/register` | Google 계정 연동 |
| `/재등록` / `/reregister` | Google 계정 재인증 |
| `/브리핑` / `/brief` | 오늘 미팅 브리핑 수동 실행 |
| `/미팅추가` / `/meet` | 자연어로 미팅 생성 |
| `/기업` / `/company` | 기업 정보 강제 리서치 |
| `/인물` / `/person` | 인물 정보 강제 리서치 |
| `/미팅시작` | 수동 노트 세션 시작 |
| `/메모` | 미팅 중 노트 추가 |
| `/미팅종료` | 세션 종료 + 회의록 즉시 생성 |
| `/회의록` | 저장된 회의록 목록 조회 |
| `/업데이트` / `/update` | company_knowledge.md 갱신 |
| `/dreamplus` | Dreamplus 계정 등록 |
| `/크레딧` | Dreamplus 크레딧 잔여량 조회 |

자연어 DM도 지원합니다 (`브리핑 해줘`, `드림플러스 크레딧 조회해줘` 등).

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
├── agents/
│   ├── before.py           # Before 에이전트 (브리핑, 리서치, 미팅 생성)
│   ├── during.py           # During 에이전트 (트랜스크립트, 노트 세션)
│   ├── after.py            # After 에이전트 (회의록 발송, 액션아이템)
│   ├── card.py             # 명함 OCR 에이전트
│   └── room.py             # Dreamplus 회의실 예약 에이전트
├── tools/
│   ├── calendar.py         # Google Calendar API
│   ├── docs.py             # Google Docs API (트랜스크립트 읽기)
│   ├── drive.py            # Google Drive API
│   ├── gmail.py            # Gmail API
│   ├── slack_tools.py      # Slack Block Kit 메시지 빌더
│   └── dreamplus.py        # Dreamplus API 클라이언트
├── prompts/
│   └── briefing.py         # LLM 프롬프트 템플릿
├── store/
│   └── user_store.py       # SQLite + Fernet 사용자 토큰 관리
├── server/
│   └── oauth.py            # FastAPI Google OAuth 콜백 서버
├── tests/                  # 단위 테스트 (149개)
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
pytest tests/ -v          # 상세 출력
pytest tests/test_before.py  # 특정 파일만
```

---

## Google OAuth 권한 범위

| Scope | 용도 |
|-------|------|
| `calendar` | 미팅 조회·생성·설명 업데이트 |
| `drive` | Contacts·회의록·company_knowledge.md 읽기/쓰기 |
| `gmail.readonly` | 이메일 맥락 검색, 이메일 주소 추출 |
| `gmail.send` | 외부용 회의록 이메일 발송 |
| `documents.readonly` | Google Meet 트랜스크립트 읽기 |
