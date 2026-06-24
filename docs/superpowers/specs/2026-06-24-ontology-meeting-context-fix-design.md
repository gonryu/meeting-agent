# 온톨로지 미팅 맥락 정합성 (E) — 클리핑 누수·critic 누수·media 미팅로그

> 근거: 이데일리 라이브 딥 리서치에서 ADB 기사 클리핑이 "사내 지식"으로 새고, "# 교정된 브리핑" 헤더 노출. 데이터 진단으로 원인 확정.

## 근본 원인 (라이브 데이터 확인)
1. **person 혼동**: `이정훈 기자`(이데일리)↔`이정훈 CSO`(파라메타)가 엮여, CSO의 ADB 발표 기사가 이데일리 cluster에 person 경유로 포함.
2. **R1 폴백 버그**: `company_research_sources`가 회사-직접연결 0개일 때 `pool = connected or norm["documents"]`로 **person-경유 클리핑 전체 사용**. (이데일리 depth=2+time_range → 직접연결 0 → 폴백 → ADB 요약)
3. **critic 누수**: `ontology_grounding_check.md`가 "교정된 브리핑을 출력" → LLM이 `# 교정된 브리핑` 머리말을 본문에 포함.

## 결정 (사용자)
- Q1=(가): **media 동향은 비움/해당없음**(기존 C 유지, 뉴스 스킵).
- Q2: **media의 사내 맥락 = 우리 미팅 로그(인터뷰)만**(클리핑 배제).

## 수정

### E1 — R1 폴백 제거 (전 업체)
`tools/ontology.company_research_sources`: `pool = connected or norm["documents"]` → **`pool = connected`**(회사 직접연결만). 직접연결 0개면 빈 docs(잘못된 클리핑보다 없음이 낫다). KOMSA는 직접연결 7개라 영향 없음.

### E2 — critic 누수 수정
`prompts/templates/ontology_grounding_check.md`: "교정된 마크다운만 출력하라. **제목·머리말(예: '# 교정된 브리핑')·설명을 절대 붙이지 마라.** 교정된 본문 그 자체만." 명시.

### E3 — media → 미팅 로그 (클리핑 배제)
- `tools/ontology.company_meeting_docs(user_id, company, max_docs=4)` 신규: `document_search(query=company, seed_entity=slug, sort_by=score)` → **회사-직접연결(min_hop==0 또는 slug∈matched_via)** AND **미팅 로그 제목**(`_is_meeting_log_title`)만. snippet 있는 것. (cluster+time_range가 인터뷰를 놓치므로 document_search 경로 사용.)
- `_is_meeting_log_title(title)`: **미팅 키워드 필수**(회의|미팅|인터뷰|간담회|회의록|논의|미팅로그|워크숍). 날짜만 있는 클리핑("20260213_파라메타,…")은 키워드 없어 제외. (`_is_meeting_title`의 날짜-매칭보다 엄격.)
- `agents/before.deep_company_ontology(user_id, company, is_media=False)`: **is_media면 `company_meeting_docs` → `synthesize_recent_situation`(라이트, 미팅 맥락 프로즈)**, 아니면 기존 `company_research_sources` → `synthesize_company_brief`(딥). media는 미팅 로그만 → "이정훈 기자와 2026-01 인터뷰: …".
- `main._post_company_research_result`: 전달된 `content`(wiki) frontmatter에서 `company_type` 파싱 → `is_media = (company_type=="media")` → `deep_company_ontology(..., is_media=is_media)`.

## 아키텍처/원칙
- 회사-직접연결만(person 혼동·클리핑 차단)이 핵심 불변식. media는 미팅 로그로 한정.
- 게이팅·best-effort 폴백 유지. 위키 미저장.

## 테스트
- E1: company_research_sources 연결0이면 docs=[](폴백 안 함). (mock cluster: 전부 person-경유 → []).
- E2: grounding_check 프롬프트에 머리말 금지 문구 존재(텍스트 검증) — 누수는 라이브 회귀.
- E3: `_is_meeting_log_title` — "260129 … 인터뷰" True, "20260213_파라메타, ADB…" False(키워드 없음), "회의록" True. `company_meeting_docs` 회사-직접+미팅키워드 필터(mock). `deep_company_ontology` is_media True→meeting 경로(mock), False→deep 경로.

## 파일 변경
| 파일 | 변경 |
|---|---|
| `tools/ontology.py` | E1 폴백 제거, `_is_meeting_log_title`·`company_meeting_docs` 신규 |
| `prompts/templates/ontology_grounding_check.md` | E2 머리말 금지 |
| `agents/before.py` | `deep_company_ontology(is_media=)` media 분기 |
| `main.py` | company_type 파싱 → is_media 전달 |
| `tests/test_ontology_meeting_fix.py` | 위 |

## 비목표
- person 엔티티 disambiguation(온톨로지 측 데이터 이슈, 우리 회피만). media 동향에 보도이력 표시(Q1=가로 제외). investor 등 다른 타입.
