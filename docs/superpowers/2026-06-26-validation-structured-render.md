# 검증 시나리오 — 회사리서치 구조화 렌더 (스트랭글러 단계0~2)

대상: 라이브 (PR #68, #69 배포 완료). `STRUCTURED_RENDER` 기본 ON.
목적: 동향이 `<링크|제목> — 썰` 구조로 나오는지 + 회귀(동향 누락/링크만 덜렁/개요 누출/엉뚱한 줄) 없는지.

모든 시나리오는 봇에게 **DM**으로 보냄. 온디맨드 "{업체} 리서치"는 `force=True` 신선 재리서치라
**단계1(오케→객체→직렬화) + 단계2(위키→단일파서→NewsItem 렌더)를 한 번에** 검증.

---

## A. 일반 외부 업체 — 동향 있음 (핵심)

**입력 (DM):** `KISA 리서치` (또는 `삼성증권 알아봐줘`, `카카오 리서치`, `두나무 리서치`)

**기대 — `📰 업체 동향`:**
```
• <https://…|국가 망 보안체계(N2SF) 도입 본격화> — KISA가 N2SF 공공 확산에 예산을 투입한다
• <https://…|블록체인 밋업데이(BCMD) 교육생 모집> — 블록체인 신뢰인프라 인력 양성
```

**PASS:**
- 각 줄 = **클릭되는 제목 링크** + ` — ` 뒤에 **한 줄 썰(설명)**.
- 2~3건. 파라메타 사업분야(블록체인·보안·디지털자산·공공 등)와 겹치는 활동.

**FAIL 신호 (있으면 캡처/붙여넣기로 알려줘):**
- ❌ 링크만 덜렁 (제목만, 썰 없음)
- ❌ "최근 동향 정보 없음" (있어야 할 동향이 안 뜸)
- ❌ `산업 위치`·`경쟁 구도` 같은 **개요 라벨이 동향에 섞임**
- ❌ `last_searched: 2026-…` 가 뉴스 한 줄로 뜸
- ❌ `[출처: 웹 검색…]` 태그가 썰 끝에 노출
- ❌ 깨진 괄호 `(2026.06.23` (안 닫힘)

---

## B. 언론사 (media)

**입력 (DM):** `이데일리 리서치`

**PASS:**
- `🔗 파라메타 서비스 연결점` = "언론사로 분류되어 … 연결점은 해당하지 않습니다" 류 문구.
- 동향: 기사 **클리핑이 안 나옴**(미팅 맥락/정보없음만). web 동향 스킵이 정상.

**FAIL:** 기사 클리핑 자료가 동향/연결점에 쏟아짐.

---

## C. 동향 없는/모호한 업체

**입력 (DM):** 파라메타 도메인과 무관하거나 공개정보 적은 업체명 (예: 잘 안 알려진 소규모 업체)

**PASS:** `📰 업체 동향` → `• 최근 동향 정보 없음` 한 줄로 **깔끔**.

**FAIL:** 빈 줄/엉뚱한 줄/개요 라벨이 뜸.

---

## D. 브리핑 경로 (다른 렌더 호출부)

**입력 (DM):** `브리핑` 또는 `오늘 미팅 브리핑` (외부 업체가 참석자인 다가오는 캘린더 미팅이 있을 때)
또는 매일 09:00 자동 브리핑에서 확인.

**PASS:** 브리핑의 `🏢 {업체} 업체 정보` 블록에서도 A와 동일하게 `<링크|제목> — 썰`.

이유: 브리핑·온디맨드 두 경로가 **같은 헬퍼(`_structured_news_items`)**를 공유. 둘 다 같아야 정상.

---

## E. 결정론적 출력 품질 eval

실제 Slack 렌더 출력은 분류별 가이드를 `tests/eval_output_quality.py`로 검증한다.

```bash
.venv/bin/python tests/eval_output_quality.py
```

분류:
- `company_research`: 제목 링크 + 한 줄 요약, raw URL/`**`/`last_searched`/`• **` 금지
- `media_company`: 언론사 뉴스 조작 금지, 연결점 비대상 고정 문구
- `ontology_render`: 한국어 관계 라벨, 번호섹션 노이즈 제거, Drive 문서 링크
- `context_block`: 이전 미팅/이메일/온톨로지 최근상황 분리, false empty 금지
- `meeting_header`: 시간/Meet/장소/업체/참석자/어젠다 표시

PASS 기준: 모든 rule 통과. 특정 분류만 볼 때는 `--category company_research`처럼 실행.

---

## F. 추가 eval — source / polish / golden

```bash
.venv/bin/python tests/eval_source_quality.py
.venv/bin/python tests/eval_polish_fidelity.py
.venv/bin/python tests/eval_company_research_golden.py
```

- `eval_source_quality.py`: 뉴스 불릿별 출처 URL 존재, 깨진 URL, 명시적 no-info 상태 검증
- `eval_polish_fidelity.py`: 윤문 전후 URL/날짜/수치/보호용어 보존 검증
- `eval_company_research_golden.py`: 대표 업체 유형(KISA/언론사/동향없음)의 Slack 출력 회귀 검증

---

## G. 선택 워크플로우 — insane-search / im-not-ai

기본 라이브 경로는 그대로 Claude web_search + `NewsItem` 구조화 렌더다. 외부 도구는 아래 플래그가 켜진 경우에만 보조 경로로 사용한다.

### insane-search assisted sources

- 모듈: `agents/research_assist.py`
- 기본값: 비활성
- 플래그:
  - `INSANE_SEARCH_ASSISTED=true`
  - `INSANE_SEARCH_RESULTS_DIR=/path/to/sources` — `{업체}/sources.md`, `{업체}/research.md`, `{업체}.md` ingest
  - `INSANE_SEARCH_COMMAND="..."` — stdout markdown을 evidence로 ingest
- 주입 위치: `research_orchestrator.run_company_research()`의 `knowledge_md` 보강. Slack에 원문 렌더하지 않음.

### im-not-ai / humanize-korean polish

- 모듈: `agents/korean_polish.py`
- 기본값: 비활성
- 플래그:
  - `KOREAN_POLISH_ENABLED=true`
  - `KOREAN_POLISH_COMMAND="..."` — stdin 원문, stdout 윤문본
  - `POLISH_MAX_CHANGE_RATIO=0.30`
- 적용 후보: 서비스 연결점 프로즈, 온톨로지 프로즈 요약, 브리핑 요약
- 적용 금지: `NewsItem.title/url/date`, URL, 수치, 날짜, 고유명사, 직접 인용
- 채택 조건: `validate_fidelity()` 통과. 실패 시 원문 유지.

---

## 회귀 모니터링 (내가 보는 것)

- 라이브 로그에 `[STRUCTURED_RENDER] {업체}: structured=N legacy=M` — 구조화 vs 레거시 추출 건수 차이.
  N<M(구조화가 덜 잡음)이 잦으면 단일 파서 보강 필요. N≥M이 기대.
- **방어적 폴백**: 구조화 0건인데 레거시가 찾으면 자동으로 레거시 렌더 → 동향이 **사라지지는 않음**.
- **킬스위치**: 문제 시 `STRUCTURED_RENDER=false`로 즉시 레거시 복귀(내가 라이브 .env에서 끔, 재시작).

---

## 결과 회신

이상하면 **해당 업체 + 그 블록 스크린샷/텍스트**를 붙여줘. `[STRUCTURED_RENDER]` 로그와 대조해
단일 파서(`parse_trend_bullets`)/추출(`extract_news_items`)/렌더(`_format_news_item_for_slack`) 중
어디서 어긋났는지 바로 짚을 수 있어.
