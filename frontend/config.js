// 관리자 프론트엔드 설정.
// - 로컬 standalone dev (http-server로 :3030 구동): 백엔드 http://localhost:8000
// - 그 외 (백엔드가 meeting.parametacorp.com/admin/으로 직접 서빙하는 프로덕션): same-origin
window.BACKEND_URL =
  window.location.port === "3030"
    ? "http://localhost:8000"
    : "";
