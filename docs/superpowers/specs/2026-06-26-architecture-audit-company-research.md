# meeting-agent 아키텍처 보고서: 회사리서치/브리핑렌더

작성일 2026-06-26 · 근거: `agents/before.py`, `agents/research_orchestrator.py`, `agents/news_relevance.py`, `tools/slack_tools.py`, `tools/drive.py`, `tools/ontology.py`
(6개 영역 병렬 감사 종합 — 모든 claim file:line 검증)

---

## 0. 한 줄 결론

**전면 재빌드는 하지 말 것.** 진앙은 좁다 — **회사리서치 산출물이 "구조화 dict → JSON 문자열 → 마크다운 → 정규식 재파싱"의 손실 왕복을 거치는 단 하나의 경로**다. 이 경로만 격리해 구조화 객체로 잘라내면 "하나 고치면 다른 게 틀어진다"가 사라진다. OAuth·캘린더·회의록·Trello·Slack로깅 등 나머지 8개 서브시스템은 안정적이며 **건드리지 않는다**.

---

## 1. 핵심 진단 — 덕지덕지의 근본 원인

### 원인 ① 손실 왕복(lossy round-trip)
같은 정보가 3번 형태를 바꾼다:
```
[수집]  industry/competitors() → dict        → json.dumps()로 프롬프트에 박아 구조 폐기 (orch:153)
[합성]  _company_synthesis() → 마크다운 STRING (orch:158,229)
[조립]  research_company()가 ## 섹션으로 이어붙임 (before:1023) → Drive 저장
[재파싱] _extract_company_content_sections() 정규식으로 제목/URL 복원 (before:1410-1583)
[렌더]  _format_news_line_for_slack 정규식 또 한 번 (slack_tools:186-223)
```
**마지막 [재파싱]에 진짜 구조가 없다.** LLM이 줄바꿈 하나만 넣어도, 헤더 포맷이 바뀌어도 추출이 침묵 실패. 코드·프롬프트가 "정규식 재파서가 안 깨지도록 LLM 출력을 통제"하는 데 에너지를 쓰는 방어 주석(before:1419, orch:219, synthesis.md:43)이 덕지덕지의 증거.

### 원인 ② 마커가 출력 포맷에 박힘 → "출력 수정 = 파서 수정"
`_RESEARCH_HEADERS` 하드코딩 문자열 집합(before:991), 뉴스 추출이 `"## "+"최근 동향"` 정확 일치에 의존(before:1422). **표시 관심사(헤더 문구·이모지)가 파싱 관심사와 같은 문자열에 결합** → "예쁘게 보여달라"는 표시 변경이 추출기를 깨뜨림.

### 원인 ③ 이중 경로 — 동일 데이터에 다른 판정 기준
오케스트레이터 경로(`_trend_relevance` Haiku 도메인렌즈)와 단일 경로(`judge_news` Sonnet 등급)가 갈라져(before:917-934), 같은 회사가 어느 경로를 타느냐로 다른 뉴스가 남음. "어떤 회사는 동향 나오고 어떤 회사는 안 나옴"류 비결정 버그의 온상.

---

## 2. 유지 / 리팩터 / 재빌드 지도

| 서브시스템 | 판정 | 근거 |
|---|---|---|
| Slack 렌더/추출 (`_extract_company_content_sections`·`_format_news_line_for_slack`) | **REBUILD** | before:1410-1583 + slack_tools:186-223 위치의존 정규식. 구조없는 문자열에서 제목/URL 추측 |
| 회사리서치 데이터흐름 (`research_company`·`run_company_research`) | **REFACTOR**(경계 신설) | orch:153-229 구조 폐기 + before:917-934 이중경로. 반환타입 구조화로 ②③ 동시 해소 |
| 뉴스/동향 판정 (`judge_news`·`_trend_relevance`) | **REFACTOR** | news_relevance:97-178 인덱스 재맵핑 왕복. 입출력 `list[NewsItem]` 통일 |
| 브리핑 오케스트레이션 (`run_briefing`·`_run_briefing_research`) | **REFACTOR**(후순위) | before:1267-1407 인라인. 구조화 경계 후 정리(지금 손대면 충돌) |
| 온톨로지 클라이언트 (`tools/ontology.py`) | **KEEP** | OntologyClient(read-only) 깨끗 |
| 온톨로지 훅(before 내) | REFACTOR(선택,후순위) | 동작 중이라 보류 |
| OAuth/등록·캘린더·회의록·Trello·Dreamplus·Slack로거 | **KEEP** | 라이브 검증·독립 수직. **절대 손대지 않음** |

**범위: 손댈 곳 2.5개** (REBUILD 1 + REFACTOR 2 + 후속 오케스트레이션). 나머지 8개 KEEP.

---

## 3. 격리 재빌드 설계

**핵심 원리: 마크다운은 저장·표시 포맷일 뿐, 통신·재파싱 포맷이 아니다.** 구조화 객체를 단일 진실로 들고 다니다 끝에서 한 방향으로만 마크다운(Drive)·Slack블록(표시)을 방출.

### 3.1 데이터 모델 (`agents/research_types.py` 신규)
```python
@dataclass
class NewsItem:
    title: str; url: str | None; summary: str
    relevance: str       # 'high'|'mid' — 태그 아닌 필드
    source: str; searched_at: str

@dataclass
class CompanyResearch:
    company_name: str; company_type: str   # 'normal'|'media'
    overview: str                          # 합성 개요(표시 전용, 재파싱 안 함)
    news: list[NewsItem] = field(default_factory=list)
    connections: list[str] = field(default_factory=list)
    email_context: str = ""; trello_context: str = ""
    parascope: list[str] = field(default_factory=list)
```
`relevance`가 **항목 안 필드** → `[관련도]` 태그 부착↔떼기 왕복 소멸.

### 3.2 모듈 경계
```
리서치(수집) run_company_research() → CompanyResearch (마크다운 STRING 아님)
        │ CompanyResearch (단일 진실)
   ┌────┼─────────────┬──────────────┐
 판정          렌더(객체→블록)      저장(write-only)
 judge(        company_block(       to_markdown(
  list[News])   CompanyResearch)     CompanyResearch)
 →list[News]   → Slack blocks       → md(한 방향)
 (필드변경)    (정규식 추출 0:      frontmatter=
               item.title/.url      dataclass→YAML
               직접)
 온톨로지(read-only) tools/ontology.py 변경없음 → CompanyResearch에 주입만
```

### 3.3 인터페이스
```python
def run_company_research(*, company_name, knowledge_md="", gmail_context="") -> CompanyResearch
def judge(items: list[NewsItem], company_name: str) -> list[NewsItem]   # 단일경로! high/mid만
def company_research_block(r: CompanyResearch) -> list[dict]            # 정규식 0
def to_markdown(r: CompanyResearch, preserved_sections: str) -> str     # 한 방향
```

### 3.4 왜 fix-one-break-another가 사라지나
표시·저장·판정이 **같은 객체를 각자 한 방향으로만** 소비 → 한 출력을 고쳐도 다른 출력의 입력(객체)이 안 바뀜. 연쇄 파손의 물리적 경로가 끊김. 헤더 문구·줄바꿈·태그가 더는 파서를 깨지 않음(파서 자체가 없음).

---

## 4. 마이그레이션 — 스트랭글러 (한 번에 안 갈아엎음)

- **0. 타입 신설** — `research_types.py` 추가. 데드코드, 동작 불변. (위험 0)
- **1. 어댑터로 구조화 진입** — `run_company_research`가 내부에서 `CompanyResearch` 만들고 `to_markdown()`으로 직렬화해 **여전히 마크다운 반환**. 외부 인터페이스 불변. 골든 회귀(뉴스 수·URL 보존).
- **2. 렌더를 구조화 입력으로(병행)** — 객체 있으면 객체로 렌더, 없으면 기존 추출기 폴백. 피처플래그 `STRUCTURED_RENDER` A/B. 폴백 살아있어 옛 파일 안 깨짐.
- **3. 판정 단일화** — `judge(list[NewsItem])` 오케·웹 공통. 이중경로 제거. `eval_news_relevance.py` 골든셋 P/R/F1 비회귀 게이트.
- **4. 추출기 폴백 강등→제거** — 단계2 플래그 기본 on 2주 운영, 폴백 발동 0회 로그 입증 후 `_extract_*`·`_format_news_line_for_slack`·`_RESEARCH_HEADERS`·synthesis.md 방어주석 삭제.
- **5. (후순위) 브리핑 오케스트레이션 정리** — 구조화 경계 자리잡은 뒤. 0~4와 절대 안 섞음.

각 단계 폴백+골든셋 게이트 → 배포 안전, 되돌림은 플래그 revert.

---

## 5. 하지 말 것 (YAGNI)
- 전면 재작성 금지. 회의록·OAuth·캘린더·Trello·Dreamplus·Slack로거 KEEP, 한 줄도 안 건드림.
- SSOT 중앙화·재설계 금지(폐기됨). `CompanyResearch`는 함수 사이 흐르는 in-flight 구조체일 뿐 중앙저장소 아님. 사용자별 토큰·Drive 위키 write-only 유지.
- Pydantic/마크다운 파서 라이브러리 도입 보류. 표준 `@dataclass`로 충분(마크다운은 방출만, 재파싱 안 함이 목표).
- 온톨로지 훅 대수술·AUTO_START 마커 재설계 보류(후순위, 직접 관련 없음).

**요약: 재빌드 충동은 정당하나 범위 과대평가. 병은 단 하나의 손실 왕복 경로. 처방 = 마크다운을 통신포맷에서 강등하고 그 사이를 `CompanyResearch` 객체로 흐르게. 스트랭글러 5단계, 각 단계 폴백+골든셋 게이트로 배포 안전. 나머지 전부 KEEP.**
