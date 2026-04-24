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

  async function apiPost(path, body) {
    const auth = getAuth();
    if (!auth) throw new Error("인증 취소됨");
    const res = await fetch(BACKEND + "/admin/api" + path, {
      method: "POST",
      headers: {
        Authorization: auth,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    if (res.status === 401) {
      clearAuth();
      throw new Error("인증 실패");
    }
    if (!res.ok) {
      let msg = "API 오류 " + res.status;
      try { const j = await res.json(); if (j.detail) msg += " — " + j.detail; } catch (_) {}
      throw new Error(msg);
    }
    return res.json();
  }

  const RESOLUTION_LABEL = {
    pending: '<span class="tag">미반영</span>',
    applied: '<span class="tag ok">반영됨</span>',
    on_hold: '<span class="tag warn">보류</span>',
  };

  function resolutionCell(f) {
    const current = f.resolution || "pending";
    const label = RESOLUTION_LABEL[current] || RESOLUTION_LABEL.pending;
    let actions;
    if (current === "pending") {
      actions = `
        <button class="act-btn" data-fid="${f.id}" data-status="applied">반영</button>
        <button class="act-btn" data-fid="${f.id}" data-status="on_hold">보류</button>
      `;
    } else {
      actions = `<button class="act-btn" data-fid="${f.id}" data-status="pending">되돌리기</button>`;
    }
    return `${label}<div class="actions">${actions}</div>`;
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
        <div class="filter-group">
          <span class="filter-label">전송</span>
          ${filterLink("all", "전체")}${filterLink("pending", "미전송")}${filterLink("notified", "전송됨")}
        </div>
        <div class="filter-group">
          <span class="filter-label">반영</span>
          ${filterLink("unresolved", "아직 반영 안됨")}${filterLink("applied", "반영됨")}${filterLink("on_hold", "보류됨")}
        </div>
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
        <td>${resolutionCell(f)}</td>
      </tr>`).join("");

    main.innerHTML = filters + `
      <div class="card">
        <h2>피드백 <span class="muted">(${items.length}건)</span></h2>
        <table>
          <thead><tr>
            <th>시각</th><th>유형</th><th>사용자</th><th>내용</th><th>전송</th><th>반영</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;
  }

  // 피드백 반영 상태 변경 버튼 — 이벤트 위임
  main.addEventListener("click", async (e) => {
    const btn = e.target.closest(".act-btn");
    if (!btn) return;
    const fid = btn.getAttribute("data-fid");
    const status = btn.getAttribute("data-status");
    if (!fid || !status) return;
    btn.disabled = true;
    try {
      await apiPost(`/feedback/${fid}/resolution`, { status });
      await renderFeedback();
    } catch (err) {
      alert("반영 상태 변경 실패: " + err.message);
      btn.disabled = false;
    }
  });

  // ── 프롬프트 템플릿 ─────────────────────────────────────
  function fmtBytes(n) {
    if (n < 1024) return n + " B";
    return (n / 1024).toFixed(1) + " KB";
  }

  async function apiPut(path, body) {
    const auth = getAuth();
    if (!auth) throw new Error("인증 취소됨");
    const res = await fetch(BACKEND + "/admin/api" + path, {
      method: "PUT",
      headers: { Authorization: auth, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.status === 401) {
      clearAuth();
      throw new Error("인증 실패");
    }
    if (!res.ok) {
      let msg = "API 오류 " + res.status;
      try { const j = await res.json(); if (j.detail) msg += " — " + j.detail; } catch (_) {}
      throw new Error(msg);
    }
    return res.json();
  }

  async function renderPrompts() {
    const hash = location.hash;
    const params = new URLSearchParams(hash.split("?")[1] || "");
    const editing = params.get("edit");
    if (editing) return renderPromptEditor(editing);

    setLoading("프롬프트 목록 불러오는 중...");
    const items = await api("/prompts");
    if (items.length === 0) {
      main.innerHTML = '<div class="card"><h2>프롬프트 템플릿</h2><div class="empty">편집 가능한 템플릿이 없습니다.</div></div>';
      return;
    }
    const rows = items.map((it) => `
      <tr>
        <td><a href="#/prompts?edit=${encodeURIComponent(it.name)}"><code>${escapeHtml(it.name)}</code></a></td>
        <td>${escapeHtml(it.description || "—")}</td>
        <td class="nowrap">${fmtBytes(it.size)}</td>
        <td class="nowrap">${escapeHtml(fmtDt(it.modified_at))}</td>
      </tr>`).join("");
    main.innerHTML = `
      <div class="card">
        <h2>프롬프트 템플릿 <span class="muted">(${items.length}개)</span></h2>
        <p class="muted small">
          <code>prompts/templates/</code> 하위 <code>.md</code> 파일. 저장 시 이전 내용은 자동으로 <code>{name}.bak.{timestamp}</code>로 백업됩니다.
          서버 재시작 없이 즉시 반영됩니다.
        </p>
        <table>
          <thead><tr><th>파일명</th><th>설명</th><th>크기</th><th>수정 시각</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    `;
  }

  async function renderPromptEditor(name) {
    setLoading("템플릿 불러오는 중...");
    const data = await api("/prompts/" + encodeURIComponent(name));
    main.innerHTML = `
      <div class="card">
        <h2>
          <a href="#/prompts" class="small">← 목록</a>
          &nbsp;<code>${escapeHtml(data.name)}</code>
        </h2>
        <p class="muted small" id="p-meta">
          ${fmtBytes(data.size)} · 최종 수정: ${escapeHtml(fmtDt(data.modified_at))}
        </p>
        <textarea id="p-content" class="prompt-editor" spellcheck="false"></textarea>
        <div class="prompt-actions">
          <button id="p-save" class="btn-primary">저장</button>
          <a href="#/prompts" class="btn-cancel">취소</a>
          <span id="p-status" class="muted small"></span>
        </div>
      </div>
    `;
    const textarea = document.getElementById("p-content");
    const statusEl = document.getElementById("p-status");
    const saveBtn = document.getElementById("p-save");
    textarea.value = data.content;
    saveBtn.addEventListener("click", async () => {
      saveBtn.disabled = true;
      statusEl.textContent = "저장 중...";
      try {
        const r = await apiPut("/prompts/" + encodeURIComponent(name),
                                { content: textarea.value });
        statusEl.textContent = `저장 완료 — 백업: ${r.backup}`;
        statusEl.classList.add("ok");
        document.getElementById("p-meta").textContent =
          `${fmtBytes(r.size)} · 최종 수정: ${fmtDt(r.modified_at)}`;
      } catch (err) {
        statusEl.textContent = "저장 실패: " + err.message;
        statusEl.classList.add("err");
      } finally {
        saveBtn.disabled = false;
      }
    });
  }

  // ── 라우터 ──────────────────────────────────────────────
  const routes = {
    "#/dashboard": renderDashboard,
    "#/users": renderUsers,
    "#/feedback": renderFeedback,
    "#/prompts": renderPrompts,
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
