# 에이전트형 리서치 엔진 설계 (Agentic Research Engine)

작성일 2026-06-29 · 설계 확정 전 리뷰용

## 0. 북극성

**"제대로 나오게 한다"** — 풍부하고(실물 자료 기반) + 정확하게(환각·동명타사 없음). 비용·범위는 품질 도달 *후에* 조이는 2차 관심사.

기준점: Claude-in-Slack이 Google Drive MCP + Gmail로 만들어낸 브리핑(실제 견적서 PDF 항목·할인·유효기한, RFQ→견적→확정 메일 흐름, 참석자 연락처, "오늘 논의 포인트"까지). 우리 봇이 이 수준을 내야 한다.

## 1. 진단 — 왜 현재 봇은 얕은가

| 격차 | 현재 | 필요 |
|---|---|---|
| **첨부 추출(한글 포함)** ⭐ | 텍스트 문서만 추출, 한글 미지원 | **PDF·docx·xlsx + .hwpx/.hwp** 본문 추출. *크리티컬 패스* — 견적 항목 분해(굿즈 45% 등)는 견적서 *본문*에서 나옴. hwp 빠지면 한국 영업문서 절반 누락 → 풍부함이 메일요약 수준으로 주저앉음 |
| **Drive 검색** | Drive에 *쓰기*만(Companies/Minutes/Sources). 사용자 Drive에서 미팅 관련 문서를 *찾지 않음* | 공유폴더 + 본인 소유 + **sharedWithMe**(동료가 공유한 deck·첨부)에서 견적·제안·deck **검색** |
| **Gmail 깊이** | `search_recent_emails` snippet만 | 스레드 본문 읽기로 거래 흐름·수치 재구성 |
| **Slack 채널 맥락** | 없음 | 내부/biz 미팅용 **특정 채널 history**(예: #parasta_biz) — 자유서술 논의(PoC 상태·일정 변경)는 Trello 카드에 없음 |
| **탐색 방식** | 고정 파이프라인(웹→Gmail→연결점→정해진 섹션) | **에이전트 다중홉 루프** — 제목→Gmail스레드→거기 언급된 견적서 파일명→그 이름으로 drive_search→PDF/hwpx 읽기. 중간 결과로 다음 쿼리 결정 |
| **모델** | 동향 판정 Haiku, 합성 Sonnet | 합성/탐색·**동명타사 판정에 capable 모델** |

## 2. 핵심 원칙 — "껍데기 유지, 두뇌만 교체"

Claude-in-Slack이 **못 하는 것** = 봇의 존재 이유 → **그대로 유지**:
- 자동 스케줄(09:00 브리핑), 트랜스크립트 폴링→회의록 자동생성, "🛑 미팅종료" 버튼, 팀 전체 사용자별 토큰 관리, 영속 상태.

**교체 대상은 "리서치 스텝" 하나**: 브리핑 `_run_briefing_research` + 온디맨드 `_post_company_research_result`가 호출하는 리서치 엔진(`run_company_research`)을 고정 파이프라인 → **에이전트 루프**로.

## 3. 아키텍처

```
[껍데기·불변] 스케줄러 / 회의록 / 버튼 / 토큰 / 세션
        │ 미팅(또는 업체명) + 요청 사용자
        ▼
[에이전트 리서치 엔진]  agents/research_agent.py (신규)
  Claude tool-use 루프 (capable 모델)
  ├─ 도구(전부 요청 사용자 OAuth, READ-ONLY):
  │   • gmail_search / gmail_read_thread   (tools/gmail.py — 스레드 본문 읽기 추가)
  │   • drive_search / drive_read          (공유폴더+본인+sharedWithMe, hwpx/hwp 추출)
  │   • slack_channel_history              (allowlist biz 채널, conversations.history)
  │   • trello_lookup                      (tools/trello.py 기존)
  │   • web_search / ontology_lookup       (기존)
  │   ※ 다중홉: 중간 결과의 파일명·회사명을 다음 검색 쿼리에 사용(프롬프트 명시)
  ├─ 도구 호출 예산 상한 + 타임아웃
  └─ 종료: structured output으로 CompanyResearch(확장) 제출
        │ CompanyResearch 객체 (단일 진실)
        ▼
[그라운딩 검증]  주장↔출처 매칭 critic (Haiku) — 출처 없는 주장 강등/제거
        ▼
[렌더/저장]  기존 구조화 경로 재사용 (스트랭글러 0~3 산출물)
   실패/타임아웃/예산초과 → 기존 파이프라인 폴백(best-effort)
```

**기존 스트랭글러와의 관계:** `CompanyResearch`가 에이전트의 **출력 스키마**. 스테이지 0~3(객체·단일파서·렌더·judge)이 이 엔진의 렌더/저장 레이어로 그대로 쓰임 → **버린 작업 아님**. 에이전트는 `run_company_research`의 *내부*를 교체.

## 4. 도구 surface (read-only, 사용자별 OAuth)

| 도구 | 동작 | 비고 |
|---|---|---|
| `gmail_search(query)` | 메일 검색 → 헤더·snippet 목록 | 기존 확장 |
| `gmail_read_thread(thread_id)` | 스레드 본문 읽기 | **신규** — 거래 흐름·수치. 선별 읽기(아래) |
| `drive_search(query)` | 공유폴더+본인+sharedWithMe 검색 → 파일 목록 | **신규·범위 한정** |
| `drive_read(file_id)` | 파일 본문 추출(PDF·docx·xlsx·**hwpx/hwp**) | **신규** — 한글 추출 포함(크리티컬) |
| `slack_channel_history(channel)` | **allowlist된 biz 채널** 최근 메시지 | **신규** — 봇 멤버 채널만, `channels:history`(전역 search 아님) |
| `trello_lookup(company)` | 업체 파이프라인 카드(체크리스트·코멘트) | **기존 `tools/trello.py` 노출** |
| `web_search(query)` | 웹 검색 | 기존 |
| `ontology_lookup(name)` | lib-mesh 엔티티·문서 | 기존, 게이팅 사용자 |

**데이터 소스(우선순위):** ① **공유 영업/제안 폴더(못박은 ID)** → ② 본인 Drive + **sharedWithMe**(동료 공유 deck·첨부) → ③ Gmail → ④ Trello 카드 → ⑤ **Slack biz 채널 history**(내부/biz 미팅) → ⑥ 웹+온톨로지. 비면 graceful 스킵.

**Drive 범위:** `drive_search`는 **(a) 공유 영업/제안 폴더**(env `DRIVE_RESEARCH_FOLDER_ID`, 팀 공유) 재귀 + **(b) 본인 소유**(`'me' in owners`) + **(c) sharedWithMe**(동료가 공유한 문서 — BD 맥락의 핵심, 이미 접근권 있음). 결과는 관련도·최신순 랭킹해 상위만(노이즈 제어). 사용자별 폴더 설정 없음(공유폴더 ID 고정).

**Slack 범위:** 전역 `search:read`(어려움·노이즈) **제외**. 대신 **봇이 초대된 biz 채널 allowlist**(env `SLACK_BIZ_CHANNELS`)에서 `conversations.history`로 자유서술 논의(PoC 상태·일정 변경 등 Trello에 없는 것)를 읽음. 내부 미팅 브리핑의 핵심 소스. 셋업: 봇을 해당 채널 초대 + `channels:history`/`groups:history` 스코프.

**gmail_read_thread 선별:** N개 스레드를 전부 풀로 읽지 말고 snippet으로 랭크 → 상위 2~3개만 본문(예산 절약 + 조기종료 방지 균형).

## 5. 출력 스키마 (CompanyResearch 확장)

기존 필드 유지 + 추가:
- `summary_line: str` — "한 줄 요약"(이 미팅이 뭔지)
- `deal_context: str` — 거래/관계 진행 흐름(RFQ→견적→확정 등, prose)
- `source_docs: list[{title, url, why}]` — 근거 자료 링크(견적서·deck 등)
- `attendees: list[{name, role, contact, note}]` — 참석자·연락처
- `talking_points: list[str]` — "오늘 논의 포인트". **retrieval이 아니라 synthesis 산출** — 조립된 전체 컨텍스트를 보고 생성("굿즈 45%"+"헤이데이 경쟁견적 존재"는 조합에서 나옴). news item만 보고 만들면 얕아짐.

각 `NewsItem`·주장은 가능한 한 `source`(gmail/drive/slack/web URL) 보유 → 그라운딩 대상.

## 6. 품질 게이트 — critic 3종 (축이 다름)

1. **URL 그라운딩 (Haiku, 기계적)** — 각 주장이 도구가 실제 반환한 출처에 근거하는지 → 미근거 주장 제거/강등. (`ontology_synth`의 R3 패턴 재사용)
2. **엔티티 동일성 = 동명타사 (capable 모델, 의미 판단)** — "디안트보르트=마케팅사 ✓ / 남아공 밴드 ✗", "komsa=해양교통안전공단 ✓ / 독일 KOMSA AG ✗". **정확성 최고가치 체크이자 제일 어려움** → Haiku 불가, 합성 모델 책임. 프롬프트 명시 + 제출 시 회사 동일성 확정.
3. **커버리지 critic (조기종료 방지)** — 예산상한은 *과탐색*만 막음. 반대 실패모드 = 얕음(Gmail만 보고 제출). 제출 전 "안 들른 소스 있나, 거기 관련 정보 있을 법한가" 1회 질문 → 있으면 추가 탐색. grounding(정확성)과 **별개 축(완전성)**.

## 7. 가드레일 / 폴백

- **도구 호출 예산** 상한(미팅당 N회) + **타임아웃**. 단, §6-3 커버리지 critic이 *조기종료*를 막아 균형(예산은 상한, critic은 하한).
- **콜드(신규) 외부 미팅 graceful degrade**: Gmail/Drive 이력 0인 첫 미팅에서 출력이 "비어보이게" penalize되지 않도록 — web+ontology만으로도 정상 렌더(이력 없음은 결함 아님).
- **폴백**: 에이전트 실패·타임아웃·예산초과 → 기존 고정 파이프라인(`run_company_research` 레거시 경로 보존). best-effort 원칙 유지.
- **read-only**: 리서치 중 쓰기 도구 없음(Drive 위키 저장은 리서치 *후* 별도).
- **플래그**: `AGENTIC_RESEARCH` env로 on/off(킬스위치), 기본 off로 배포 → 검증 후 on.

## 8. 롤아웃 (품질 도달 우선)

1. **온디맨드 먼저** (`{업체} 리서치` / `이 미팅 브리핑`) — 피드백 루프 최단, "이제 됐다" 할 때까지 반복 튜닝.
2. 안정 후 **스케줄 브리핑**을 엔진으로 전환(이때 비용·지연 최적화: 외부 미팅만, 캐시, 예산 조정).

## 9. 모델·비용 posture

- 합성/탐색·**동명타사 판정**: capable 모델(Sonnet 기본, 품질 미달 시 Opus). **URL 그라운딩 critic만 Haiku**(기계적). 커버리지 critic은 합성 모델 맥락에서.
- 비용은 **온디맨드 단계에서 실측** 후 스케줄 전환 시 조임(예산/캐시/외부미팅 한정).

## 10. 비목표 (YAGNI)

- 회의록(during/after)·스케줄러·버튼·토큰관리·OAuth·Dreamplus **손대지 않음**. Trello는 **읽기(get_card_context)만 도구로 노출** — 쓰기/등록 흐름은 불변.
- 스트랭글러 폐기 아님 — 렌더/저장 레이어로 흡수. 단계4(레거시 추출기 제거)는 폴백 안정 후.
- **Slack 전역 검색(`search:read`) 비포함** — 어려움·노이즈. 단 **allowlist 채널 history는 v1 포함**(§4).
- Drive: 공유폴더+본인+sharedWithMe만(무차별 타인문서·공유 온톨로지 적재 안 함 — 범위 통제).

## 11. v1 검증 항목 (못박은 크리티컬) + 설정

**첫 실측 미팅에서 실제로 채워지는지 확인 후 스케줄로 넘어감:**
1. **hwpx/hwp 추출** — 한글 견적서/SOW 본문이 실제로 추출돼 항목분해까지 나오는지. (`.hwpx`=zip+XML 추출 가능, 레거시 `.hwp`=바이너리→라이브러리 필요. 플랜에서 분리.)
2. **내부 미팅 = Slack 채널 history** — allowlist 채널에서 자유서술 맥락(PoC 상태·일정)이 실제로 잡히는지. Trello가 *대체 못 함*을 실미팅으로 확인.
3. **Drive sharedWithMe** — 동료 공유 deck·첨부(이사장 보고 등)가 후보에 잡히는지. 커버리지 구멍 실측.
4. **동명타사를 capable 모델로** — 디안트보르트·komsa류 오인 0건.

**설정(`.env`, 리포 커밋 금지):**
- `DRIVE_RESEARCH_FOLDER_ID` — 공유 영업/제안 폴더(사용자 제공).
- `SLACK_BIZ_CHANNELS` — biz 채널 ID allowlist + 봇 초대 + `channels:history`/`groups:history` 스코프.
- 도구 호출 예산 기본값(미팅당 N) — 온디맨드 실측 후 확정.

---

**다음:** 이 스펙 리뷰 → 확정되면 구현 계획(writing-plans)으로 온디맨드 1차 증분부터 태스크화. v1은 위 4개 검증항목을 게이트로.
