# 드림플러스 강남 회의실 예약 API 프로토콜

## 기본 정보

| 항목 | 값 |
|------|---|
| Base URL | `https://gangnam.dreamplus.asia` |
| Content-Type | `application/json; charset=UTF-8` |
| 날짜 형식 | `yyyy.MM.dd HH:mm:ss` |
| 인증 방식 | `authorization` 헤더에 `jwtToken` 전달 (Bearer 접두사 없음) |

## 인증

### 1. RSA 공개키 획득

```
POST /auth/publickey
```

- **Request Body**: 없음 (Content-Length: 0)
- **Response**:
```json
{
  "apiVersion": "1.0",
  "result": true,
  "code": "200",
  "data": {
    "RSAModulus": "<hex>",
    "RSAExponent": "10001",
    "publicKey": "<Base64 DER>"
  }
}
```

`publicKey`는 RSA 1024-bit 공개키 (DER 형식, Base64 인코딩).

### 2. 로그인

```
POST /auth/login
```

- **Request Body**:
```json
{
  "email": "mksong@parametacorp.com",
  "password": "<RSA 암호화된 비밀번호 Base64>",
  "finger_print": "<브라우저 핑거프린트 MurmurHash3>",
  "decryptRSA": 1,
  "publicKey": "<1단계에서 받은 publicKey>"
}
```

비밀번호 암호화: JSEncrypt 라이브러리로 `publicKey`를 사용하여 RSA PKCS1v1.5 암호화 후 Base64 인코딩.

#### finger_print 생성

[Fingerprint2.js](https://github.com/fingerprintjs/fingerprintjs2) 라이브러리를 사용한 브라우저 핑거프린트.

```javascript
Fingerprint2.get({
  preprocessor: function(key, value) {
    if (key === "userAgent") {
      // UA-Parser.js로 파싱하여 단순화
      return `${os.name},${os.version},${browser.name},${device.vendor},${device.model}`;
    }
    return value;
  },
  audio: { timeout: 3000 },
  excludes: {
    userAgent: false,                // 포함 (위 전처리기로 단순화)
    canvas: true,                    // 제외
    webgl: true,                     // 제외
    plugins: true,                   // 제외
    enumerateDevices: true,          // 제외
    pixelRatio: true,                // 제외
    screenResolution: true,          // 제외
    availableScreenResolution: true, // 제외
    doNotTrack: true,                // 제외
    fontsFlash: true,                // 제외
    webdriver: false                 // 포함
  }
}, function(components) {
  var values = components.map(c => c.value);
  var fingerprint = Fingerprint2.x64hash128(values.join(""), 31);
  // → "4745c59ebd0b08cd01973b42fe0d3db3" (32자 hex)
});
```

- 결과: **MurmurHash3 x64 128-bit** 해시값 (32자 hex 문자열)
- canvas, WebGL, 플러그인, 해상도 등은 제외 → 환경 변화에 덜 민감
- userAgent는 OS명/버전, 브라우저명, 디바이스만 추출하여 단순화
- `webdriver` 감지 포함 (자동화 도구 탐지)
- 동일 기기/브라우저에서는 항상 같은 값이 생성되므로, 자동화 시에는 한 번 생성한 값을 고정하여 재사용 가능

- **Response** (주요 필드):
```json
{
  "data": {
    "id": 103620,
    "name": "송문규",
    "email": "mksong@parametacorp.com",
    "companyId": 754,
    "companyName": "파라메타",
    "companyOaCode": "17104",
    "oaCode": "a1710435",
    "centerCode": 12,
    "jwtToken": "<암호화된 JWT>",
    "refreshToken": "<암호화된 리프레시 토큰>"
  }
}
```

이후 모든 API 요청에 `authorization: <jwtToken>` 헤더를 포함해야 한다.

## 암호화 (ek/ed)

일부 API는 요청 body를 RSA+AES 하이브리드 암호화로 전송한다.

### 알고리즘

| 항목 | 상세 |
|------|------|
| 대칭키 알고리즘 | **AES-256-CTR** |
| 비대칭키 알고리즘 | **RSA-1024 PKCS1v1.5** |
| AES 키 생성 | `crypto.randomBytes(24).toString("base64")` → 32자 문자열 |
| IV 생성 | `crypto.randomBytes(16)` → 16 bytes |
| 키 재사용 | 없음 (매 요청마다 새 AES 키 + 새 IV 생성) |

### 암호화 흐름

```
1. AES 키 생성
   keyStr = base64(randomBytes(24))  // 32자 문자열

2. ek 생성 (RSA로 AES 키 암호화)
   ek = base64(RSA_PKCS1v1.5_Encrypt(publicKey, Buffer.from(keyStr)))
   → 128 bytes, base64로 172 chars

3. ed 생성 (AES로 데이터 암호화)
   iv = randomBytes(16)
   cipher = createCipheriv("aes-256-ctr", keyStr, iv)
   ed = base64(iv + cipher.update(JSON.stringify(data)) + cipher.final())

4. 전송
   POST body: { "ek": "<ek>", "ed": "<ed>" }
```

### TypeScript 구현 예시 (Bun / Node.js)

```typescript
import crypto from "crypto";

function encryptRequest(
  data: Record<string, unknown>,
  publicKeyB64: string
): { ek: string; ed: string } {
  // 1. AES 키 생성 (24 random bytes → base64 → 32자 문자열)
  const keyStr = crypto.randomBytes(24).toString("base64"); // 32 chars

  // 2. ek: RSA PKCS1v1.5로 AES 키 암호화
  const pem = `-----BEGIN PUBLIC KEY-----\n${publicKeyB64}\n-----END PUBLIC KEY-----`;
  const ek = crypto.publicEncrypt(pem, Buffer.from(keyStr)).toString("base64");

  // 3. ed: AES-256-CTR로 데이터 암호화
  const iv = crypto.randomBytes(16);
  const plaintext = JSON.stringify(data);
  const cipher = crypto.createCipheriv("aes-256-ctr", keyStr, iv);
  const ct = Buffer.concat([cipher.update(plaintext, "utf-8"), cipher.final()]);
  const ed = Buffer.concat([iv, ct]).toString("base64");

  return { ek, ed };
}
```

### 암호화 대상 API

| API | 암호화 | 비고 |
|-----|--------|------|
| `POST /auth/login` | ✗ | 비밀번호만 RSA 암호화 |
| `POST /api2/meetingrooms` | ✗ | 평문 `{}` |
| `POST /api2/meetingroom/daily` | ✗ | 평문 |
| `POST /api2/meetingroom/reservation` (생성) | ✗ | 평문 |
| `POST /api2/meetingroom/reservations` (조회) | ✗ | 평문 |
| `GET /api2/meetingroom/refund/{id}` | ✗ | 평문 (GET) |
| `DELETE /api2/meetingroom/reservation` (취소) | **✓** | `{"id":<reservationId>}` |
| `POST /api2/invoice/point` | **✓** | 포인트 조회 |
| `POST /api2/invoice/upcoming` | **✓** | 예정 청구 조회 |
| `POST /api2/notices/main` | **✓** | 공지사항 조회 |
| `POST /api2/reservation/facility/price` | **✓** | 시설 가격 조회 |

JS 소스에서 `dataNoEncrypt: true` 옵션이 있는 API 호출은 암호화를 건너뛴다. 기본값은 암호화 적용.

## 회의실 API

### 3. 회의실 목록 조회

```
POST /api2/meetingrooms
Authorization: <jwtToken>
```

- **Request Body**: `{}`
- **Response**:
```json
{
  "list": [
    {
      "id": 2021,
      "roomCode": 201,
      "roomName": "Meeting Room 2A",
      "floor": 2,
      "maxMember": 8,
      "equipment": "TV, 화이트보드",
      "imageUrl": "https://dreamplus.center/assets/images/dpdm/rmap_2a.jpg",
      "point": 10000
    }
  ]
}
```

`point`는 30분 기준 사용 포인트.

### 4. 회의실 일일 가용 현황

```
POST /api2/meetingroom/daily
Authorization: <jwtToken>
```

- **Request Body**:
```json
{
  "startTime": "2026.04.01 16:00:00",
  "endTime": "2026.04.01 20:00:00"
}
```

- **Response** (수용인원별 집계):
```json
{
  "list": [
    {
      "maxMember": 4,
      "count": 8,
      "usedMinutes": 30,
      "totalMinutes": 1920
    },
    {
      "maxMember": 8,
      "count": 27,
      "usedMinutes": 1200,
      "totalMinutes": 6480
    }
  ]
}
```

### 5. 예약 생성

```
POST /api2/meetingroom/reservation
Authorization: <jwtToken>
```

- **Request Body** (평문, 암호화 없음):
```json
{
  "roomCode": 802,
  "startTime": "2026.04.01 20:00:00",
  "endTime": "2026.04.01 20:30:00",
  "title": "팀회의"
}
```

- **Response**: HTTP 200 (body 비어있음)

`roomCode`는 회의실 목록의 `roomCode` 필드 사용. 시간 단위는 30분.

### 6. 예약 목록 조회

```
POST /api2/meetingroom/reservations
Authorization: <jwtToken>
```

두 가지 조회 패턴이 있다.

**패턴 A — 당일 전체 예약 (메인 페이지)**:
```json
{
  "data": {
    "searchType": "startTime",
    "cancelDate": "2026.04.01 00:00:00",
    "startTime": "2026.04.01 00:00:00",
    "endTime": "2026.04.01 23:59:59"
  }
}
```

**패턴 B — 회사별 기간 조회 (마이페이지)**:
```json
{
  "page": 1,
  "size": 1000000,
  "date1": "2026.04.01",
  "date2": "2026.04.30",
  "order": "mr.start_time asc",
  "data": {
    "companyId": 754,
    "searchType": "insertDate"
  },
  "searchType": "startTime"
}
```

- **Response**:
```json
{
  "count": 1,
  "totalCount": 406354,
  "list": [
    {
      "id": 462420,
      "reservationState": 531,
      "reservationStateName": "예약 완료",
      "memberId": 103620,
      "memberName": "송문규",
      "companyName": "파라메타",
      "roomCode": 802,
      "roomName": "Meeting Room 8B",
      "point": 0,
      "title": "팀회의",
      "startTime": "2026.04.01 20:00",
      "endTime": "2026.04.01 20:30",
      "insertDate": "2026.04.01 16:17",
      "cancelDate": "2026.04.01 16:18"
    }
  ]
}
```

### 7. 환불 정보 조회

```
GET /api2/meetingroom/refund/{reservationId}
Authorization: <jwtToken>
```

- **Response**:
```json
{
  "data": {
    "id": 462420,
    "reservationState": 531,
    "reservationStateName": "예약 완료",
    "roomName": "Meeting Room 8B",
    "point": 0,
    "refund": 0,
    "title": "팀회의",
    "startTime": "2026.04.01 20:00",
    "endTime": "2026.04.01 20:30"
  }
}
```

### 8. 예약 취소

```
DELETE /api2/meetingroom/reservation
Authorization: <jwtToken>
Content-Type: application/json; charset=UTF-8
```

- **Request Body** (ek/ed 암호화):
```json
{
  "ek": "<RSA 암호화된 AES 키>",
  "ed": "<AES-256-CTR 암호화된 데이터>"
}
```

암호화 전 평문:
```json
{"id":462420}
```

- **Response**:
```json
{
  "apiVersion": "1.0",
  "result": true,
  "code": "200",
  "message": ""
}
```

취소 후 `reservationState`가 `531` → `532`로 변경된다.

## 예약 상태 코드

| 코드 | 상태 |
|------|------|
| 531 | 예약 완료 |
| 532 | 취소 완료 |
| 534 | 사용 완료 |

## 전체 플로우

```
[인증]
  POST /auth/publickey              → RSA 공개키 획득
  POST /auth/login                  → jwtToken 획득

[회의실 조회]
  POST /api2/meetingrooms           → 전체 회의실 목록
  POST /api2/meetingroom/daily      → 시간대별 가용 현황

[예약 생성]
  POST /api2/meetingroom/reservation  → roomCode, startTime, endTime, title (평문)

[예약 조회]
  POST /api2/meetingroom/reservations → 예약 목록 확인

[예약 취소]
  GET  /api2/meetingroom/refund/{id}  → 환불 금액 사전 확인
  DELETE /api2/meetingroom/reservation → {"id": reservationId} (ek/ed 암호화)

[취소 확인]
  POST /api2/meetingroom/reservations → reservationState 532 확인
```

## 공통 응답 형식

```json
{
  "apiVersion": "1.0",
  "result": true,
  "code": "200",
  "message": "",
  "data": {},
  "list": [],
  "count": 0,
  "totalCount": 0
}
```

`result: false`이면 에러. `code`와 `message`로 상세 확인.

## 회의실 목록 (참고)

| roomCode | 이름 | 층 | 인원 | 포인트/30분 |
|----------|------|---|------|------------|
| 201 | Meeting Room 2A | 2 | 8 | 10,000 |
| 202 | Meeting Room 2B | 2 | 18 | 20,000 |
| 203~209 | Meeting Room 2C~2I | 2 | 4~8 | 10,000 |
| 210 | Meeting Room 2J | 2 | 18 | 20,000 |
| 301~305 | Meeting Room 3A~3E | 3 | 4~8 | 10,000 |
| 802 | Meeting Room 8B | 8 | - | - |
| ... | (총 43개 회의실, 2층~17층) | | | |
