# ParaMee 봇 출력 카탈로그 (전체 정리)

> 2026-06-22 전수 스캔(10개 영역, 출력 217종). 봇이 사용자/관리자에게 내보내는 모든 출력을 대분류별로 정리.
> 컬럼: **출력 | 트리거 | 표면 | 포맷 | 데이터출처**.

---

## 1. 브리핑

| 출력 | 트리거 | 표면 | 포맷 | 데이터출처 |
|---|---|---|---|---|
| 인트로 메시지 (`📅 …님의 {기간} 일정`) | `run_briefing()` 다중 미팅 조회 | DM | 평문 | Slack users_info |
| 미팅 없음 안내 | 기간 내 미팅 0개 | DM | 평문 | Calendar |
| ① 헤더 블록 (제목·시간·장소·참석자·어젠다·Meet) | `_send_briefing()` (외부) | DM/스레드 | Slack Block | Calendar + users_info |
| ② 업체 리서치 블록 (동향·ParaScope·연결점·Trello·온톨로지·업데이트) | `_run_briefing_research()` | DM/스레드 | Slack Block | Drive wiki + 온톨로지 |
| ③ 담당자 블록 (이름·직책·LinkedIn·메모·함께한 미팅) | 인물 리서치 완료 | DM/스레드 | Slack Block | 참석자 + `_person_meetings()` |
| ④ 이전 맥락 블록 (회의록·Trello·이메일·온톨로지) | 맥락 수집 완료 | DM/스레드 | Slack Block | `get_previous_context()` + 온톨로지 |
| 내부 미팅 브리핑 블록 (리서치 제외) | `_send_internal_briefing()` | DM/스레드 | Slack Block | Calendar |
| Todo 블록 (오늘의 활성 할일) | `run_briefing()` 완료 후 | DM/스레드 | Slack Block | todo 모듈 |
| 진행 메시지 ×3 (🔍/👤/📨 …중) | 각 리서치 단계 시작 | DM/스레드 | 평문 | 파라미터 |
| 온톨로지 토큰 만료 안내 (🔑) | `OntologyAuthError` | DM/스레드 | 평문 | 예외 |
| 오류 메시지 ×3 | 각 단계 예외 | DM/스레드 | 평문 | Exception(429=할당량) |

## 2. 회의록

| 출력 | 트리거 | 표면 | 포맷 | 데이터출처 |
|---|---|---|---|---|
| 소스선택 블록 (🎙️/📎/📝/🕐/❌) | `/미팅종료` 후 | DM/채널·스레드 | Block(버튼) | 활성 세션 |
| 트랜스크립트 첨부 대기 | 📎첨부 선택 | DM | 평문 | `_pending_uploaded_transcript` |
| 회의록 초안 카드 (메타+본문+✅저장/✏️수정/❌취소) | 생성 직후 | DM/채널 | Block(MD+버튼) | `_pending_minutes` |
| 수정 요청 스레드 안내 / 수정 진행·결과 재발송 | ✏️수정·스레드 답글 | DM | 스레드→새 카드 | `find_draft_by_thread_ts` |
| 저장 중 → 최종 완료 + Drive 링크(내부/외부) | ✅저장 | DM/채널 | 평문+링크 | Drive |
| 취소 확인 / 보강 안내(90분) | ❌취소·노트만 생성 | DM | 평문 | `_pending`/`_awaiting_transcript` |
| 검토 대기 목록(`/대기회의록`)·대기 버튼 | 슬래시·초안 발송 | DM/채널·스레드 | Block(목록+버튼) | `_pending_minutes` |
| 회의록 작성 즉시(`/회의록작성`)·문서기반(F4) | 슬래시·문서 업로드 | DM/채널·스레드 | 텍스트+Block | 세션/파일 |
| **검색 결과**(`/회의록 검색어`) | get_minutes(검색) | DM/채널·스레드 | **평문 MD**(최대20) | Drive 파일명 |
| **전체 목록**(`/회의록`) | get_minutes(목록) | DM/채널·스레드 | **Block**(목록+버튼) | Drive 폴더 |
| 양식 보정 목록·미리보기(L1 diff)·L1/L2 보정 완료 | `/회의록정리`·버튼 | DM/채널·스레드 | Block(diff)+링크 | `diagnose/normalize` |

## 3. 액션아이템 · Todo

| 출력 | 트리거 | 표면 | 포맷 | 데이터출처 |
|---|---|---|---|---|
| 액션아이템 추출 결과 / 담당자별 개인화 DM | 회의록 완료(After) | DM/담당자 DM | mrkdwn | LLM 추출 |
| 담당자 미매칭 안내 (전 소스 실패) | `_lookup_person` 실패 | 주최자 DM | 평문 | 모든 소스 |
| 매일 08:00 리마인더 (D-day/D-1 우선순위) | cron 08:00 | 담당자 DM | mrkdwn(이모지) | DB |
| 완료 버튼 응답 | 완료 클릭 | DM | 단문 | DB |
| Todo 추가/다중추가/과거완료팁/목록/완료·취소·삭제/수정 | 슬래시·자연어·버튼 | DM/채널 | 텍스트+Block | LLM 파싱+DB |
| Todo Drive 동기화(오픈루프·이력) | 생성/완료/취소/수정 | Drive | 마크다운 | DB→Drive |

## 4. Trello

| 출력 | 트리거 | 표면 | 포맷 | 데이터출처 |
|---|---|---|---|---|
| 등록 제안 UI(유사카드+드롭다운+신규) / 스레드 등록 플로우 | 회의 후·자연어·스레드 | DM/채널·스레드 | Block | 액션DB+Trello |
| 신규 카드 확인 / 등록 완료·실패 / 건너뜀 | 버튼·API 결과 | DM/호출위치 | 평문+링크 | Trello API |
| 카드 조회 결과 / 미발견 | `/트렐로조회`·자연어 | DM | MD 불릿 | Trello |
| 브리핑 내 카드 정보 | 09:00 브리핑 | DM | Block | `get_card_context` |
| 인증 안내·링크 / 토큰 저장 | `/trello`·자연어 | DM+웹 | Block(링크) | OAuth |
| 주간 보고 요약(📊)+Docs 상세 / 리스크 강조(🚨) | 금21:00·자연어 | 관리자채널+Docs | mrkdwn+Doc | Trello REST+LLM |

## 5. 제안서 · 피드백

| 출력 | 트리거 | 표면 | 포맷 | 데이터출처 |
|---|---|---|---|---|
| 제안서 키워드 감지 제안 | 회의록 확정 후 | DM | Block | 회의 본문 |
| 개요 확인(6필드)·수정 / 초안(2500자+Docs)·수정 / 완료·취소 | 버튼·스레드 | DM | Block(MD) | orchestrator |
| 제안서 Docs 링크(📝편집) | 초안 생성/수정 | DM | Block(URL) | Drive |
| 피드백 접수 확인(#id+유형+요약) | 자연어 피드백 | DM/스레드 | 메시지 | LLM 분류 |
| 피드백 다이제스트(카테고리+원문) | 매일 22:00 | FEEDBACK_CHANNEL | 메시지 | DB |

## 6. 드림플러스 회의실

| 출력 | 트리거 | 표면 | 포맷 | 데이터출처 |
|---|---|---|---|---|
| 예약 추천 리스트(3개+네비)·페이지 갱신 | `/회의실예약`·자연어·미팅직후 | DM/채널·스레드 | Block | DP API |
| 예약 확정·캘린더 장소 업데이트 / 예약 내역·취소(+환불) | 버튼 | DM/채널·스레드 | 평문 | DP+Calendar |
| 크레딧 조회 / 계정 설정(웹폼·성공·실패) | 슬래시·모달 | DM+웹 | 평문/HTML | DP API |
| 오류·예외 안내 25+종 (시간파싱/가용없음/세션만료/미설정 등) | 각 단계 검증·예외 | DM/채널·스레드 | 평문 | DP/JWT/세션 |

## 7. 정기 발송 (스케줄러)

| 출력 | 트리거 | 표면 | 포맷 | 데이터출처 |
|---|---|---|---|---|
| 매일 09:00 자동 브리핑 | cron 09:00 | 사용자 DM | 브리핑 블록 | Calendar(`briefing_enabled≠0`) |
| 미팅 5분 전 알람(+🛑버튼) | 매분 폴링 | 사용자 DM | Block | Calendar(중복방지DB) |
| 매일 08:00 액션 리마인더 | cron 08:00 | 담당자 DM | 우선순위 리스트 | DB |
| 매일 22:00 피드백 다이제스트 | cron 22:00 | FEEDBACK_CHANNEL | 카테고리 그룹 | DB |
| 매주 금 21:00 Trello 주간 보고 | cron 금21:00 | TRELLO_REPORT_CHANNEL | 요약+Docs | Trello 7일 |
| 10분/2분 트랜스크립트 폴링 | 주기 | 내부(생성시 DM) | 로그/DM | Drive/세션 |
| 매일 03:00 메시지 로그 정리 | cron 03:00 | 내부 | 로그 | message_log(90일) |

## 8. 등록 · 설정 · 도움말 · 인텐트 폴백

| 출력 | 트리거 | 표면 | 포맷 | 데이터출처 |
|---|---|---|---|---|
| 등록/재등록 안내·Google 인증 완료(HTML+DM)·토큰 만료 | 미등록·OAuth·만료 | DM+웹 | 평문/HTML | Google OAuth |
| Trello/온톨로지/드림플러스 등록 안내(URL+웹폼+검증) | 슬래시·자연어 | DM+웹 | 평문+HTML form | OAuth/MCP/DP |
| 설정 토글 블록(브리핑/미팅 알람)·in-place 업데이트 | `/설정`·버튼 | DM | Block | DB |
| 도움말 텍스트(전 기능) / 자연어 질문 답변 / 명령어 추천 | `/도움말`·question·unknown | DM/채널 | 평문 MD | 정적/LLM |
| 기업 리서치 블록 / **인물 리서치 평문(300자)** | `/company`·`/person` | DM | Block / **평문** | Drive+Trello+온톨로지 |
| 업체 메모 저장 / 명함 OCR / 음성 STT / 문서 추출 안내 | 자연어·업로드 | DM | 평문 | OCR/STT/추출 |
| 파라미터 누락 안내 다수 / 폴백 안내 / 웹 랜딩 오류 | 검증 실패·예외 | DM/웹 | 평문/HTML | 검증 |
| 배포 완료/실패 경보 | `/deploy` webhook | FEEDBACK_CHANNEL | 평문 | git+systemctl |

## 9. 관리자 페이지 (웹)

| 출력 | 트리거 | 표면 | 포맷 | 데이터출처 |
|---|---|---|---|---|
| 대시보드(집계+최근 피드백5) | `#/dashboard` | 관리자웹 | 평문+테이블 | counts/feedback/message_stats |
| 사용자 목록 / 상세+대화 타임라인(인·아웃 인터리브) | `#/users` | 관리자웹 | 테이블/채팅버블 | `list_messages(asc)` |
| 피드백 목록(필터+상태변경) / 메시지 로그 피드·상세 | `#/feedback`·`#/messages` | 관리자웹 | 필터+테이블 | `list_messages` |
| 프롬프트 템플릿 목록·편집기(백업) | `#/prompts` | 관리자웹 | 테이블/에디터 | `prompts/templates/*.md` |

## 10. 온톨로지 (lib-mesh)

| 출력 | 트리거 | 포맷 | 데이터출처 | 상태 |
|---|---|---|---|---|
| ① 업체 온톨로지 — 브리핑 ④(`🔗`) | `company_context(recent)` | Block(관계6+문서5) | entity_cluster | 배포 #46 |
| ② 업체 온톨로지 — standalone 리서치(`🧠`) | `research_company` 포스팅 | Block(관계6+문서5) | entity_cluster | 배포 #49 |
| ③ 인물 미팅이력 — 브리핑 ③(`함께한 미팅`) | `_person_meetings` | Block 리스트(6) | person_context+정규식 | 배포 #47 |
| ④ 토큰 만료 안내 / ⑥ 등록 안내 | 401·슬래시·자연어 | 평문+버튼 | 예외/intent | 배포 #48·#50 |
| ⑤ 딥 리서치 합성 브리핑(출처+critic) | 온디맨드 리서치 | Block(구조화 synthesis) | cluster+document_fetch+Sonnet+Haiku critic | **inflight** |
| ⑦ `company_research_sources`(R1 필터·문서 선별) | `deep_company_ontology` | 구조화 dict | cluster+document_fetch | **inflight** |

---

## ⚠️ 정리·표준화 필요 목록

### 우선순위 높음
1. **원시 cluster 덤프 노이즈 (온톨로지 ①②③)** — 브리핑·standalone·인물 블록 모두 entity_cluster 관계/문서를 원시 나열. LLM 합성 미적용, 그래프 메타데이터(`instance-of: 01. Cluster 구성하기` 등) 노출. 인플라이트 ⑤⑦(grounding 합성+R1 필터)이 정조준하나 standalone(②)만 적용 예정 → **①③(브리핑)에도 적용 필요**.
2. **이메일이 이름으로 노출 (브리핑 참석자·담당자 `• min`)** — displayName→Slack→Contacts→이메일 폴백 체인의 마지막 단계가 사용자에게 자주 보임. 사내 도메인 표시 규칙도 없음.
3. **온톨로지 문서 출처/링크 누락** — ①② "문서" 항목이 제목만, 클릭 링크(uri) 미노출. (⑦ dict엔 포함, 렌더 미반영) — 근거 추적 불가.

### 우선순위 중간
4. **회의록 검색 vs 목록 포맷 불일치** — 같은 `get_minutes`인데 검색=평문MD, 목록=Block. 통합 필요.
5. **기업(Block) vs 인물(평문300자) 리서치 포맷 이원화** — 인물만 구조화 수준 낮음.
6. **Trello/액션 mrkdwn 볼드 `**` 혼용** — Slack 정식은 `*…*`. 클라이언트별로 별표 노출. 표준화.
7. **평문 오류 메시지 톤·구조 불균일 (드림플러스 25+·등록 다수)** — 이모지(⚠️/❌/🔑) 규칙 없음. 공통 에러 템플릿화.
8. **다이제스트·주간보고 LLM 폴백 격차** — orchestrator 비활성 시 원시 나열로 품질 편차.

### 우선순위 낮음
9. 진행 메시지(🔍/👤/📨/💾) 산발 → 공통 유틸.
10. Todo 마감색상·D-day가 Todo에만 → 액션 리마인더와 통일.
11. 헤더 어젠다 줄바꿈 규칙 미정렬.

---

## 표준화 제안

공통 **출력 포맷 하네스**를 도입 — (a) 헤더(이모지+제목+출처 한 줄), (b) 본문(LLM 합성 또는 구조화 섹션), (c) 출처·링크 footer, (d) 에러 톤 4단계를 단일 빌더로 강제. 1차 대상은 **`tools/slack_tools.py` 블록 빌더 4종**(`build_company_research_block`·`build_persons_block`·`build_context_block`·헤더): 온톨로지 합성(grounding+critic), 이메일→이름 폴백 표기 규칙, 클릭 가능한 출처 링크를 일괄 주입하면 높음 1·2·3이 한 곳에서 해소. 2차로 **`agents/after.py`·`trello_report.py` mrkdwn 볼드(`**`→`*`) 정규화** + **드림플러스/등록 평문 에러 공통 템플릿**, 그리고 진행 메시지·D-day/색상 표기를 공유 유틸(`slack_tools.progress()`·`format_due()`)로 통일.
