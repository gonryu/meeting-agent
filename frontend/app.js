(function () {
  "use strict";

  const BACKEND = (window.BACKEND_URL || "").replace(/\/+$/, "");
  const main = document.getElementById("main");
  const logoutBtn = document.getElementById("logout");
  const backendLabel = document.getElementById("backend-label");
  backendLabel.textContent = BACKEND;

  const CATEGORY_LABEL = {
    bug_report: "🐞 버그",
    feature_request: "✨ 기능 요청",
    improvement: "🔧 개선",
  };

  function escapeHtml(s) {
    if (s == null) return "";
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function fmtDt(iso) {
    if (!iso) return "-";
    return String(iso).replace("T", " ").slice(0, 16);
  }

  // ── 인증 ────────────────────────────────────────────────
  function getAuth() {
    let auth = sessionStorage.getItem("admin_auth");
    if (!auth) {
      const pw = window.prompt("관리자 비밀번호를 입력하세요:");
      if (!pw) return null;
      auth = "Basic " + btoa("admin:" + pw);
      sessionStorage.setItem("admin_auth", auth);
      logoutBtn.style.display = "";
    }
    return auth;
  }

  function clearAuth() {
    sessionStorage.removeItem("admin_auth");
    logoutBtn.style.display = "none";
  }

  async function api(path) {
    const auth = getAuth();
    if (!auth) throw new Error("인증 취소됨");
    const res = await fetch(BACKEND + "/admin/api" + path, {
      headers: { Authorization: auth },
    });
    if (res.status === 401) {
      clearAuth();
      throw new Error("인증 실패 — 비밀번호를 확인해주세요");
    }
    if (res.status === 503) {
      throw new Error("서버에 ADMIN_PASSWORD가 설정되어 있지 않습니다");
    }
    if (!res.ok) {
      throw new Error("API 오류 " + res.status);
    }
    return res.json();
  }

  // ── 렌더링 ──────────────────────────────────────────────
  function setLoading(label) {
    main.innerHTML = '<div class="card"><div class="empty">' + escapeHtml(label) + "</div></div>";
  }

  async function renderDashboard() {
    setLoading("대시보드 불러오는 중...");
    const data = await api("/dashboard");
    const c = data.counts;
    const fb = data.recent_feedback || [];

    const stats = `
      <div class="stats">
        <div class="stat"><div class="label">등록 사용자</div>
          <div class="value">${c.users}</div></div>
        <div class="stat"><div class="label">회의록</div>
          <div class="value">${c.meetings}</div></div>
        <div class="stat"><div class="label">미처리 피드백</div>
          <div class="value">${c.feedback_pending}</div>
          <div class="sub">전체 ${c.feedback_total}건</div></div>
        <div class="stat"><div class="label">오픈 액션아이템</div>
          <div class="value">${c.action_open}</div>
          <div class="sub">전체 ${c.action_total}건</div></div>
      </div>
    `;

    let recent;
    if (fb.length === 0) {
      recent = '<div class="card"><h2>최근 피드백</h2><div class="empty">아직 수신된 피드백이 없습니다.</div></div>';
    } else {
      const rows = fb.map((f) => `
        <tr>
          <td>${escapeHtml(fmtDt(f.created_at))}</td>
          <td><span class="tag">${escapeHtml(CATEGORY_LABEL[f.category] || f.category)}</span></td>
          <td>${escapeHtml(f.user_name || f.user_id)}</td>
          <td>${escapeHtml(f.content)}</td>
          <td>${f.notified ? '<span class="tag ok">전송됨</span>' : '<span class="tag warn">대기</span>'}</td>
        </tr>`).join("");
      recent = `
        <div class="card">
          <h2>최근 피드백 <a href="#/feedback" class="small">전체 보기 →</a></h2>
          <table>
            <thead><tr><th>시각</th><th>유형</th><th>사용자</th><th>내용</th><th>상태</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      `;
    }

    main.innerHTML = stats + recent;
  }

  async function renderUsers() {
    setLoading("사용자 목록 불러오는 중...");
    const users = await api("/users");
    if (users.length === 0) {
      main.innerHTML = '<div class="card"><h2>사용자 목록</h2><div class="empty">등록된 사용자가 없습니다.</div></div>';
      return;
    }
    const rows = users.map((u) => `
      <tr>
        <td>
          <div>${escapeHtml(u.name || "—")}</div>
          <div class="muted small">${escapeHtml(u.email || "")}</div>
          <div class="muted small"><code>${escapeHtml(u.slack_user_id)}</code></div>
        </td>
        <td>${escapeHtml(fmtDt(u.registered_at))}</td>
        <td>${escapeHtml(fmtDt(u.last_active))}</td>
        <td class="center">${u.has_drive ? "🟢" : "—"}</td>
        <td class="center">${u.has_trello ? "🟢" : "—"}</td>
        <td class="center">${u.has_dreamplus ? "🟢" : "—"}</td>
        <td>${escapeHtml(String(u.briefing_hour))}시</td>
      </tr>`).join("");

    main.innerHTML = `
      <div class="card">
        <h2>사용자 목록 <span class="muted">(${users.length}명)</span></h2>
        <table>
          <thead><tr>
            <th>사용자</th><th>등록</th><th>최근 활동</th>
            <th>Drive</th><th>Trello</th><th>Dreamplus</th><th>브리핑</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;
  }

  async function renderFeedback() {
    const hash = location.hash;
    const params = new URLSearchParams((hash.split("?")[1] || ""));
    const filter = params.get("filter") || "all";

    setLoading("피드백 불러오는 중...");
    const data = await api("/feedback?filter=" + encodeURIComponent(filter));
    const items = data.items || [];

    const filterLink = (name, label) =>
      `<a href="#/feedback?filter=${name}" class="${filter === name ? "active" : ""}">${label}</a>`;

    const filters = `
      <div class="filters">
        ${filterLink("all", "전체")}${filterLink("pending", "미전송")}${filterLink("notified", "전송됨")}
      </div>
    `;

    if (items.length === 0) {
      main.innerHTML = filters + '<div class="card"><div class="empty">피드백이 없습니다.</div></div>';
      return;
    }

    const rows = items.map((f) => `
      <tr>
        <td class="nowrap">${escapeHtml(fmtDt(f.created_at))}</td>
        <td><span class="tag">${escapeHtml(CATEGORY_LABEL[f.category] || f.category)}</span></td>
        <td>
          <div>${escapeHtml(f.user_name || "—")}</div>
          <div class="muted small"><code>${escapeHtml(f.user_id)}</code></div>
        </td>
        <td>
          <pre class="content">${escapeHtml(f.content)}</pre>
          <div class="muted small" style="margin-top:6px">원문: ${escapeHtml(f.original)}</div>
        </td>
        <td>${f.notified ? '<span class="tag ok">전송됨</span>' : '<span class="tag warn">대기</span>'}</td>
      </tr>`).join("");

    main.innerHTML = filters + `
      <div class="card">
        <h2>피드백 <span class="muted">(${items.length}건)</span></h2>
        <table>
          <thead><tr>
            <th>시각</th><th>유형</th><th>사용자</th><th>내용</th><th>상태</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;
  }

  // ── 라우터 ──────────────────────────────────────────────
  const routes = {
    "#/dashboard": renderDashboard,
    "#/users": renderUsers,
    "#/feedback": renderFeedback,
  };

  function updateNav() {
    const path = location.hash.split("?")[0] || "#/dashboard";
    document.querySelectorAll("nav a").forEach((a) => {
      a.classList.toggle("active", a.getAttribute("href") === path);
    });
  }

  async function navigate() {
    if (!location.hash) {
      location.hash = "#/dashboard";
      return;
    }
    updateNav();
    const path = location.hash.split("?")[0];
    const render = routes[path];
    if (!render) {
      main.innerHTML = '<div class="card"><div class="empty">알 수 없는 페이지</div></div>';
      return;
    }
    try {
      await render();
    } catch (e) {
      main.innerHTML = `
        <div class="card">
          <h2>⚠️ 오류</h2>
          <p>${escapeHtml(e.message)}</p>
          <p class="muted small">네트워크 탭 확인 — 백엔드 CORS 설정 또는 BACKEND_URL(${escapeHtml(BACKEND)})을 점검해주세요.</p>
        </div>
      `;
    }
  }

  logoutBtn.addEventListener("click", () => {
    clearAuth();
    location.reload();
  });

  window.addEventListener("hashchange", navigate);
  window.addEventListener("load", navigate);

  if (sessionStorage.getItem("admin_auth")) {
    logoutBtn.style.display = "";
  }
})();
