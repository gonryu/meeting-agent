# 브리핑 품질 개선 2차 — A/B/C/D (설계)

> 근거: 이데일리 라이브 브리핑 6개 이슈 병렬 진단(2026-06-24). 4그룹으로 묶어 **A→B→D→C 순** 구현(각 그룹 별도 PR).

## A. 즉효 — 업데이트체크 숨김(#3) + 온톨로지 스니펫/프롬프트(#6)

### A1 (#3) 업데이트 체크 = 내부 부기 → Slack 표시 제거
- 원인: `build_company_research_block`(`tools/slack_tools.py:337-343`)의 `🧭 업데이트 체크` 섹션이 "신규 리서치로 Wiki 생성" 같은 *리서치 워크플로우 상태*를 사용자에 노출. 업체 정보 아님.
- 수정: 해당 렌더 블록(337-343) **제거**. wiki 저장(`before.py`)은 감사용으로 유지. `update_lines` 파라미터는 시그니처 유지(하위호환), 단지 미렌더.

### A2 (#6) 온톨로지 라이트 합성 메타-코멘트 차단
- 원인: ① 스니펫 앞 출처마커(`> ⚠️ UNCERTAIN — confidence: 0.55`, `> ✓ LIKELY …`)가 LLM을 "불확실"로 오해시킴 ② 프롬프트가 메타-코멘트("스니펫 완성 후 제공") 허용. (라이브 확인: 스니펫 내용은 충분했음.)
- 수정:
  - `agents/ontology_synth._fmt_snippets`: 스니펫에서 **출처마커 줄 제거**(`^>\s*(⚠️|✓|✗|ℹ️).*confidence.*$` 및 선두 `> ` 인용 제거).
  - `prompts/templates/ontology_recent.md`: "근거 없으면 생략" → "근거 없으면 정확히 `최근 기록 없음` 출력. **메타-코멘트(스니펫 불완전/완성 후 제공/향후 정리 등) 절대 금지**." + 잘못된 예시 명시.
  - `synthesize_recent_situation`: 합성 결과에 메타-코멘트 키워드("스니펫", "완성 후", "정리하겠", "향후", "확보하면", "불완전") 포함 시 **None 반환**(호출부가 라이트 폴백/생략) — 안전망.

## B. 이메일 노이즈 필터(#5)

- 원인: `tools/gmail.py`가 캘린더 알림만 제외(`_is_calendar_notification`). Google 알리미·noreply·광고·뉴스레터 통과 → 브리핑 이메일맥락 오염.
- 수정: `tools/gmail._is_worthless_email(msg_headers/from/subject)` 신규 — 캘린더알림 + **Google 알리미(googlealerts·google.com 알림)**·**noreply/no-reply 발신**·**마케팅 키워드(unsubscribe 헤더, 제목 '알리미'·newsletter·광고)** 통합. `search_recent_emails`의 기존 `_is_calendar_notification` 호출 지점을 `_is_worthless_email`로 교체. best-effort(헤더 없으면 통과).

## D. 이전 미팅 맥락에 온톨로지 주입(#4) — SSOT 불필요

- 원인: `get_previous_context`(`agents/before.py`)가 Trello+Drive+Gmail만. 온톨로지의 미팅 문서(예: 260129 이데일리 인터뷰)를 안 봄.
- 수정: `_run_briefing_research` ④ 또는 `get_previous_context`에서 게이팅 사용자 대상 **온톨로지 미팅 문서**를 이전맥락에 추가. 재사용: `company_research_sources`(이미 cluster+문서)에서 **미팅성 문서**(제목 회의/미팅/인터뷰/간담회 또는 날짜 패턴)만 골라 `context["ontology_meetings"] = [{title, uri, ym}]`. `build_context_block`이 "📌 이전 미팅 맥락"에 온톨로지 미팅(링크) 렌더. 비용: 이미 호출하는 retrieval 재사용(추가 호출 없음 — `ontology_recent`/`company_context` 결과 활용). SSOT 신규 저장 없음.

## C. 언론사 등 비대상 업체 처리(#1+#2) — 업체타입 분류 (옵션 가)

- 원인: 이데일리(언론사)에 뉴스 동향·서비스 연결점을 억지로 생성. etype=organization이라 미디어 구분 안 됨.
- 수정 (옵션 가 — LLM 1회 분류 + wiki 캐시):
  - `agents/before._classify_company_type(company, context)` 신규 — LLM(Haiku) 1회로 `prospect|media|investor|partner|public_agency|other` 분류. 프롬프트 `prompts/templates/company_type.md`. wiki frontmatter(`company_type`)에 캐시 → 재분류 안 함.
  - `research_company`: `company_type == "media"`면 **뉴스 리서치 스킵**(동향 섹션 "언론사 — 동향 리서치 해당 없음" 또는 생략) + **서비스 연결점 대신** "언론사는 파라메타 서비스 연결점 해당 없음" 고정 문구.
  - 향후 `investor`/`public_agency`도 연결점 톤 조정 가능(이번엔 media만 처리, 나머지 prospect 동일).

## 공통 원칙
- 게이팅(`_ontology_enabled`) 유지(온톨로지 관련 A2·D). A1·B·C는 전체 적용(포맷/필터·분류, 동작 안전).
- best-effort 폴백. 위키 합성 미저장(오염 방지). 라이브 검증 후 배포.

## 구현 순서 / PR
1. **A** (A1+A2) — 작음, 즉효.
2. **B** (#5) — 중간.
3. **D** (#4) — 중간.
4. **C** (#1+#2, 옵션 가) — 중간.

## 테스트 (그룹별)
- A1: `build_company_research_block`에 update_lines 줘도 "업데이트 체크" 미렌더.
- A2: `_fmt_snippets` 출처마커 제거; 프롬프트 핫리로드; 메타-코멘트 키워드 결과→None.
- B: `_is_worthless_email` — 알리미/noreply/광고 True, 정상 메일 False.
- D: 미팅성 문서 필터; `build_context_block` 온톨로지 미팅 링크 렌더.
- C: `_classify_company_type` media 분류(mock LLM); research_company media 분기(뉴스 스킵·연결점 고정문구); wiki 캐시 재사용.

## 비목표
- investor/public_agency 맞춤 처리(media만). 회의록 검색/목록 통합·에러 톤(별도). ont 토큰 회전(운영).
