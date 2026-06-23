# 브리핑 온톨로지 티어 — 목적인지 라이트 합성 (설계)

> 선행: 온톨로지 read 통합(#46)·딥 리서치(#51)·출력 포맷 하네스(#52). 본 스펙은 **브리핑** ④이전맥락의 온톨로지를 원시 cluster → **"최근 상황 2~3줄" 프로즈**로. 딥(research)과 달리 **가볍게**(브리핑은 전 미팅 일괄이라 비용 민감).

## 1. 목표

미팅 제목의 목적에 맞춘 **최근 상황** 요약을 브리핑에 주입. 예: "Komsa 마케팅 진행협의" → "KOMSA 홍보예산 턴키 협의 중(2026-02), 제안서 수주 확정(6/9)" 수준. 외부 참석자 미팅이력(#47)은 유지.

## 2. 확정 사실 (라이브 프로브, 2026-06-23)

- **신뢰 retrieval = `document_search(query="{업체} {제목초점}", seed_entity=slug, mode=hybrid, sort_by=score, top_k=8)` + R1 필터.**
  - **질의에 업체명 포함**이 핵심: seed_entity 부스트만으론 스코핑 약함. 업체명을 token 질의에 넣으면 상위가 해당 업체 문서로 정렬됨(검증: 상위 4건 hop=0, 스니펫 "우선협상대상자 선정 완료"·"MTIS 앱 공지·배너로 전자증서 발급 유도"·"홍보 목적은 인지도 제고…").
  - **`sort_by=score`(관련도)** 사용. **`sort_by=recent`는 타사 누수**(상호운용·HAVAH 마케팅 등 hop=None/2) → 금지.
  - **R1 필터**: `min_hop==0` 또는 `seed_slug in matched_via_entities`인 결과만. 타사 컷.
  - 결과에 **`snippet`(본문 일부)** 포함 → `document_fetch` 불필요(딥과의 비용 차이 핵심).
- 응답 `data.results[]` = `{title, snippet, source_uri, ym, min_hop, matched_via_entities}`.

## 3. 설계 — 라이트 경로

### 3.1 retrieval (`tools/ontology.py`)
- `recent_company_docs(user_id, company, focus_query, max_docs=4)` 신규:
  - 토큰 없으면 None. `entity_find`→slug. slug 없으면 `{slug:None, docs:[]}`.
  - `document_search(query=f"{company} {focus_query}".strip(), seed_entity=slug, mode="hybrid", sort_by="score", top_k=8)`.
  - **R1 필터**: `r.get("min_hop")==0 or slug in (r.get("matched_via_entities") or [])`.
  - 상위 `max_docs`건 → `{slug, docs:[{title, snippet, uri, ym}]}`(snippet 빈 것 제외).

### 3.2 라이트 합성 (`agents/ontology_synth.py`)
- `synthesize_recent_situation(company, docs)` 신규 — **Haiku**(딥은 Sonnet, 브리핑은 cheap). 프롬프트 `prompts/templates/ontology_recent.md`: "제공된 스니펫만 근거로 {업체} **최근 상황 2~3문장**. 추측 금지, 날짜 있으면 포함, 핵심만." grounding 제약 동일. snippet 없으면 None. critic 없음(표면 작음·비용). best-effort → 실패 시 None.
- 반환: `{summary: str, docs: [{title, uri}]}`(프로즈 + 상위 문서 링크 2~3개) 또는 None.

### 3.3 묶음·게이팅 (`agents/before.py`)
- `briefing_ontology_summary(user_id, company, title)` 신규:
  - `_ontology_enabled(user_id)` 아니면 None.
  - focus_query = `title`에서 `_` 이후 잘라냄(`"Komsa 마케팅 진행협의_박종도대리"`→`"Komsa 마케팅 진행협의"`). (업체명은 §3.1에서 별도 prepend되므로 중복 무해.)
  - `ontology.recent_company_docs` → `ontology_synth.synthesize_recent_situation`. 예외/None → None.

### 3.4 브리핑 배선 (업그레이드형 폴백)
- `_run_briefing_research` ④블록: `summary = briefing_ontology_summary(user_id, company_name, meeting.get("summary",""))`.
  - 성공 → `context["ontology_recent"] = summary`(프로즈+링크). **구조화 cluster(`_company_ontology`) 호출 생략**(프로즈가 대체, 비용·중복 절약).
  - None → 기존 `_company_ontology`(구조화, #52로 정리됨) 폴백. (ont 다운/미등록 → 그것도 None → 섹션 생략.)

### 3.5 렌더 (`tools/slack_tools.build_context_block`)
- `context.get("ontology_recent")` 있으면 🔗 온톨로지(사내 지식) 아래 **프로즈 + 문서 링크**(`<uri|title>`) 렌더.
- 없고 `context.get("ontology")`(구조화) 있으면 기존 렌더(#52). 둘 다 없으면 섹션 없음.

## 4. 비용 / 원칙
- 브리핑 미팅당 온톨로지 = `entity_find`+`document_search`+Haiku 1회(문서 본문 fetch 없음). 딥(research, Sonnet+critic+N fetch)보다 훨씬 쌈 → 09시 일괄에 적합.
- 게이팅 `_ontology_enabled` 재사용(현재 본인만, ont 다운/실패 폴백).
- 합성문 **위키 미저장**(오염 방지).

## 5. 테스트
- `recent_company_docs`: R1 필터(hop>0·미연결 제거), 업체명 prepend 질의, snippet 빈 것 제외(httpx MockTransport).
- `synthesize_recent_situation`: Haiku 호출·grounding 프롬프트·snippet 없으면 None·실패 None(mock LLM).
- `briefing_ontology_summary`: 게이팅·focus_query(`_` 절단)·실패 None.
- `build_context_block`: `ontology_recent` 프로즈+링크 렌더, 없으면 구조화 폴백.
- 회귀: 기존 온톨로지/브리핑 테스트.

## 6. 파일 변경 요약
| 파일 | 변경 |
|---|---|
| `tools/ontology.py` | `recent_company_docs`(search+R1) |
| `agents/ontology_synth.py` | `synthesize_recent_situation`(Haiku 라이트) |
| `prompts/templates/ontology_recent.md` | 신규 라이트 합성 프롬프트 |
| `agents/before.py` | `briefing_ontology_summary` + ④블록 배선(프로즈 우선/구조화 폴백) |
| `tools/slack_tools.py` | `build_context_block` 프로즈 렌더 분기 |
| `tests/…` | §5 |
| `CLAUDE.md` | 브리핑 티어 한 줄 |

## 7. 위험 / 롤아웃
- 게이팅(본인만). 프로즈 실패→구조화→생략 3단 폴백이라 브리핑 안 깨짐. ont 다운 안전.
- 배포 후 라이브 브리핑으로 육안 확인.

## 비목표 (YAGNI)
- 외부 참석자 "지난 논의" **내용** 합성(현재 미팅이력 제목만 #47 — 후속). 회의록 검색/목록 통합·에러 톤(후속). `document_fetch` 본문(브리핑은 snippet으로 충분).
