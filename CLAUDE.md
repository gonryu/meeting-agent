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
- 드림플러스 회의실 관련 기능은 libs/dreamplus-apis.md 파일을 참고합니다.

## 아키텍처 개요

### 진입점 — `main.py`

Slack 이벤트·슬래시 커맨드·버튼 액션 핸들러를 모두 등록하는 단일 파일입니다. APScheduler로 3개의 정기 작업을 관리합니다:

- `scheduled_briefing()` — 매일 09:00 KST, 전체 사용자 브리핑
- `check_transcripts()` — 10분 주기, Drive 트랜스크립트 폴링
- `action_item_reminder()` — 매일 08:00 KST, 오픈 액션아이템 DM

### 에이전트 레이어 — `agents/`


| 파일             | 역할                                                                                           |
| -------------- | -------------------------------------------------------------------------------------------- |
| `before.py`    | 브리핑·리서치·미팅 생성. 가장 복잡. 모듈 수준 상태(`_pending_agenda`, `_meeting_drafts`, `_pending_meetings`) 보유 |
| `during.py`    | 트랜스크립트 수집·수동 노트·회의록 초안 생성. `_pending_minutes` 상태 관리                                          |
| `after.py`     | 회의록 발송(Gmail)·액션아이템 추출·Contacts 갱신. During Agent 완료 후 백그라운드 스레드로 실행                          |
| `card.py`      | 명함 이미지 OCR (Claude Haiku Vision)                                                             |
| `dreamplus.py` | 드림플러스 회의실 예약 (JWT 인증)                                                                        |


### 상태 관리 패턴

에이전트들은 두 종류의 상태를 사용합니다:

- **메모리 딕셔너리**: `_pending_agenda`, `_meeting_drafts`, `_pending_minutes` — 재시작 시 소멸
- **파일 영속화**: `data/pending_agenda.json`, `.sessions/processed_{user_id}.json` — 재시작 후에도 유지
- **SQLite** (`store/users.db`): 사용자 토큰, 액션아이템, 드래프트

### LLM 호출 — `agents/before.py`

```python
_search(prompt)    # Gemini + GoogleSearch → 실패 시 Claude + web_search
_generate(prompt)  # Gemini generate → 실패 시 Claude (검색 없음)
generate_text()    # _generate의 public 래퍼 (main.py의 인텐트 분류에서 사용)
```

Gemini `gemini-2.0-flash`가 기본, 오류(429 등) 시 Claude `claude-haiku-4-5`로 자동 폴백합니다. 

회의록 추출 시에는 Claude sonnet을 사용합니다.

### 브리핑 비동기 흐름

`run_briefing()`은 2단계로 동작합니다:

1. **즉시**: 모든 미팅의 헤더 블록을 순서대로 발송 (`_send_briefing` → `build_meeting_header_block`)
2. **백그라운드**: 단일 스레드에서 업체별 순차 처리 (`_run_all_briefing_research` → `_run_briefing_research`) — 다중 업체 결과 섞임 방지

### Google OAuth — 스코프 주의사항

스코프는 두 파일에서 관리합니다:

- `server/oauth.py` — 실제 사용자 동의 화면 요청 스코프 (여기서 추가해야 동의 화면에 반영)
- `store/user_store.py` — 토큰 복원 시 사용 (기존 토큰 갱신 호환성 때문에 `contacts.readonly` 의도적 제외)

스코프를 추가할 경우 **두 파일 모두 확인**하고, 기존 사용자에게 `/재등록` 안내가 필요한지 판단해야 합니다.

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
- 명시적인 커밋과 푸시 명령이 있을때에만 커밋과 푸시를 수행함

