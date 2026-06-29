# 에이전트형 리서치 엔진 설계 (Agentic Research Engine)

작성일 2026-06-29 · 설계 확정 전 리뷰용

## 0. 북극성

**"제대로 나오게 한다"** — 풍부하고(실물 자료 기반) + 정확하게(환각·동명타사 없음). 비용·범위는 품질 도달 *후에* 조이는 2차 관심사.

기준점: Claude-in-Slack이 Google Drive MCP + Gmail로 만들어낸 브리핑(실제 견적서 PDF 항목·할인·유효기한, RFQ→견적→확정 메일 흐름, 참석자 연락처, "오늘 논의 포인트"까지). 우리 봇이 이 수준을 내야 한다.

## 1. 진단 — 왜 현재 봇은 얕은가

| 격차 | 현재 | 필요 |
|---|---|---|
| **Drive 검색** | Drive에 *쓰기*만(Companies/Minutes/Sources). 사용자 Drive에서 미팅 관련 문서를 *찾지 않음* | 영업/제안 폴더에서 견적·제안·deck **검색** |
| **Gmail 깊이** | `search_recent_emails` snippet만 | 스레드 본문 읽기로 거래 흐름·수치 재구성 |
| **탐색 방식** | 고정 파이프라인(웹→Gmail→연결점→정해진 섹션) | **에이전트 루프** — 제목→업체→메일스레드→첨부PDF→deck 단서 추적·교차참조 |
| **모델** | 동향 판정 Haiku, 합성 Sonnet | 합성/탐색에 유능 모델 |

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
  │   • gmail_search / gmail_read_thread   (tools/gmail.py — 스레드 읽기 추가)
  │   • drive_search / drive_read          (tools/drive.py — 영업/제안 폴더 한정 검색 추가)
  │   • web_search                         (기존)
  │   • ontology_lookup                    (tools/ontology.py 기존)
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
| `gmail_read_thread(thread_id)` | 스레드 본문 읽기 | **신규** — 거래 흐름·수치 |
| `drive_search(query)` | **영업/제안 상위폴더 하위** 검색 → 파일 목록 | **신규·범위 한정** |
| `drive_read(file_id)` | 파일 본문/추출 텍스트(PDF·docx·xlsx) | **신규** |
| `trello_lookup(company)` | 업체 파이프라인 카드(체크리스트·코멘트) | **기존 `tools/trello.py` 노출** — 내부 거래단계/액션 맥락 |
| `web_search(query)` | 웹 검색 | 기존 |
| `ontology_lookup(name)` | lib-mesh 엔티티·문서 | 기존, 게이팅 사용자 |

**데이터 소스 5층(우선순위):** ① **공유 영업/제안 폴더(못박은 ID)** → ② 사용자 본인 Drive → ③ Gmail → ④ Trello(업체 카드) → ⑤ 웹+온톨로지. 비어있으면 graceful 스킵.

**Drive 범위:** `drive_search`는 **(a) 공유 영업/제안 폴더**(env `DRIVE_RESEARCH_FOLDER_ID`, 팀 공유·전 사용자 접근) 재귀 + **(b) 요청 사용자 본인 소유 파일**(`'me' in owners`). 둘 다 사용자 OAuth로 접근. 공유폴더 ID는 `.env`에 고정(사용자별 폴더 설정 없음 — 각자 Drive 관리가 달라 못박는 게 안전). 본인 Drive가 비어도(미사용자) 공유폴더+Gmail+Trello로 진행.

**Slack은 v1 비포함**(§10) — 대화 검색은 user-token `search:read` 필요·노이즈 큼. 같은 내부 맥락을 Trello가 구조화로 제공하므로 2차.

## 5. 출력 스키마 (CompanyResearch 확장)

기존 필드 유지 + 추가:
- `summary_line: str` — "한 줄 요약"(이 미팅이 뭔지)
- `deal_context: str` — 거래/관계 진행 흐름(RFQ→견적→확정 등, prose)
- `source_docs: list[{title, url, why}]` — 근거 자료 링크(견적서·deck 등)
- `attendees: list[{name, role, contact, note}]` — 참석자·연락처
- `talking_points: list[str]` — "오늘 논의 포인트"

각 `NewsItem`·주장은 가능한 한 `source`(gmail/drive/web URL) 보유 → 그라운딩 대상.

## 6. 그라운딩 검증 (정확성 게이트)

- 합성 후 critic 패스(`ontology_synth.synthesize_company_brief`의 R3 grounding critic 패턴 재사용): 각 주장이 도구가 실제 반환한 출처에 근거하는지 확인 → 미근거 주장 제거/강등.
- 동명 타사 배제: 에이전트 프롬프트에 명시 + 검증 단계에서 회사 동일성 확인.
- 모든 외부 주장에 출처 표기.

## 7. 가드레일 / 폴백

- **도구 호출 예산** 상한(미팅당 N회) + **타임아웃**. 초과 시 현재까지 수집분으로 합성 or 폴백.
- **폴백**: 에이전트 실패·타임아웃·예산초과 → 기존 고정 파이프라인(`run_company_research` 레거시 경로 보존). best-effort 원칙 유지.
- **read-only**: 리서치 중 쓰기 도구 없음(Drive 위키 저장은 리서치 *후* 별도).
- **플래그**: `AGENTIC_RESEARCH` env로 on/off(킬스위치), 기본 off로 배포 → 검증 후 on.

## 8. 롤아웃 (품질 도달 우선)

1. **온디맨드 먼저** (`{업체} 리서치` / `이 미팅 브리핑`) — 피드백 루프 최단, "이제 됐다" 할 때까지 반복 튜닝.
2. 안정 후 **스케줄 브리핑**을 엔진으로 전환(이때 비용·지연 최적화: 외부 미팅만, 캐시, 예산 조정).

## 9. 모델·비용 posture

- 합성/탐색: capable 모델(Sonnet 기본, 품질 미달 시 Opus 검토). 그라운딩 critic: Haiku.
- 비용은 **온디맨드 단계에서 실측** 후 스케줄 전환 시 조임(예산/캐시/외부미팅 한정).

## 10. 비목표 (YAGNI)

- 회의록(during/after)·스케줄러·버튼·토큰관리·OAuth·Dreamplus **손대지 않음**. Trello는 **읽기(get_card_context)만 도구로 노출** — 쓰기/등록 흐름은 불변.
- 스트랭글러 폐기 아님 — 렌더/저장 레이어로 흡수. 단계4(레거시 추출기 제거)는 폴백 안정 후.
- **Slack 대화 검색 v1 비포함** — user-token `search:read` 필요·노이즈. 2차.
- Drive: 공유폴더+본인 소유만(타인 공유문서 무차별 검색·공유 온톨로지 적재 안 함 — 프라이버시·범위 통제).

## 11. 설정·오픈 이슈

- ✅ **Drive 폴더(확정)**: 공유 영업/제안 폴더 ID를 `.env` `DRIVE_RESEARCH_FOLDER_ID`에 고정(사용자가 제공) + 사용자 본인 소유 파일. 사용자별 폴더 설정 없음.
- 도구 호출 예산 기본값(미팅당 N) 실측 후 확정.
- 첨부 PDF/xlsx 텍스트 추출 경로(기존 문서 업로드 추출 재사용 가능 여부).
- (참고) `DRIVE_RESEARCH_FOLDER_ID` 값은 라이브 `.env`에만 — 리포 커밋 금지.

---

**다음:** 이 스펙 리뷰 → 확정되면 구현 계획(writing-plans)으로 온디맨드 1차 증분부터 태스크화.
