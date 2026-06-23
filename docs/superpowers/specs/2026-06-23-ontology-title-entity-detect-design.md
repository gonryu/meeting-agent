# 온톨로지 기반 제목 엔티티 감지 — 업체 추론 폴백 (설계)

> 문제: 라이브 브리핑에서 "KISA, 과기부 간담회"·"이데일리 이정훈기자님" 등이 **관련 업체 미감지 → 리서치 블록 통째 생략**(헤더만). 원인: 참석자가 전부 사내라 역추론 막히고, LLM 제목추론도 보수적이라 명백한 기관(KISA·이데일리·과기부)도 NONE.
> 해결: 온톨로지 `entity_find`(7.5만 엔티티)를 **업체 추론 마지막 폴백**으로 추가 — LLM 추측이 아니라 사실 기반 엔티티 매칭.

## 1. 목표

기존 업체 추론 체인(extendedProperties → LLM 제목추론 → 참석자 역추론)이 모두 실패할 때, 제목 토큰을 `entity_find`로 검증해 **알려진 조직 엔티티**면 관련 업체로 태깅 → 리서치/온톨로지 경로 진입.

## 2. 확정 사실 (라이브 프로브, 2026-06-23)

`entity_find`는 `etype`(organization/project/concept/technology/person…)·`match_kind`(exact/substring/fuzzy)·`importance`·`sources_count`·`aliases` 반환. 검증:
- **KISA** → exact, etype=organization, imp 1.0, src 262 ✓
- **이데일리** → exact, etype=organization, src 1 ✓
- **과기부** → exact, etype=organization, src 9 ✓
- **6ixgo** → 깨끗한 매칭 없음("Go" technology substring=오탐, vigo fuzzy). **exact 아님 → 제외** ✓ (6ixgo는 내부 제작작업이라 미감지가 맞음)

→ **채택 규칙: `match_kind=="exact"` + `etype∈{organization, company}`.** 정밀도 높음(기관 잡고, 기술·인물·내부작업 배제).

## 3. 설계

### 3.1 감지기 (`tools/ontology.py`)
- `detect_company_in_title(user_id, title) -> str | None` 신규:
  - 토큰 없거나 토큰 미보유 시 None(게이팅은 호출부에서).
  - **토큰화**: 제목을 공백·구두점(`,·_/()-:` 등)으로 분리, 길이<2·`_STOPWORDS`(간담회/미팅/회의/촬영/제작/협의/진행/후속/논의/주간/정기/mou/poc/킥오프 등) 제거, 앞에서 최대 5개.
  - 각 토큰 `entity_find(token, limit=2)` → 매칭 중 **`match_kind=="exact"` & `etype in {organization, company}` & `importance>=0.5` & title이 `_OWN_ORG_DENYLIST`(parametacorp·iconloop·infrateam·enterprise…) 아님** 인 것만 후보.
  - 후보 중 `(importance, sources_count)` 최고 1개의 **title**(canonical) 반환. 없으면 None.
- 상수: `_STOPWORDS`(set), `_OWN_ORG_DENYLIST`(set, 소문자 비교), `_ORG_ETYPES={"organization","company"}`.
- best-effort: 온톨로지 예외 시 None(로그). `OntologyAuthError`는 삼켜 None(브리핑 흐름 안 깸 — 만료 DM은 ④에서 별도 처리).

### 3.2 배선 (`agents/before.py` run_briefing)
- 참석자 역추론 블록(~1282-1290) 다음, extendedProperties 저장(~1292) 앞에 4번째 폴백:
```python
        # FR-B17: 온톨로지 엔티티 감지 (게이팅) — LLM/참석자 추론 실패 시 사실 기반 폴백
        if not company_names and _ontology_enabled(user_id):
            try:
                from tools import ontology
                detected = ontology.detect_company_in_title(user_id, meeting.get("summary", ""))
                if detected:
                    company_names = [detected]
                    log.info(f"업체명 추론 성공 (온톨로지): '{meeting.get('summary')}' → {detected}")
            except Exception as oe:
                log.warning(f"온톨로지 제목 감지 실패: {oe}")
```
- 이후 기존 흐름: `company_names` 있으면 extendedProperties 저장 + `_send_briefing` + research_queue 등록 → 리서치(업체 온톨로지 프로즈 #53 포함) 정상 진입.

### 3.3 내부작업 안전판
- 감지로 업체 태깅돼 리서치 진입해도, `_run_briefing_research`의 `briefing_classifier`(internal 판정 시 스킵, ~1408)가 그대로 작동 → "6ixgo 촬영" 류가 혹 매칭돼도 내부면 리서치 스킵. (단 6ixgo는 §2처럼 애초 매칭 안 됨.)
- 감지 결과 extendedProperties 저장(기존 FR-B15) → 다음 조회 재사용(매번 entity_find 안 함).

## 4. 비용 / 게이팅
- `_ontology_enabled` 사용자 + **기존 추론 전부 실패한 미팅만** 실행. 미팅당 ≤5 entity_find(LLM 없음, 싸다). 결과 캐시(extendedProperties).
- 비게이팅·토큰없음 → 기존 동작(감지 없음).

## 5. 테스트
- `detect_company_in_title`: "KISA, 과기부 간담회"→"KISA"(최고 importance), "이데일리 이정훈기자님"→"이데일리", "6ixgo MoU 촬영"→None(exact org 없음), 사내팀("InfraTeam 회의")→None(denylist), 토큰없음 None, 토큰 보유시 게이팅은 호출부 (httpx MockTransport).
- 토큰화/스톱워드/etype·match_kind·denylist 필터 단위.
- `_ontology_enabled` False면 호출 안 됨(배선 테스트는 회귀로).

## 6. 파일 변경 요약
| 파일 | 변경 |
|---|---|
| `tools/ontology.py` | `detect_company_in_title` + `_STOPWORDS`/`_OWN_ORG_DENYLIST`/`_ORG_ETYPES` |
| `agents/before.py` | run_briefing 4번째 폴백 배선(게이팅) |
| `tests/test_ontology_detect.py` | §5 |
| `CLAUDE.md` | 업체추론 체인에 온톨로지 폴백 한 줄 |

## 7. 위험 / 롤아웃
- 게이팅(본인만). 폴백이라 기존 추론 성공 시 미동작. 오탐 위험은 exact+org+denylist로 억제. ont 다운/실패 → None(기존 동작).
- 배포 후 라이브 브리핑으로 KISA/이데일리 감지 확인.

## 비목표 (YAGNI)
- project/concept etype 채택(정밀도 위해 org만). 복수 업체 동시 태깅(최고 1개). 인물 엔티티 기반 담당자 보강(후속). LLM 제목추론 프롬프트 변경(온톨로지 폴백으로 충분).
