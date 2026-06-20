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

  const MSG_CATEGORY_LABEL = {
    briefing: "📢 브리핑",
    minutes: "📝 회의록",
    action_item: "✅ 액션",
    meeting_alarm: "⏰ 미팅알람",
    room: "🏢 회의실",
    proposal: "📄 제안서",
    feedback: "💬 피드백",
    other: "··· 기타",
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
    const ms = data.message_stats || { total: 0, failures: 0, active_recipients: 0 };

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
        <div class="stat"><div class="label">오늘 발송</div>
          <div class="value">${ms.total}</div></div>
        <div class="stat"><div class="label">오늘 발송 실패</div>
          <div class="value">${ms.failures}</div>
          <div class="sub">${ms.failures > 0 ? '<a href="#/messages?ok=0">실패 보기 →</a>' : "정상"}</div></div>
        <div class="stat"><div class="label">오늘 수신 사용자</div>
          <div class="value">${ms.active_recipients}</div></div>
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
    const uidParam = new URLSearchParams(location.hash.split("?")[1] || "").get("uid");
    if (uidParam) return renderUserDetail(uidParam);
    setLoading("사용자 목록 불러오는 중...");
    const users = await api("/users");
    if (users.length === 0) {
      main.innerHTML = '<div class="card"><h2>사용자 목록</h2><div class="empty">등록된 사용자가 없습니다.</div></div>';
      return;
    }
    const rows = users.map((u) => `
      <tr>
        <td>
          <div><a href="#/users?uid=${encodeURIComponent(u.slack_user_id)}">${escapeHtml(u.name || "—")}</a></div>
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

  async function renderUserDetail(uid) {
    setLoading("사용자 상세 불러오는 중...");
    const [users, msgData] = await Promise.all([
      api("/users"),
      api("/users/" + encodeURIComponent(uid) + "/messages"),
    ]);
    const u = (users || []).find((x) => x.slack_user_id === uid) || { slack_user_id: uid };
    const items = msgData.items || [];
    const uname = escapeHtml(u.name || uid);
    const bubbles = items.map((m) => {
      const inbound = m.direction === "inbound";
      const who = inbound ? `👤 ${uname}` : "🤖 봇";
      const cat = inbound ? "" : `<span class="tag">${escapeHtml(MSG_CATEGORY_LABEL[m.category] || m.category || "기타")}</span>`;
      const fail = (!inbound && !m.ok) ? ' <span class="tag warn">실패</span>' : "";
      const raw = m.text || "";
      const txt = escapeHtml(raw.slice(0, 500)) + (raw.length > 500 ? "…" : "");
      return `
        <div class="chat-row ${inbound ? "chat-in" : "chat-out"}">
          <div class="chat-meta">${who} · ${escapeHtml(fmtDt(m.ts))} ${cat}${fail}</div>
          <div class="chat-bubble">${txt || "(본문 없음)"}</div>
        </div>`;
    }).join("");
    main.innerHTML = `
      <div class="card">
        <h2><a href="#/users" class="small">← 사용자 목록</a>&nbsp;${uname}</h2>
        <p class="muted small">
          <code>${escapeHtml(uid)}</code> · ${escapeHtml(u.email || "")} ·
          Drive ${u.has_drive ? "🟢" : "—"} / Trello ${u.has_trello ? "🟢" : "—"} / Dreamplus ${u.has_dreamplus ? "🟢" : "—"}
        </p>
      </div>
      <div class="card">
        <h2>대화 타임라인 <span class="muted">(${items.length}건)</span></h2>
        ${items.length === 0
          ? '<div class="empty">기록된 대화가 없습니다.</div>'
          : `<div class="chat">${bubbles}</div>`}
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

  // 메시지 행 클릭 → 상세
  main.addEventListener("click", (e) => {
    const row = e.target.closest(".msg-row");
    if (!row) return;
    const p = new URLSearchParams(msgFilterParams());
    p.set("id", row.getAttribute("data-id"));
    location.hash = "#/messages?" + p.toString();
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

  // ── 메시지 로그 ─────────────────────────────────────────
  function msgFilterParams() {
    const qs = location.hash.split("?")[1] || "";
    return new URLSearchParams(qs);
  }

  async function renderMessages() {
    const params = msgFilterParams();
    if (params.get("id")) return renderMessageDetail(params.get("id"));

    const LIMIT = 100;
    const offset = parseInt(params.get("offset") || "0", 10) || 0;

    setLoading("메시지 불러오는 중...");
    const apiParams = new URLSearchParams(params);
    apiParams.delete("id");
    apiParams.set("limit", LIMIT);
    apiParams.set("offset", offset);
    const data = await api("/messages?" + apiParams.toString());
    const items = data.items || [];

    const cat = params.get("category") || "";
    const okv = params.get("ok");
    const userv = params.get("user") || "";
    const qv = params.get("q") || "";
    const dfrom = (params.get("date_from") || "").slice(0, 10);
    const dto = (params.get("date_to") || "").slice(0, 10);

    const catLink = (name, label) => {
      const p = new URLSearchParams(params);
      if (name) p.set("category", name); else p.delete("category");
      p.delete("id"); p.delete("offset");
      return `<a href="#/messages?${p.toString()}" class="${cat === name ? "active" : ""}">${label}</a>`;
    };
    const okLink = (val, label) => {
      const p = new URLSearchParams(params);
      if (val === null) p.delete("ok"); else p.set("ok", val);
      p.delete("id"); p.delete("offset");
      const cur = okv == null ? "" : okv;
      return `<a href="#/messages?${p.toString()}" class="${String(cur) === String(val ?? "") ? "active" : ""}">${label}</a>`;
    };

    const filters = `
      <div class="filters">
        <div class="filter-group">
          <span class="filter-label">유형</span>
          ${catLink("", "전체")}${catLink("briefing", "브리핑")}${catLink("minutes", "회의록")}${catLink("action_item", "액션")}${catLink("meeting_alarm", "미팅알람")}${catLink("other", "기타")}
        </div>
        <div class="filter-group">
          <span class="filter-label">발송</span>
          ${okLink(null, "전체")}${okLink("1", "성공")}${okLink("0", "실패")}
        </div>
        <div class="filter-group">
          <span class="filter-label">기간</span>
          <input id="msg-date-from" class="msg-search" type="date" value="${escapeHtml(dfrom)}" style="min-width:140px">
          <span class="muted">~</span>
          <input id="msg-date-to" class="msg-search" type="date" value="${escapeHtml(dto)}" style="min-width:140px">
        </div>
        <div class="filter-group">
          <input id="msg-search" class="msg-search" placeholder="본문 검색…" value="${escapeHtml(qv)}">
          <button id="msg-search-btn" class="act-btn">검색</button>
          ${userv ? `<span class="muted small">수신자 필터: <code>${escapeHtml(userv)}</code></span>` : ""}
        </div>
      </div>
    `;

    const navParams = (off) => {
      const p = new URLSearchParams(params);
      p.delete("id");
      if (off > 0) p.set("offset", off); else p.delete("offset");
      return p.toString();
    };
    const prevCtl = offset > 0
      ? `<a href="#/messages?${navParams(Math.max(0, offset - LIMIT))}">← 이전</a>`
      : '<span class="muted">← 이전</span>';
    const nextCtl = items.length === LIMIT
      ? `<a href="#/messages?${navParams(offset + LIMIT)}">다음 →</a>`
      : '<span class="muted">다음 →</span>';
    const pager = (items.length === 0 && offset === 0)
      ? ""
      : `<div class="filter-group" style="margin-top:10px">${prevCtl}<span class="muted small">${items.length ? `${offset + 1}–${offset + items.length}` : "0"}</span>${nextCtl}</div>`;

    const rows = items.map((m) => `
        <tr class="msg-row" data-id="${m.id}" style="cursor:pointer">
          <td class="nowrap">${escapeHtml(fmtDt(m.ts))}</td>
          <td>
            <div>${escapeHtml(m.recipient_name || "—")}</div>
            <div class="muted small"><code>${escapeHtml(m.recipient_user_id || m.channel || "")}</code></div>
          </td>
          <td><span class="tag">${escapeHtml(MSG_CATEGORY_LABEL[m.category] || m.category || "기타")}</span></td>
          <td>${m.ok ? '<span class="tag ok">성공</span>' : '<span class="tag warn">실패</span>'}</td>
          <td>${escapeHtml((m.text || "").slice(0, 80))}${(m.text || "").length > 80 ? "…" : ""}</td>
        </tr>`).join("");
    const inner = items.length === 0
      ? '<div class="empty">메시지가 없습니다.</div>'
      : `<table>
            <thead><tr><th>시각</th><th>수신자</th><th>유형</th><th>발송</th><th>본문</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>`;
    const body = `<div class="card">
          <h2>메시지 로그 <span class="muted">(${items.length}건${offset > 0 ? `, offset ${offset}` : ""})</span></h2>
          ${inner}
          ${pager}
        </div>`;
    main.innerHTML = filters + body;

    const searchBtn = document.getElementById("msg-search-btn");
    const searchInput = document.getElementById("msg-search");
    const doSearch = () => {
      const p = new URLSearchParams(params);
      const v = searchInput.value.trim();
      if (v) p.set("q", v); else p.delete("q");
      p.delete("id"); p.delete("offset");
      location.hash = "#/messages?" + p.toString();
    };
    searchBtn.addEventListener("click", doSearch);
    searchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(); });

    const applyDates = () => {
      const p = new URLSearchParams(params);
      p.delete("id"); p.delete("offset");
      const f = document.getElementById("msg-date-from").value;
      const t = document.getElementById("msg-date-to").value;
      if (f) p.set("date_from", f); else p.delete("date_from");
      if (t) p.set("date_to", t + "T23:59:59"); else p.delete("date_to");
      location.hash = "#/messages?" + p.toString();
    };
    document.getElementById("msg-date-from").addEventListener("change", applyDates);
    document.getElementById("msg-date-to").addEventListener("change", applyDates);
  }

  async function renderMessageDetail(id) {
    setLoading("메시지 불러오는 중...");
    const m = await api("/messages/" + encodeURIComponent(id));
    const back = new URLSearchParams(msgFilterParams());
    back.delete("id");
    let blocks = "";
    if (m.blocks_json) {
      let pretty = m.blocks_json;
      try { pretty = JSON.stringify(JSON.parse(m.blocks_json), null, 2); } catch (_) {}
      blocks = `<h3>blocks</h3><pre class="content">${escapeHtml(pretty)}</pre>`;
    }
    main.innerHTML = `
      <div class="card">
        <h2><a href="#/messages?${back.toString()}" class="small">← 목록</a>&nbsp;메시지 #${escapeHtml(String(m.id))}</h2>
        <p class="muted small">
          ${escapeHtml(fmtDt(m.ts))} · ${escapeHtml(m.method)} ·
          수신자 ${escapeHtml(m.recipient_name || m.recipient_user_id || m.channel || "—")} ·
          ${m.ok ? "성공" : "실패: " + escapeHtml(m.error || "")}
        </p>
        <h3>text</h3>
        <pre class="content">${escapeHtml(m.text || "(없음)")}</pre>
        ${blocks}
      </div>
    `;
  }

  // ── 라우터 ──────────────────────────────────────────────
  const routes = {
    "#/dashboard": renderDashboard,
    "#/users": renderUsers,
    "#/feedback": renderFeedback,
    "#/messages": renderMessages,
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
