# 드림플러스 강남 회의실 예약 시스템 API 분석 문서

> **분석 대상**: https://gangnam.dreamplus.asia/reservation/meetingroom  
> **분석 일자**: 2026-03-30  
> **분석 방법**: 브라우저 DevTools 네트워크 인터셉터, JS 소스 정적 분석, API 직접 호출 테스트

---

## 1. 시스템 개요

드림플러스 강남 회의실 예약 시스템은 **Vue.js 기반 SPA(Single Page Application)**으로 구성되어 있으며, 백엔드와 REST API(`/api2/...`)로 통신한다.

- **프론트엔드**: Vue.js + Vuex + Vue Router
- **인증 방식**: JWT (JSON Web Token)
- **API Base URL**: `https://gangnam.dreamplus.asia`
- **API Prefix**: `/api2/`
- **데이터 교환 형식**: JSON

---

## 2. 인증 (Authentication)

### 2.1 인증 흐름

```
1. 이메일 + 비밀번호로 로그인
   POST /api2/login  →  jwtToken + refreshToken 발급

2. 이후 모든 API 요청에 JWT를 헤더에 포함
   Authorization: Bearer <jwtToken>

3. JWT 만료 시 refreshToken으로 재발급
   (refresh 엔드포인트 확인 필요)
```

### 2.2 토큰 저장 위치

| 저장소 | 키 | 내용 |
|---|---|---|
| `sessionStorage` | `meInfo` | 사용자 전체 정보 + JWT |
| `sessionStorage` | `profile` | 사용자 프로필 요약 |
| `sessionStorage` | `userInfo` | 암호화된 사용자 정보 |
| `sessionStorage` | `publicKey` | RSA 공개키 (비밀번호 암호화용) |
| `sessionStorage` | `fingerPrint` | 브라우저 핑거프린트 |

### 2.3 meInfo 객체 구조 (sessionStorage)

로그인 성공 시 `sessionStorage.meInfo`에 저장되는 필드:

```json
{
  "schema": "dreamplus",
  "centerId": "1",
  "id": 103614,
  "name": "사용자명",
  "email": "user@company.com",
  "phone": "010-XXXX-XXXX",
  "companyId": 754,
  "companyName": "회사명",
  "oaCode": "...",
  "centerCode": "...",
  "jwtToken": "<JWT 액세스 토큰>",
  "refreshToken": "<리프레시 토큰>",
  "isAdmin": false,
  "isAdminActive": false,
  "roles": [...],
  "enabled": true,
  "username": "user@company.com",
  "lastLoginDate": "...",
  "insertDate": "...",
  "updateDate": "..."
}
```

### 2.4 API 공통 요청 헤더

```
Content-Type: application/json
Access-Control-Allow-Origin: *
Access-Control-Allow-Headers: *
Authorization: Bearer <jwtToken>
```

### 2.5 비밀번호 암호화

- 로그인 시 비밀번호는 평문으로 전송되지 않음
- `sessionStorage.publicKey`에 저장된 **RSA 공개키**로 암호화 후 전송
- 라이브러리: `browserify-sign`, `public-encrypt` (페이지 로드 JS에서 확인)

---

## 3. 공통 API 응답 구조

모든 API는 동일한 래퍼 구조로 응답한다:

```json
{
  "apiVersion": "1.0",
  "result": true,          // 성공: true / 실패: false
  "code": "200",           // HTTP 상태 코드 유사
  "message": "...",        // 메시지 (오류 시 상세 내용)
  "data": { ... }          // 실제 데이터 (배열 또는 객체)
}
```

### 주요 응답 코드

| code | 의미 |
|---|---|
| `200` | 성공 |
| `301` | JWT 토큰 만료 (`"JWT 토큰이 만료되었습니다."`) |
| `401` | 인증 실패 |
| `400` | 잘못된 요청 |

---

## 4. 회의실 관련 API 엔드포인트

### 4.1 회의실 목록 조회

**`POST /api2/meetingrooms`**

페이지 로드 및 날짜 변경 시 자동 호출된다.

#### 요청 바디

```json
{
  "data": {
    "date": "2026-03-30"
  }
}
```

> 날짜 필터, 시간 필터, 인원 필터도 data에 포함될 것으로 추정 (추가 분석 필요)

#### 응답 구조

```json
{
  "apiVersion": "1.0",
  "result": true,
  "code": "200",
  "data": [
    {
      "id": "<회의실 ID>",
      "name": "Meeting Room 2A",
      "floor": "2",
      "capacity": 8,
      "amenities": ["TV", "화이트보드"],
      "centerId": "1",
      ...
    },
    ...
  ]
}
```

- 총 **38개** 회의실 반환 (기본 날짜 기준)
- **층별 구성**: 2F, 3F, 7F, 8F, 9F, 10F, 11F, 12F, 13F, 14F, 16F, 17F, 19F

#### 확인된 회의실 목록 (2F 기준 예시)

| 회의실명 | 수용 인원 | 비품 |
|---|---|---|
| Meeting Room 2A | 8명 | TV, 화이트보드 |
| Meeting Room 2B | 18명 | 빔프로젝터, 화이트보드 |
| Meeting Room 2C | 6명 | TV, 화이트보드 |
| Meeting Room 2D | 6명 | TV, 화이트보드 |
| Meeting Room 2E | 6명 | TV, 화이트보드 |
| Meeting Room 2F | 8명 | TV, 화이트보드 |
| Meeting Room 2G | 8명 | TV, 화이트보드 |
| Meeting Room 2H | 4명 | TV, 화이트보드 |
| Meeting Room 2I | 4명 | TV, 화이트보드 |
| Meeting Room 2J | (미확인) | - |

---

### 4.2 예약 목록 조회

**`POST /api2/meetingroom/reservations`**

날짜 변경 시 해당 날짜의 예약 현황을 조회한다.

#### 요청 바디 (실제 캡처)

```json
{
  "data": {
    "searchType": "startTime",
    "cancelDate": "2026.04.04 00:00:00",
    "startTime": "2026.04.04 00:00:00",
    "endTime": "2026.04.04 23:59:59"
  }
}
```

| 필드 | 타입 | 설명 |
|---|---|---|
| `searchType` | string | 검색 기준 (`"startTime"` 고정으로 보임) |
| `cancelDate` | string | 조회 기준 날짜 (`"YYYY.MM.DD 00:00:00"` 형식) |
| `startTime` | string | 조회 시작 시각 (`"YYYY.MM.DD 00:00:00"`) |
| `endTime` | string | 조회 종료 시각 (`"YYYY.MM.DD 23:59:59"`) |

> **날짜 형식 주의**: `YYYY.MM.DD HH:mm:ss` (점(.) 구분자 사용)

---

### 4.3 예약 생성

**`POST /api2/meetingroom/reservation`**

실제 예약을 생성하는 엔드포인트 (JS 소스에서 확인, 페이로드 구조는 추가 분석 필요).

#### 추정 요청 바디 구조

```json
{
  "data": {
    "meetingRoomId": "<회의실 ID>",
    "centerId": "1",
    "startTime": "2026.04.04 14:00:00",
    "endTime": "2026.04.04 15:00:00",
    "title": "회의 제목",
    "attendees": 5
  }
}
```

> ⚠️ 위 구조는 추정값임. 실제 예약 시도를 통해 검증 필요.

---

### 4.4 예약 취소/환불

**`POST /api2/meetingroom/refund/<reservationId>`**

JS 소스에서 확인된 환불/취소 엔드포인트.

---

### 4.5 일별 예약 현황

**`POST /api2/meetingroom/daily`**

JS 소스에서 확인된 일별 조회 엔드포인트 (세부 구조 추가 분석 필요).

---

## 5. 로그인 관련 API

### 5.1 이메일 ID 찾기

**`GET /api2/login/email/search`**

### 5.2 이메일 인증 코드 전송

**`POST /api2/login/email/send`**

### 5.3 비밀번호 설정

**`POST /api2/login/password/set`**

### 5.4 비밀번호 초기화

**`POST /api2/login/password/reset`**

### 5.5 비밀번호 3개월 경과 처리

**`POST /api2/login/password/3months`**

### 5.6 비밀번호 변경 스킵

**`POST /api2/login/skipChangePassword`**

---

## 6. 기타 관련 API (전체 엔드포인트 목록)

JS 소스 정적 분석으로 확인된 전체 엔드포인트:

```
# 회의실
POST /api2/meetingrooms                    # 회의실 목록
POST /api2/meetingroom/reservations        # 예약 목록 조회
POST /api2/meetingroom/reservation         # 예약 생성
POST /api2/meetingroom/refund/<id>         # 예약 취소/환불
POST /api2/meetingroom/daily               # 일별 현황

# 포인트/청구
GET  /api2/invoice/point/meetingroom       # 회의실 포인트 청구 내역

# 미디어센터
POST /api2/mediacenter/checkReservation
POST /api2/mediacenter/items
POST /api2/mediacenter/reservation
POST /api2/mediacenter/reservations
POST /api2/mediacenter/reservationInfos

# 이벤트홀
POST /api2/eventhall/facility
POST /api2/venue/reservations
POST /api2/venue/holidays
POST /api2/venue/reservation

# 공통
POST /api2/commonCodes                     # 공통 코드 목록
GET  /api2/file                            # 파일 업/다운로드
POST /api2/company/pass                    # 회사 패스 정보

# 공지/이벤트
GET  /api2/events
GET  /api2/event/top
POST /api2/event/apply
GET  /api2/notice
POST /api2/notice/<id>
POST /api2/commentpaging
POST /api2/comment
POST /api2/comments
```

---

## 7. 브라우저 자동화 방식 (Claude in Chrome)

스킬 구현 시 API 직접 호출이 어려울 경우, 브라우저 자동화 방식을 사용할 수 있다.

### 7.1 로그인 자동화 절차

```
1. https://gangnam.dreamplus.asia/login 이동
2. 이메일 입력란 클릭 → 이메일 입력
3. 비밀번호 입력란 클릭 → 비밀번호 입력
4. "로그인" 버튼 클릭
5. 리다이렉트 완료 확인 (URL이 /login이 아닌지 체크)
```

### 7.2 회의실 예약 UI 절차

```
1. https://gangnam.dreamplus.asia/reservation/meetingroom 이동
2. 날짜 선택 (< > 버튼 또는 캘린더 아이콘)
3. 시간 필터 설정 (시작 시간 ~ 종료 시간)
4. 인원 필터 설정 (모든인실 드롭다운)
5. 층 탭 선택 (2F / 3F / 7F / ...)
6. 원하는 회의실 카드 클릭
7. 예약 상세 폼에서 시간대 선택 후 확인
```

---

## 8. 기술적 제약사항 및 주의사항

### 8.1 세션 만료

- JWT는 세션 기반으로 `sessionStorage`에 저장됨
- **탭/브라우저를 닫으면 세션 소멸** → 재로그인 필요
- JWT 만료 시 응답: `{"code": "301", "message": "JWT 토큰이 만료되었습니다."}`

### 8.2 비밀번호 암호화

- 로그인 시 비밀번호는 **RSA 공개키로 암호화** 후 전송
- 공개키는 페이지 로드 시 `sessionStorage.publicKey`에 저장
- 직접 API 호출 시 암호화 처리 구현 필요

### 8.3 날짜/시간 형식

- API 요청 시 날짜 형식: **`YYYY.MM.DD HH:mm:ss`** (점 구분자)
- 예: `"2026.04.04 14:00:00"`

### 8.4 요청 바디 래핑

- 모든 POST 요청의 실제 데이터는 **`data` 키로 래핑**됨
- 예: `{ "data": { "startTime": "...", "endTime": "..." } }`

### 8.5 CORS

- `Access-Control-Allow-Origin: *` 헤더를 요청에 포함
- 동일 도메인 내 요청에서는 쿠키 기반 세션도 병행 사용 가능

---

## 9. 추가 분석 필요 항목

| 항목 | 상태 | 비고 |
|---|---|---|
| 로그인 API 요청 페이로드 (암호화 방식) | ⏳ 미완 | RSA 암호화 구현 필요 |
| 회의실 목록 조회 전체 필터 파라미터 | ⏳ 미완 | 시간/인원 필터 구조 |
| 예약 생성 API 실제 페이로드 | ⏳ 미완 | 실제 예약 시도로 검증 필요 |
| JWT refresh 엔드포인트 | ⏳ 미완 | `/api2/login` 또는 별도 엔드포인트 |
| 예약 취소 API 페이로드 | ⏳ 미완 | refund 엔드포인트 구조 |
| 회의실 ID 값 목록 | ⏳ 미완 | meetingrooms 응답에서 확인 필요 |

---

## 10. 스킬 구현 권장 방식

분석 결과를 바탕으로 두 가지 구현 방식을 권장한다:

### 방식 A: Claude in Chrome 브라우저 자동화 (권장)
- **장점**: 로그인/암호화 복잡성 없음, UI 변경에도 어느 정도 유연
- **단점**: 브라우저가 열려 있어야 함, 속도 느림
- **적합 상황**: 즉시 구현, 안정적 운영

### 방식 B: API 직접 호출
- **장점**: 빠름, 백그라운드 실행 가능
- **단점**: RSA 암호화 구현 필요, 세션 관리 복잡
- **적합 상황**: 자동화 파이프라인, 다중 예약 관리

---

*이 문서는 브라우저 분석을 통해 리버스 엔지니어링한 내용이며, 공식 API 문서가 아닙니다.*
