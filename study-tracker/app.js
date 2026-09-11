// ---------------------------------------------------------------
// app.js
// 画面の描画・状態管理・イベント処理
// ---------------------------------------------------------------

const state = {
  settings: Storage.getSettings(),
  view: null,
  connected: false,
  connecting: false,
  calendars: [],
  events: [],
  loadingEvents: false,
  loadError: null,
  plan: emptyPlan(),
  settingsDraft: null,
};

function emptyPlan() {
  return { title: "", date: todayKey(), startTime: "19:00", durationMinutes: 60, memo: "" };
}

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function escapeHtml(str) {
  return String(str ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function showToast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => el.classList.remove("show"), 2400);
}

// ---------------- 初期化・ルーティング ----------------

function init() {
  state.settings = Storage.getSettings();
  state.connected = GCal.isConnected();
  state.view = state.settings.clientId ? "home" : "onboarding";

  render();

  document.body.addEventListener("click", onBodyClick);
  document.body.addEventListener("submit", onBodySubmit);

  if (state.connected) {
    GCal.ensureGapi()
      .then(() => {
        const saved = GCal.getSavedToken();
        if (saved) window.gapi.client.setToken({ access_token: saved.access_token });
        refreshEvents();
      })
      .catch(() => {
        state.connected = false;
        render();
      });
  }
}

function setView(view) {
  state.view = view;
  if (view === "plan") state.plan = emptyPlan();
  if (view === "settings") state.settingsDraft = { ...state.settings };
  render();
  window.scrollTo(0, 0);
}

function render() {
  const app = $("#app");
  const tabbar = $("#tabbar");
  const showTabs = state.view !== "onboarding";
  tabbar.classList.toggle("hidden", !showTabs);

  $$(".tab-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === state.view);
  });

  switch (state.view) {
    case "onboarding": app.innerHTML = renderOnboarding(); break;
    case "home": app.innerHTML = renderHome(); break;
    case "plan": app.innerHTML = renderPlan(); break;
    case "history": app.innerHTML = renderHistory(); break;
    case "settings": app.innerHTML = renderSettings(); break;
    default: app.innerHTML = renderHome();
  }
}

// ---------------- Googleカレンダー連携 ----------------

function matchesFilter(event, settings) {
  if (!settings.keyword) return true;
  const kw = settings.keyword.toLowerCase();
  const text = `${event.summary || ""} ${event.description || ""}`.toLowerCase();
  return text.includes(kw);
}

function eventStart(event) {
  return new Date(event.start.dateTime || event.start.date);
}
function eventEnd(event) {
  return new Date(event.end.dateTime || event.end.date);
}
function isTimedEvent(event) {
  return !!(event.start && event.start.dateTime);
}
function eventDurationMinutes(event) {
  if (!isTimedEvent(event)) return null;
  return Math.max(0, (eventEnd(event) - eventStart(event)) / 60000);
}

// 実績(ローカル保存の上書き)。未設定の場合、終了済みなら「完了」扱いにする
function effectiveStatus(event) {
  const saved = Storage.getProgress(event.id);
  if (saved) return saved;
  const planned = eventDurationMinutes(event) || 0;
  const isPast = eventEnd(event).getTime() < Date.now();
  return { status: isPast ? "done" : "planned", actualMinutes: isPast ? planned : 0, memo: "" };
}

async function refreshEvents() {
  if (!state.connected) return;
  state.loadingEvents = true;
  state.loadError = null;
  render();
  try {
    const now = new Date();
    const timeMin = new Date(now); timeMin.setDate(timeMin.getDate() - 30); timeMin.setHours(0, 0, 0, 0);
    const timeMax = new Date(now); timeMax.setDate(timeMax.getDate() + 14); timeMax.setHours(23, 59, 59, 999);
    const items = await GCal.listEvents(state.settings.calendarId || "primary", timeMin.toISOString(), timeMax.toISOString());
    state.events = items.filter((ev) => ev.status !== "cancelled" && matchesFilter(ev, state.settings));
  } catch (e) {
    state.loadError = gcalErrorMessage(e);
    if (gcalErrorIsAuthExpired(e)) {
      GCal.clearToken();
      state.connected = false;
    }
  } finally {
    state.loadingEvents = false;
    render();
  }
}

async function doConnect() {
  if (!state.settings.clientId) {
    showToast("先に設定でクライアントIDを入力してください");
    setView("settings");
    return;
  }
  state.connecting = true;
  render();
  try {
    await GCal.connect(state.settings.clientId);
    state.connected = true;
    try {
      state.calendars = await GCal.listCalendars();
    } catch (e) {
      state.calendars = [];
    }
    await refreshEvents();
    showToast("Googleカレンダーに接続しました");
  } catch (e) {
    showToast(e.message || "接続に失敗しました");
  } finally {
    state.connecting = false;
    render();
  }
}

function doDisconnect() {
  GCal.disconnect();
  state.connected = false;
  state.events = [];
  state.calendars = [];
  render();
  showToast("接続を解除しました");
}

async function doAddPlan() {
  const p = state.plan;
  if (!p.title.trim()) { showToast("科目・内容を入力してください"); return; }
  if (!p.date || !p.startTime) { showToast("日付と開始時刻を入力してください"); return; }
  const duration = Number(p.durationMinutes) || 0;
  if (duration <= 0) { showToast("学習時間を正しく入力してください"); return; }

  const [y, m, d] = p.date.split("-").map(Number);
  const [hh, mm] = p.startTime.split(":").map(Number);
  const start = new Date(y, m - 1, d, hh, mm);
  const end = new Date(start.getTime() + duration * 60000);
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;

  const kw = state.settings.keyword;
  const title = kw && p.title.toLowerCase().includes(kw.toLowerCase()) ? p.title : (kw ? `[${kw}] ${p.title}` : p.title);

  const event = {
    summary: title,
    description: p.memo || undefined,
    start: { dateTime: start.toISOString(), timeZone },
    end: { dateTime: end.toISOString(), timeZone },
  };

  try {
    await GCal.insertEvent(state.settings.calendarId || "primary", event);
    showToast("学習予定をカレンダーに追加しました");
    state.plan = emptyPlan();
    await refreshEvents();
    setView("home");
  } catch (e) {
    showToast(gcalErrorMessage(e));
  }
}

function markStatus(eventId, status) {
  const ev = state.events.find((e) => e.id === eventId);
  if (!ev) return;
  const planned = eventDurationMinutes(ev) || 0;
  const current = Storage.getProgress(eventId) || {};
  let actualMinutes = current.actualMinutes ?? planned;

  if (status === "done") {
    actualMinutes = planned;
  } else if (status === "skipped") {
    actualMinutes = 0;
  } else if (status === "partial") {
    const input = prompt("実際に勉強した時間(分)を入力してください", String(Math.round(current.actualMinutes ?? planned)));
    if (input === null) return;
    const n = Number(input);
    if (!Number.isFinite(n) || n < 0) { showToast("数値を入力してください"); return; }
    actualMinutes = n;
  }

  Storage.setProgress(eventId, { status, actualMinutes, memo: current.memo || "" });
  render();
}

function editMemo(eventId) {
  const ev = state.events.find((e) => e.id === eventId);
  if (!ev) return;
  const current = effectiveStatus(ev);
  const memo = prompt("メモ(任意)", current.memo || "");
  if (memo === null) return;
  Storage.setProgress(eventId, { status: current.status, actualMinutes: current.actualMinutes, memo });
  render();
}

// ---------------- 集計 ----------------

function computeStats(events) {
  const now = new Date();
  const weekStart = startOfWeek(now);
  const todayStr = todayKey();

  let weekPlanned = 0, weekDone = 0;
  let todayPlanned = 0, todayDone = 0;
  const doneDates = new Set();

  events.forEach((ev) => {
    if (!isTimedEvent(ev)) return;
    const start = eventStart(ev);
    if (start < weekStart) return;
    const planned = eventDurationMinutes(ev) || 0;
    const eff = effectiveStatus(ev);
    const counted = eff.status === "skipped" ? 0 : (Number(eff.actualMinutes) || 0);

    weekPlanned += planned;
    weekDone += counted;

    const dk = dateKeyOf(start);
    if (dk === todayStr) {
      todayPlanned += planned;
      todayDone += counted;
    }
    if (counted > 0 && (eff.status === "done" || eff.status === "partial")) {
      doneDates.add(dk);
    }
  });

  let streak = 0;
  const cursor = new Date();
  while (doneDates.has(dateKeyOf(cursor))) {
    streak++;
    cursor.setDate(cursor.getDate() - 1);
  }

  return { weekPlanned, weekDone, todayPlanned, todayDone, streak };
}

// ---------------- イベントハンドラ ----------------

function onBodyClick(e) {
  const tab = e.target.closest(".tab-btn");
  if (tab) { setView(tab.dataset.view); return; }

  const actionEl = e.target.closest("[data-action]");
  if (!actionEl) return;
  const action = actionEl.dataset.action;

  switch (action) {
    case "connect": doConnect(); break;
    case "disconnect": {
      if (confirm("Googleカレンダーとの接続を解除します。よろしいですか？")) doDisconnect();
      break;
    }
    case "refresh-events": refreshEvents(); break;
    case "go-plan": setView("plan"); break;
    case "mark-status": markStatus(actionEl.dataset.eventId, actionEl.dataset.status); break;
    case "edit-memo": editMemo(actionEl.dataset.eventId); break;
    case "save-onboarding-clientid": {
      const input = $("#f-clientid-onboard");
      const clientId = (input?.value || "").trim();
      if (!clientId) { showToast("クライアントIDを入力してください"); return; }
      state.settings = { ...state.settings, clientId };
      Storage.saveSettings(state.settings);
      showToast("保存しました");
      setView("home");
      break;
    }
  }
}

function onBodySubmit(e) {
  if (e.target.id === "form-plan") {
    e.preventDefault();
    state.plan.title = $("#f-title", e.target).value;
    state.plan.date = $("#f-date", e.target).value;
    state.plan.startTime = $("#f-time", e.target).value;
    state.plan.durationMinutes = Number($("#f-duration", e.target).value);
    state.plan.memo = $("#f-memo", e.target).value;
    doAddPlan();
    return;
  }

  if (e.target.id === "form-settings") {
    e.preventDefault();
    const f = e.target;
    const settings = {
      clientId: $("#f-clientid", f).value.trim(),
      calendarId: $("#f-calendarid", f).value.trim() || "primary",
      keyword: $("#f-keyword", f).value.trim(),
      goalWeeklyHours: Number($("#f-goal", f).value) || 0,
    };
    state.settings = settings;
    Storage.saveSettings(settings);
    showToast("設定を保存しました");
    if (state.connected) refreshEvents();
    setView("home");
    return;
  }
}

// ---------------- 画面描画 ----------------

function renderOnboarding() {
  return `
    <div class="card">
      <h1>📚 学習進捗トラッカー</h1>
      <p class="muted">Googleカレンダーの予定を「学習計画」として使い、実際に学習したかどうかを記録・可視化するアプリです。</p>
    </div>
    <div class="card">
      <h2>はじめに: GoogleのクライアントIDを入力</h2>
      <p class="muted">このアプリからあなたのGoogleカレンダーに安全にアクセスするには、Google Cloud ConsoleでOAuthクライアントIDを発行し、ここに入力する必要があります。手順は「設定」画面の説明、またはREADMEをご覧ください。</p>
      <label>Google OAuth クライアントID</label>
      <input id="f-clientid-onboard" type="text" placeholder="xxxxxxxx.apps.googleusercontent.com" value="${escapeHtml(state.settings.clientId)}">
      <button class="btn btn-primary" style="margin-top:14px" data-action="save-onboarding-clientid">保存して始める</button>
    </div>
  `;
}

function renderConnectionCard() {
  if (!state.connected) {
    return `
      <div class="card">
        <h2>Googleカレンダーに接続</h2>
        <p class="muted">接続すると、キーワード「${escapeHtml(state.settings.keyword || "(フィルタなし)")}」を含む予定を学習計画として取得します。</p>
        <button class="btn btn-primary" data-action="connect" ${state.connecting ? "disabled" : ""}>
          ${state.connecting ? "接続中…" : "Googleでログイン"}
        </button>
      </div>
    `;
  }
  return "";
}

function renderHome() {
  if (!state.connected) {
    return renderConnectionCard();
  }

  const stats = computeStats(state.events);
  const goalMinutes = (Number(state.settings.goalWeeklyHours) || 0) * 60;
  const pct = goalMinutes > 0 ? Math.min(100, Math.round((stats.weekDone / goalMinutes) * 100)) : 0;

  const now = new Date();
  const todayEvents = state.events.filter((ev) => isTimedEvent(ev) && dateKeyOf(eventStart(ev)) === todayKey());
  const upcomingToday = todayEvents.filter((ev) => eventEnd(ev) >= now);

  return `
    <div class="card">
      <div class="row" style="align-items:center;justify-content:space-between">
        <h1 style="margin:0">📚 今週の進捗</h1>
        <button class="btn btn-outline btn-sm" data-action="refresh-events" ${state.loadingEvents ? "disabled" : ""}>${state.loadingEvents ? "更新中…" : "更新"}</button>
      </div>
      ${state.loadError ? `<p class="muted" style="color:var(--red)">${escapeHtml(state.loadError)}</p>` : ""}
      <div class="progress-wrap">
        <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
        <div class="row" style="justify-content:space-between;margin-top:6px">
          <span class="muted">${formatMinutes(stats.weekDone)} / 目標${state.settings.goalWeeklyHours}時間</span>
          <span class="muted">${pct}%</span>
        </div>
      </div>
      <div class="row wrap" style="margin-top:14px;gap:14px">
        <div class="stat"><div class="stat-value">${formatMinutes(stats.todayDone)}</div><div class="stat-label">今日の学習時間</div></div>
        <div class="stat"><div class="stat-value">🔥${stats.streak}</div><div class="stat-label">連続学習日数</div></div>
      </div>
    </div>

    <div class="card">
      <div class="row" style="align-items:center;justify-content:space-between">
        <h2 style="margin:0">今日の予定</h2>
        <button class="btn btn-secondary btn-sm" data-action="go-plan">＋ 予定を追加</button>
      </div>
      ${upcomingToday.length === 0
        ? `<p class="muted">今日これからの学習予定はありません。</p>`
        : `<div class="stack">${upcomingToday.map(renderEventRow).join("")}</div>`}
    </div>
  `;
}

function renderPlan() {
  const p = state.plan;
  return `
    <div class="card">
      <h1>学習予定を追加</h1>
      <p class="muted">ここで追加した予定はGoogleカレンダーに書き込まれます。</p>
      ${!state.connected ? `<p class="muted" style="color:var(--orange)">先にホーム画面からGoogleに接続してください。</p>` : ""}
      <form id="form-plan" class="stack">
        <div>
          <label>科目・内容</label>
          <input id="f-title" type="text" placeholder="例: 数学 二次関数の演習" value="${escapeHtml(p.title)}" required>
        </div>
        <div class="row">
          <div class="col">
            <label>日付</label>
            <input id="f-date" type="date" value="${escapeHtml(p.date)}" required>
          </div>
          <div class="col">
            <label>開始時刻</label>
            <input id="f-time" type="time" value="${escapeHtml(p.startTime)}" required>
          </div>
        </div>
        <div>
          <label>学習時間(分)</label>
          <input id="f-duration" type="number" min="5" step="5" value="${Number(p.durationMinutes) || 60}" required>
        </div>
        <div>
          <label>メモ(任意)</label>
          <input id="f-memo" type="text" placeholder="教材や目標など" value="${escapeHtml(p.memo)}">
        </div>
        <button class="btn btn-primary" type="submit" ${state.connected ? "" : "disabled"}>カレンダーに追加</button>
      </form>
    </div>
  `;
}

function renderEventRow(ev) {
  const eff = effectiveStatus(ev);
  const planned = eventDurationMinutes(ev);
  const start = eventStart(ev);
  const timeLabel = isTimedEvent(ev)
    ? start.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" })
    : "終日";
  const statusLabel = { planned: "予定", done: "完了", partial: "一部", skipped: "未実施" }[eff.status] || "予定";
  const statusClass = { planned: "status-planned", done: "status-done", partial: "status-partial", skipped: "status-skipped" }[eff.status] || "status-planned";

  return `
    <div class="event-row">
      <div class="event-row-main">
        <div class="row" style="justify-content:space-between;align-items:baseline">
          <strong>${escapeHtml(ev.summary || "(無題の予定)")}</strong>
          <span class="badge ${statusClass}">${statusLabel}</span>
        </div>
        <div class="muted" style="font-size:12px">
          ${timeLabel}${planned != null ? ` ・ 予定${formatMinutes(planned)}` : ""}${eff.status !== "planned" ? ` ・ 実績${formatMinutes(eff.actualMinutes)}` : ""}
        </div>
        ${eff.memo ? `<div class="muted" style="font-size:12px;margin-top:2px">📝 ${escapeHtml(eff.memo)}</div>` : ""}
      </div>
      <div class="event-row-actions">
        <button class="btn btn-outline btn-sm" data-action="mark-status" data-event-id="${escapeHtml(ev.id)}" data-status="done">完了</button>
        <button class="btn btn-outline btn-sm" data-action="mark-status" data-event-id="${escapeHtml(ev.id)}" data-status="partial">一部</button>
        <button class="btn btn-outline btn-sm" data-action="mark-status" data-event-id="${escapeHtml(ev.id)}" data-status="skipped">未実施</button>
        <button class="btn btn-outline btn-sm" data-action="edit-memo" data-event-id="${escapeHtml(ev.id)}">メモ</button>
      </div>
    </div>
  `;
}

function renderHistory() {
  if (!state.connected) return renderConnectionCard();

  const byDate = {};
  state.events.forEach((ev) => {
    const dk = isTimedEvent(ev) ? dateKeyOf(eventStart(ev)) : dateKeyOf(new Date(ev.start.date));
    if (!byDate[dk]) byDate[dk] = [];
    byDate[dk].push(ev);
  });
  const dates = Object.keys(byDate).sort((a, b) => (a < b ? 1 : -1));

  if (dates.length === 0) {
    return `<div class="card"><p class="muted">表示できる学習予定がありません。キーワード設定や接続中のカレンダーを確認してください。</p></div>`;
  }

  return dates.map((dk) => `
    <div class="card">
      <h2>${formatDateLabel(dk)}</h2>
      <div class="stack">${byDate[dk].map(renderEventRow).join("")}</div>
    </div>
  `).join("");
}

function renderSettings() {
  const s = state.settingsDraft || state.settings;
  const calendarOptions = state.calendars.length
    ? state.calendars.map((c) => `<option value="${escapeHtml(c.id)}" ${c.id === s.calendarId ? "selected" : ""}>${escapeHtml(c.summary)}</option>`).join("")
    : "";

  return `
    <div class="card">
      <h1>設定</h1>
      <form id="form-settings" class="stack">
        <div>
          <label>Google OAuth クライアントID</label>
          <input id="f-clientid" type="text" value="${escapeHtml(s.clientId)}" placeholder="xxxxxxxx.apps.googleusercontent.com">
        </div>
        <div>
          <label>対象カレンダー</label>
          ${calendarOptions
            ? `<select id="f-calendarid">${calendarOptions}</select>`
            : `<input id="f-calendarid" type="text" value="${escapeHtml(s.calendarId)}" placeholder="primary">`}
          <p class="muted" style="font-size:12px;margin-top:4px">接続すると、あなたが持つカレンダーの一覧から選べるようになります。</p>
        </div>
        <div>
          <label>学習予定として扱うキーワード</label>
          <input id="f-keyword" type="text" value="${escapeHtml(s.keyword)}" placeholder="勉強">
          <p class="muted" style="font-size:12px;margin-top:4px">タイトルまたは詳細にこの文字列を含む予定だけを学習計画として集計します。空欄にするとカレンダー内の全予定が対象になります。</p>
        </div>
        <div>
          <label>週間学習目標(時間)</label>
          <input id="f-goal" type="number" min="0" step="0.5" value="${Number(s.goalWeeklyHours) || 0}">
        </div>
        <button class="btn btn-primary" type="submit">保存</button>
      </form>
      ${state.connected
        ? `<button class="btn btn-danger" style="margin-top:10px" data-action="disconnect">Googleとの接続を解除</button>`
        : `<button class="btn btn-secondary" style="margin-top:10px" data-action="connect">Googleでログイン</button>`}
    </div>
    <div class="card">
      <h2>Googleクライアントの作り方</h2>
      <p class="muted">
        1. Google Cloud Consoleでプロジェクトを作成し「Google Calendar API」を有効化<br>
        2. 「OAuth同意画面」を設定(テスト中はテストユーザーに自分を追加)<br>
        3. 「認証情報」から「OAuthクライアントID」(種類: ウェブアプリケーション)を作成<br>
        4. 「承認済みのJavaScript生成元」に、このアプリを開いているURL(例: http://localhost:8080 や https://あなたのGitHub Pages URL)を追加<br>
        5. 発行されたクライアントIDをこの画面に貼り付けて保存
      </p>
    </div>
  `;
}
