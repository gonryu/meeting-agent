# 온톨로지 합성 하네스 — 딥 리서치 + 품질관리 (설계)

> 선행: `2026-06-21-ontology-read-integration-design.md`(read 통합, 배포·라이브 검증 완료).
> 본 문서는 그 위에 **합성 품질**을 올린다 — 원시 cluster 덤프 → 출처 기반 자연어 브리핑.

## 1. 배경 / 문제

현재 온톨로지 출력은 `entity_cluster` 한 번의 **원시 덤프**(`part-of: Enterprise`, 날것 파일명)라 사람이 못 읽는다. 라이브 검증에서 ont 자체 AI는 문서 본문(제안서 266억·KPI·회의록)을 읽어 풍부한 리포트를 내는 걸 확인했고, 우리 MCP는 **retrieval 7종(에이전트 도구 없음)**이라 그 깊이는 **우리 하네스가 직접** `cluster → document_fetch → 합성`으로 만들어야 한다(라이브 프로브로 `document_fetch`가 제안서/회의록 본문을 반환함을 검증).

**품질 리스크(핵심):** 검색결과 위 합성은 ① 환각(출처에 없는 사실) ② 타사 노이즈(seed 부스트 약함 — KISA배터리/HAVAH 마케팅 누수 관측) ③ 누락. 회의에 들어가는 브리핑이라 사실오류는 치명적.

## 2. 2-티어 (사용자 승인)

| 티어 | 트리거 | 깊이 | 비용 |
|---|---|---|---|
| **딥 리서치** | 온디맨드 "{업체} 리서치" | cluster + 상위 문서 N개 본문 `document_fetch` + Sonnet 합성 + grounding critic | 높음(요청당), 사용자가 기다림 |
| **브리핑** (별도 증분) | 미팅 5분전/09시 | 제목→초점 `document_search` + person_context + 라이트 합성 | 낮음, 빠름 |

**본 스펙의 구현 범위 = 딥 리서치 티어 + 품질 하네스 + golden eval.** 브리핑 티어는 §7에 설계만 두고 다음 증분.

## 3. 품질 하네스 (사용자 요구 — "하네스/에이전트로 품질관리")

**구분: 봇 런타임(가벼움) vs 개발타임(무거움).** Workflow 다중에이전트는 *개발·측정*용이지 봇 요청경로에 못 넣는다.

### 3.1 런타임 (요청마다, 경량)
- **R1 relevance 필터** — cluster/search 결과에서 **업체 엔티티에 직접 연결된 것(min_hop=0 또는 matched_via_entities에 seed 포함)만** 합성 입력으로. 타사 노이즈 제거. LLM 없음.
- **R2 grounding 제약 프롬프트** — 합성 프롬프트에 "제공된 출처 스니펫에 명시된 사실만 사용. 추론·추측 금지. 각 핵심수치/주장 뒤 `[출처: 문서명]`. 출처에 없으면 쓰지 말 것." 강제.
- **R3 grounding critic (딥 리서치만, 1패스)** — 합성 결과 + 원본 스니펫을 받아 **각 사실 주장이 출처에 있는지** 판정하는 검증 LLM 1회. 미지지 주장은 제거/플래그. (adversarial-verify 1인 — harness-100 research-assistant 비평 구조.) best-effort: critic 실패 시 R2 결과 그대로 통과(절대 빈 답 강제 안 함).

### 3.2 개발타임 (골든셋 eval — `news_relevance` 패턴 차용)
- **D1 golden eval** `tests/eval_ontology_grounding.py` — 케이스 = {company, source_snippets[], synthesized_brief}. 측정: **(a) 환각률**(브리핑의 사실 주장 중 출처 미지지 비율), **(b) 누락**(출처의 핵심 항목 중 누락). LLM-as-judge로 채점, 골든셋 `tests/golden/ontology_grounding.jsonl`. `eval_news_relevance.py`의 oracle/stub/sonnet 모드·임계 패턴 그대로.

## 4. 아키텍처 — 모듈 책임

- `tools/ontology.py` (retrieval 전용, LLM 없음):
  - `document_fetch(user_id, document_id, level="summary", max_chars=3000)` 신규 — `OntologyClient.call_tool("document_fetch", …)` 래핑, `data.body_markdown`·`source_uri`·`space_display` 반환.
  - `_normalize_cluster` 보강 — 문서에 `source_uri`·`space_display`·`ym`(날짜)·`matched_via_entities` 포함(현재 title만 → 링크·날짜·연결 보존).
  - `company_research_sources(user_id, company, max_docs=6)` 신규 — cluster → R1 필터 → 상위 문서 선별(우선순위: 제안서·계약·회의록 > 주간보고; 최신 우선) → 각 `document_fetch` → `{relations[], docs:[{title, summary, uri, space, ym}]}` 반환. **순수 retrieval + 필터(R1)**, 합성 없음.
- `agents/ontology_synth.py` (신규, LLM 합성 — before.py 비대화 방지 위해 분리):
  - `synthesize_company_brief(company, sources)` — R2 프롬프트로 Sonnet 합성 → R3 critic → 최종 마크다운. 프롬프트 템플릿 `prompts/templates/ontology_brief.md`·`ontology_grounding_check.md`(핫리로드).
  - best-effort: 합성/critic 실패 시 사유 로그 + 구조화 폴백(현 cluster 렌더).
- `agents/before.py`:
  - `_company_ontology`는 유지(라이트). 신규 `deep_company_ontology(user_id, company)` = `ontology.company_research_sources` → `ontology_synth.synthesize_company_brief`. 게이팅 `_ontology_enabled`.
- `main.py`:
  - `research_company` 결과 포스팅(`_post_company_research_result`)에서 게이팅 사용자면 **라이트 cluster 대신 딥 브리핑** 렌더. (현 `_company_ontology` → `deep_company_ontology`로 교체, 실패 시 라이트 폴백.)

## 5. 캐시 / 오염

- 딥 브리핑은 **온디맨드**라 기본 캐시 없음(요청 시 fresh). 
- **절대 Drive 업체 위키에 합성문 저장 금지**(온톨로지 Drive 재크롤 → 합성문 재색인 오염). 필요 시 DB 단기 캐시만(본 스펙 범위 밖).

## 6. 게이팅 / 안전

- `_ontology_enabled`(기존: 토큰 필수 + GA/allowlist) 재사용. 토큰 없음/비활성/실패 → 기존 라이트 또는 비온톨로지 경로 폴백. 브리핑 안 깨짐.
- read-only. 비용: 딥 리서치 1회 = cluster 1 + document_fetch N(≤6) + Sonnet 합성 1 + critic 1. 온디맨드라 허용.

## 7. 브리핑 티어 (설계만 — 다음 증분)

- 제목 파싱 → 초점 키워드(업체명·일반어[협의/미팅/회의/진행] 제거). 
- 초점 있으면 `document_search(query=focus, seed_entity=company, sort_by=recent)` + R1 필터; 없으면 `entity_cluster(recent time_range)`.
- 외부 참석자 → `person_context`(기존) + 그 사람 참여 문서로 "지난 논의" 라이트.
- 라이트 합성(R2만, critic 없음 — 표면 작음). 출력 "최근 상황 2~3줄 + 이 사람과 지난 논의".

## 8. 테스트

- `tools/ontology.py`: `document_fetch` 파싱(body_markdown/uri); `_normalize_cluster` 보강 필드; `company_research_sources` R1 필터(타사 hop>0 제거)·문서 우선순위·fetch 묶음(httpx MockTransport).
- `agents/ontology_synth.py`: 합성 프롬프트 구성; critic이 미지지 주장 제거(mock LLM); 실패 시 폴백.
- `agents/before.deep_company_ontology`: 게이팅·실패 폴백.
- `eval_ontology_grounding.py`: oracle 모드 sanity + 골든셋 로드.

## 9. 파일 변경 요약

| 파일 | 변경 |
|---|---|
| `tools/ontology.py` | `document_fetch`, `_normalize_cluster` 보강, `company_research_sources`(+R1) |
| `agents/ontology_synth.py` | 신규 — 합성 + grounding critic |
| `prompts/templates/ontology_brief.md`, `ontology_grounding_check.md` | 신규 프롬프트 |
| `agents/before.py` | `deep_company_ontology` |
| `main.py` | `_post_company_research_result`에서 딥 브리핑 사용(게이팅·폴백) |
| `tests/…`, `tests/eval_ontology_grounding.py`, `tests/golden/ontology_grounding.jsonl` | §8 |
| `CLAUDE.md` | 온톨로지 절에 딥 리서치·품질 하네스 반영 |

## 비목표 (YAGNI)

- 봇 런타임에 다중에이전트 판정 패널(개발타임 Workflow로만).
- 브리핑 티어 구현(다음 증분).
- 합성문 영구 캐시/되먹임(오염 방지).
- `document_fetch` original(전문) — summary로 충분, 비용↑ 회피.
