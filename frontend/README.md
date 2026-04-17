# Meeting Agent Admin Frontend

관리자 대시보드 SPA. 프로덕션에서는 FastAPI(`server/oauth.py`)가 `/admin/` 경로로 정적 서빙하고, 로컬 standalone dev에서는 본 디렉터리를 `python3 -m http.server`로 직접 띄워 운영·로컬 백엔드에 붙습니다.

## 실행 방식

### A. 프로덕션·같은 호스트 로컬 테스트 (권장)

백엔드가 `/admin/`으로 프론트엔드를 직접 서빙:

```bash
cd .. && bash start.sh
# → http://localhost:8000/admin/
```

`config.js`의 `BACKEND_URL`이 자동으로 빈 문자열(same-origin)로 잡힙니다.

### B. standalone dev (백엔드와 포트를 분리해 프론트만 반복 수정)

```bash
./serve.sh
# → http://localhost:3030 (PORT=xxxx ./serve.sh 로 변경 가능)
```

`config.js`가 port `3030`을 감지해 백엔드를 `http://localhost:8000`으로 자동 설정합니다.

접속 시 비밀번호 프롬프트가 뜹니다. 서버 `.env`의 `ADMIN_PASSWORD` 값을 입력하세요.
인증 상태는 `sessionStorage`에 저장되어 탭을 닫으면 자동으로 해제됩니다.

## 백엔드 URL 수동 변경

기본 자동 감지가 맞지 않을 때만 `config.js`를 편집:

```js
window.BACKEND_URL = "https://meeting.parametacorp.com";   // 또는 http://localhost:8000
```

## 구조

- `index.html` — 단일 HTML 셸
- `config.js` — 백엔드 URL (git 커밋됨)
- `app.js` — 라우팅·API 호출·렌더링 (vanilla JS)
- `style.css` — 스타일
- `serve.sh` — 로컬 개발 서버 런처 (`python3 -m http.server`)

빌드 도구 없음. Node.js 불필요.

## 백엔드 요구사항

- `/admin/api/dashboard`, `/admin/api/users`, `/admin/api/feedback` 엔드포인트
- HTTP Basic Auth — 비밀번호가 `ADMIN_PASSWORD` 환경변수와 일치
- CORS — `http://localhost:3030`, `http://localhost:3000` 허용 (기본값)

추가 허용 오리진은 백엔드 `.env`의 `ADMIN_FRONTEND_ORIGINS` (쉼표 구분)에 설정.
