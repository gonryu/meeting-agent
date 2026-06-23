# 출력 포맷 하네스 — 결정론적 표준화 레이어 (설계)

> 근거: `docs/OUTPUT_CATALOG.md`(출력 217종 전수). 높음 이슈 3개가 `tools/slack_tools.py` 빌더로 수렴.
> 본 스펙은 **LLM 없는 결정론적 포맷 표준화**만 다룬다. 브리핑 온톨로지의 LLM 프로즈 합성(라이트 합성)은 비용 발생 기능이라 **별도 증분(브리핑 티어)으로 보류**.

## 1. 목표 / 범위

카탈로그 우선순위 **높음 3개**를 공통 유틸로 일괄 해소:
1. **온톨로지 원시 덤프 정리** — 영어 관계타입→한국어 라벨, depth-2 노이즈(`01. Cluster 구성하기` 등) 필터, 문서를 클릭 링크로.
2. **이름 해석 통일** — 담당자 블록의 `email.split("@")[0]`(→`• min`) 제거, 헤더와 동일 리졸버 사용, 폴백=전체 이메일.
3. **mrkdwn 볼드 정규화** — `**bold**`→`*bold*` (Slack 정식), Trello/액션 출력 13곳.

**비목표(보류):** 브리핑/인물 온톨로지의 LLM 프로즈 합성(브리핑 티어 증분), 회의록 검색/목록 포맷 통합(중간 우선순위, 후속), 에러 톤 템플릿(후속).

## 2. 현재 상태 (코드 근거)

- 온톨로지 렌더: `tools/slack_tools.py` `build_company_research_block`(:279-282, `• {relation}: {title}` / `• 문서: {title}`), `build_context_block`(:384-387, `   • {relation}: {title}` / `   • 문서: {title}`). 둘 다 **영어 관계타입 그대로**, 문서 **링크 없이 제목만**.
- 관계/문서 데이터: `tools/ontology._normalize_cluster`(:88) — `relations:[{relation, title}]`(hop 정보 버림 → depth-2 노이즈 섞임), `documents:[{title,id,uri,space,ym,matched}]`(uri 있음, 렌더 미사용).
- 이름 해석: 헤더 👥는 `agents/before._resolve_attendee_names`(:462, 4단계 체인, 폴백=전체 이메일 ✓). **담당자 블록**은 `_run_briefing_research`(:1528 `person_names = [a.get("name") or a.get("email","").split("@")[0] …]`)로 **별도 크루드 경로** → `• min`.
- mrkdwn `**`: `agents/after.py`·`agents/trello_report.py`·`tools/trello.py`에 ~13곳.

## 3. 설계 — 공통 유틸 + 적용

### 3.1 온톨로지 렌더 정리 (`tools/slack_tools.py`)
- **`_KO_RELATION` 매핑** + `_relation_label(en)`:
  `part-of`→"소속", `related-to`→"관련", `uses`→"활용", `depends-on`→"의존", `implements`→"구현", `instance-of`→"유형", `alias-of`→"별칭", `mentioned`→"언급", `supersedes`→"대체". 미매핑은 원문 유지.
- **노이즈 필터** `_is_noise_relation(title)` — 제목이 번호섹션 패턴(`^\s*\d{1,4}[.\s]`)이면 True(예 `01. Cluster 구성하기`, `0102. PrivateKey…`). 렌더에서 제외.
- **문서 링크** — `d.get('uri')` 있으면 `<uri|title>`, 없으면 title. 표시 제목은 `_doc_label(title)`로 확장자·해시 꼬리 정리(예 `발표자료_KOMSA_Proposal_…_최종.pdf`→`발표자료_KOMSA_Proposal_최종`은 과하므로 **최소 정리**: 끝 `.pdf/.pptx/.xlsx/.md` 확장자만 제거).
- `build_company_research_block`(②라이트 경로)·`build_context_block`(①브리핑) 렌더를 위 유틸로 교체. **`ontology_brief`(딥 합성, #51) 경로는 불변**(이미 프로즈).
- `_normalize_cluster`(`tools/ontology.py`): relations에 `hop` 보존 + 노이즈/`instance-of` 번호섹션 1차 필터(retrieval 단계에서 거르면 인물 블록도 동시 정리). 단, 키(`relation`,`title`)는 유지하고 `hop`만 추가(하위호환).

### 3.2 이름 해석 통일 (`agents/before.py`)
- `_resolve_attendee_names`를 단건 재사용 가능하게 `_resolve_one_name(attendee, creds)` 추출(또는 기존 함수 재사용). 
- `_run_briefing_research`의 person 루프: **표시명**은 리졸버로 해석(Calendar displayName→Slack→Contacts→**전체 이메일**), **검색키**(research_person 인자)는 기존대로. 즉 `persons_info`에 `{"name": 표시명, "search": 검색명, ...}` 분리. `build_persons_block`은 표시명 사용.
- 폴백 표시: localpart 금지. 전체 이메일(`min@icon.foundation`)로 — 모호성 제거. (사내 도메인 라벨링은 후속.)

### 3.3 mrkdwn 정규화 (`tools/slack_tools.py` 유틸 + 호출부)
- `to_slack_mrkdwn(text)` — `**bold**`→`*bold*` 변환(이미 단일 `*`는 보존, 정규식 `\*\*(.+?)\*\*`→`*\1*`). 
- `agents/after.py`·`agents/trello_report.py`·`tools/trello.py`의 사용자 노출 문자열 생성부에 적용(발송 직전 1회). best-effort, 텍스트만.

## 4. 아키텍처 / 원칙
- 모든 유틸 **순수함수**(LLM·IO 없음) → 빠르고 테스트 쉬움.
- `slack_tools`는 렌더 전용 유지(LLM 금지). 이름 리졸버는 `before`(creds 접근)에 둠.
- 하위호환: `_normalize_cluster` 반환 키 유지(`relation`,`title`,`hop` 추가만) → 기존 호출부(`company_context`/`person_context`/딥 경로) 불변.

## 5. 테스트
- `_relation_label`: 매핑/미매핑 통과.
- `_is_noise_relation`: `01. Cluster 구성하기`·`0102. PrivateKey` True, `KISA 공공과제`·`InfraTeam` False.
- 온톨로지 렌더(`build_context_block`/`build_company_research_block`): 한국어 라벨 출력, 노이즈 제외, uri 있으면 `<uri|title>` 링크, 확장자 제거.
- `to_slack_mrkdwn`: `**x**`→`*x*`, `*y*` 보존, 혼합 케이스.
- 이름 통일: displayName 있으면 그대로, 없고 Slack/Contacts 있으면 그 이름, 다 없으면 전체 이메일(localpart 아님). `build_persons_block` 표시명 사용.
- `_normalize_cluster`: hop 보존 + 번호섹션 노이즈 필터, 기존 키 유지(회귀).

## 6. 파일 변경 요약
| 파일 | 변경 |
|---|---|
| `tools/slack_tools.py` | `_relation_label`/`_KO_RELATION`/`_is_noise_relation`/`_doc_label`/`to_slack_mrkdwn` 유틸 + 온톨로지 렌더 2곳 교체 |
| `tools/ontology.py` | `_normalize_cluster`에 hop 보존 + 번호섹션 노이즈 필터 |
| `agents/before.py` | person 루프 표시명 리졸버 통일(검색키 분리) |
| `agents/after.py`·`agents/trello_report.py`·`tools/trello.py` | 사용자 노출 문자열에 `to_slack_mrkdwn` 적용 |
| `tests/…` | §5 |
| `CLAUDE.md` | (선택) 출력 포맷 규칙 한 줄 |

## 7. 위험 / 롤아웃
- 순수 포맷 변경, 게이팅 불필요. 온톨로지 렌더는 기존에도 게이팅 사용자만 보임. mrkdwn/이름은 전 사용자 영향이나 **표시 개선만**(동작 불변). 회귀는 pytest로 커버.
- 배포해도 안전(additive 포맷). ont 다운 시 온톨로지 섹션은 어차피 폴백 생략.

## 비목표 재확인 (YAGNI)
- 브리핑 온톨로지 LLM 프로즈(브리핑 티어), 회의록 검색/목록 통합, 에러 톤 4단계 템플릿, 진행메시지/D-day 공유 유틸 — 모두 후속 증분.
