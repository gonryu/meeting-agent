# 온톨로지(lib-mesh) read 통합 — 설계

> 사전 결정: `memory/data-architecture-pivot.md` (SSOT 폐기·사용자별 토큰·온톨로지 read).
> B(Q&A 관측)는 별개로 완료·배포됨. 본 문서는 **A: 브리핑의 내부 맥락을 사내 온톨로지 검색으로 채우는** 설계.

## 1. 목표

브리핑의 **내부 맥락** 칸(②파라메타 서비스 연결점 · ③인물 내부 접점 · **④이전 맥락(최대 수혜)**)을 사내 온톨로지 lib-mesh의 검색 결과로 채운다. 원칙: **읽기 = 온톨로지(RAG), 합성/추론 = 우리 Claude Sonnet.** ①헤더·②최근 뉴스는 안 건드림(캘린더·web_search 유지).

## 2. 확정 사실 (라이브 프로브로 검증, 2026-06-21)

- **엔드포인트**: `POST https://ont.parametacorp.com/mcp/` — **트레일링 슬래시 필수.** `/mcp`(슬래시 없음)는 `307`로 `/mcp/`에 리다이렉트하는데, httpx/requests가 리다이렉트를 따라가며 **Authorization 헤더를 드롭** → `401 missing bearer token`. **처음부터 `/mcp/`로 직타하고 리다이렉트를 따라가지 말 것.**
- **전송**: MCP Streamable-HTTP. JSON-RPC 2.0, `protocolVersion: 2025-06-18`. 응답 `Content-Type: application/json`(SSE 아님). 세션ID 없이 동작(stateless) 확인 — `Mcp-Session-Id` 있으면 부착, 없어도 OK.
- **인증**: 헤더 `Authorization: Bearer <JWT>` (HS256, `sub`=사용자 이메일, `exp`≈발급+30일, `scopes`=`spaces:*` 다수). 토큰은 ont의 **"MCP 설정 복사"** 블록(JSON)에 내장됨 — 별도 "토큰 발급" 화면이 아니라 MCP 설정을 통째로 복사하는 방식.
- **핸드셰이크**: `initialize` → `notifications/initialized`(notify) → `tools/call`.
- **도구 7개** (`tools/list` 결과):
  - **엔티티(그래프)**: `entity_find(name*, limit, scope)` · `entity_neighbors(slug*, hop, relation_types, direction, limit)` · `entity_path(from_*, to*, max_paths, max_length)` · **`entity_cluster(seed*, depth, include_documents, limit_entities, limit_documents, time_range, exclude_pattern, sources, …)`**
  - **문서**: `document_search(query*, mode[hybrid|token|vector|vector_graph], corpus[page|attachment|all], top_k, seed_entity, time_range, expand_alias, …)` · `document_related_entities(document_id*, limit)` · `document_fetch(document_id*, level[summary|original], max_chars)`
- **응답 형식**: `result.content[].text` 안에 JSON 문자열, **`{"data": {…}}` 봉투**. 예:
  - `entity_find` → `data.matches[]` = `{slug, title, category, tags, confidence, importance, sources_count, match_kind, space}`
  - `entity_cluster` → `data.{seed, entities[], documents[], …}`. `entities[]` = `{slug, hop, title, via}` (`via` = 관계타입: `part-of`/`related-to`/`uses`/…)
- **검증 데이터**: `entity_find("KOMSA")` → `entity/komsa`(exact, sources_count 10). `entity_cluster` → Enterprise·InfraTeam·KCA·KISA 공공과제·서울시청 블록체인 마이그레이션 등 풍부한 관계 그래프. (데이터 깊이 충분 확인.)

## 3. 브리핑 질의 패턴 (핵심)

**업체 컨텍스트** = `entity_find` → `entity_cluster`:
1. `entity_find(name=업체명, limit=5)` → `matches[]` 중 최선(`match_kind=exact` 우선, `confidence`·`importance`) slug 선택. alias·오타 보정됨.
2. `entity_cluster(seed=slug, depth=2, include_documents=True, limit_entities, limit_documents, time_range=…)` → `entities[]`(관계 `via`) + `documents[]`. = "협업·계약 이력 + 진행 이슈 + 관련 문서" 한 번에.
3. (선택) 상위 문서 `document_fetch(document_id, level="summary")` → 인용 스니펫.
4. `entities`(관계) + `documents`(+요약) → **우리 Sonnet이 ④이전맥락/②연결점 섹션으로 합성.**

**인물**: `entity_find(이름)` → `entity_neighbors`/`entity_cluster`로 내부 접점(전에 만났나·우리쪽 담당). 공개 프로필(LinkedIn·직책)은 기존 `web_search` 유지.

**키워드 본문**이 필요하면 `document_search(query, mode="hybrid", seed_entity=slug)`.

## 4. 3층 신선도

| 층 | 내용 | 도구·정책 |
|---|---|---|
| 안정 내부 | 회사 개요·산업 맥락 | `entity_cluster` 광역, **길게 캐시**(Drive wiki) |
| 변동 내부 | ④이전맥락·진행이슈·최근문서 | `entity_cluster`+`document_search`에 **최근 `time_range`**, **미팅 트리거로 라이브** |
| 외부 뉴스 | ②최근동향 | `web_search`+`news_relevance`, **온톨로지 밖**, 매번 라이브 |

되먹임은 **회의록만**(회의록이 Drive에 저장 → 온톨로지가 Drive 크롤 시 자동 흡수). 합성 리서치·뉴스는 온톨로지 색인 금지.

## 5. 모듈 — `tools/ontology.py` (신규)

- `_client_for_user(user_id)` — 사용자별 토큰 복호화 + (선택)세션 캐시. `tools/trello.py::_client_for_user` 패턴.
- `_call(token, method, params)` — Streamable-HTTP JSON-RPC. `/mcp/` **직타**(follow_redirects=False), 헤더 `Authorization`/`Content-Type`/`Accept`/`MCP-Protocol-Version: 2025-06-18`. 세션당 `initialize`+`initialized` 1회 후 `tools/call`. `httpx`(사내 방화벽 이슈 시 `verify=False` 폴백 가능 — 단 ont는 Cloudflare 정상 인증서). 타임아웃·예외.
- `_parse(result)` — `result.content[].text` → `json.loads` → `data` 봉투 반환.
- 고수준: `entity_find(token, name, limit=5)`, `entity_cluster(token, seed, depth=2, include_documents=True, time_range=None, …)`, `document_search(...)`, `document_fetch(...)`,
  그리고 브리핑용 묶음 **`company_context(user_id, company_name, recent=False)`** → find→cluster(+옵션 document_fetch) 실행 후 `{entities, relations, documents}` 정규화 반환. `recent=True`면 변동층용 최근 `time_range` 적용.
- 모든 호출 **best-effort**: 실패 시 예외를 호출부로 올리되, 브리핑 경로에서 잡아 섹션만 생략(§7).

## 6. 사용자별 토큰 등록 (PAT / 랜딩 붙여넣기)

- `store/user_store.py`: 컬럼 **`ontology_token_enc`** (Fernet). `save/get/clear_ontology_token(user_id)` — `trello_token_enc` 미러.
- 플로우 (`/trello` 패턴):
  1. `/온톨로지`(슬래시) 또는 자연어("온톨로지 등록") → Slack DM에 **짧은 등록 링크**(서버 리다이렉트, state 키).
  2. 랜딩 페이지 `GET /ontology/register?state=…` — **폼**: "ont에서 'MCP 설정 복사' → 여기에 붙여넣기".
  3. `POST /ontology/save {state, config}` → 붙여넣은 MCP 설정(JSON 또는 텍스트)에서 **`Authorization: Bearer` 토큰 추출**(정규식 `eyJ…` 폴백) → `tools/list` 1회 호출로 **유효성 검증**(401이면 거부+안내) → Fernet 암호화 저장 → 확인 DM.
- `server/oauth.py`: `build_ontology_register_url(state)`, `GET /ontology/register`(폼 HTML), `POST /ontology/save`.
- **보안 이점**: 토큰이 Slack을 안 거치고 랜딩(HTTPS)으로 서버 직행 → B 인바운드 로거에 안 잡힘. (bare JWT는 `_redact_secrets`의 `token=` 패턴에 안 걸리므로 DM 붙여넣기 금지.)

## 7. 만료·실패 처리 (브리핑 견고성)

- 브리핑 중 온톨로지 호출이 **401/만료** → 해당 섹션만 생략(브리핑 자체는 정상 진행) + **"온톨로지 토큰 만료 — `/온톨로지`로 재등록"** DM 1회(사용자별 쿨다운으로 도배 방지). Google `/재등록` 감각.
- **timeout/5xx/파싱 실패** → 섹션 생략 + 로그(`log.warning`). 절대 브리핑을 깨지 않음.
- **미등록 사용자** → 온톨로지 섹션 없이 **기존 경로**(웹 검색·기존 위키)로 폴백. (온톨로지는 가산일 뿐, 필수 아님.)

## 8. 게이팅 · 롤아웃

- 환경변수 **`ONTOLOGY_BETA_USERS`** (쉼표구분 user_id, 기본 빈값 = 아무도 안 탐).
- 브리핑/리서치 경로에서 `if user_id in ONTOLOGY_BETA_USERS and has_ontology_token(user_id):` 일 때만 온톨로지 분기, 그 외엔 기존 동작 **완전 동일**.
- 배포해도 전원 무영향. 본인만 allowlist에 넣어 **④이전맥락부터 prod 실검증** → 만족 시 확대 / 플래그 전체 ON.

## 9. 보안

- 토큰 DB **Fernet 암호화**(기존 `_fernet()`), `.env`·git 금지.
- dev 프로브용 `~/.lib_mesh_mcp.json`은 레포 밖(`~/`, chmod 600) 일회성 — 커밋 대상 아님.
- ⚠️ **채팅에 노출됐던 토큰 회전 필요** — ont에 regenerate/revoke 옵션 있는지 확인 후 폐기.
- 사용자별 토큰이라 각자 **자신의 scope 범위에서만** 읽음 → 권한 누수 자동 방지(우리가 며칠 고민한 그 문제 해소).

## 10. 테스트 (pytest — httpx mock, 기존 패턴)

- `tools/ontology.py`: `_parse`가 `content[].text`의 `data` 봉투 파싱; `entity_find`/`entity_cluster` 응답 정규화; `company_context` 묶음; `/mcp/` 직타·리다이렉트 미추종; 401/timeout 시 예외(폴백은 호출부 테스트).
- 등록: MCP 설정(JSON·텍스트 양형)에서 Bearer 추출; `/ontology/save` 저장+검증; 잘못된 설정/401 토큰 거부.
- `user_store`: `ontology_token_enc` save/get/clear 라운드트립.
- 게이팅: beta 아닌/토큰 없는 사용자는 온톨로지 경로 안 탐(기존 폴백).
- 브리핑 통합: beta+등록 사용자는 cluster 결과가 합성 입력에 포함(mock); 만료/미등록은 기존 경로.

## 11. 파일 변경 요약

| 파일 | 변경 |
|---|---|
| `tools/ontology.py` | 신규 — MCP 클라이언트 + 고수준 질의 + `company_context` |
| `store/user_store.py` | `ontology_token_enc` 컬럼 + save/get/clear |
| `server/oauth.py` | `/ontology/register`(폼)·`/ontology/save`·`build_ontology_register_url` |
| `frontend/` (또는 oauth 인라인 HTML) | 등록 랜딩 폼(단순) |
| `agents/before.py` | `research_company`/브리핑에 `ONTOLOGY_BETA_USERS` 게이트 + 온톨로지 분기(④이전맥락·②연결점·③인물 내부), 실패 시 섹션 생략 폴백 |
| `prompts/templates/*` | 합성 프롬프트에 온톨로지 컨텍스트(entities/relations/docs) 주입 |
| `main.py` | `/온톨로지` 커맨드 + 자연어 인텐트 라우팅 |
| `CLAUDE.md` | "온톨로지 연동" 절 추가 |
| `tests/` | §10 |

## 비목표 (YAGNI)

- 온톨로지 **쓰기/되먹임**(회의록 제외 — 그건 Drive 크롤로 자동).
- `entity_path`/`entity_neighbors` 고급 그래프 순회 — 초기엔 `entity_cluster` 한 도구로 충분(추후 필요시).
- 자동 토큰 갱신(refresh token) — PAT 재등록으로 처리(ont가 장수명 토큰 주면 빈도↓).
- 온톨로지 결과의 영구 캐시를 온톨로지에 되먹이기(오염 방지).
