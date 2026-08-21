// ---------------------------------------------------------------
// app.js
// 画面の描画・状態管理・イベント処理
// ---------------------------------------------------------------

const state = {
  profile: Storage.getProfile(),
  view: null,
  capture: { file: null, dataUrl: null, analyzing: false, result: null, error: null, note: "" },
  showDetail: false,
  historyOpenDate: null,
  editingProfile: false,
};

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
  showToast._t = setTimeout(() => el.classList.remove("show"), 2200);
}

// ---------------- 初期化・ルーティング ----------------

const TAB_ICONS = { home: "home", capture: "camera", history: "calendar", settings: "settings" };

function init() {
  state.view = state.profile ? "home" : "onboarding";

  // タブバーのアイコンを絵文字からSVGに差し替え
  $$(".tab-btn").forEach((btn) => {
    const wrap = $(".ic-wrap", btn);
    if (wrap) wrap.innerHTML = iconHtml(TAB_ICONS[btn.dataset.view], 17);
  });

  render();

  // イベント委譲（1回だけ登録）
  document.body.addEventListener("click", onBodyClick);
  document.body.addEventListener("change", onBodyChange);
  document.body.addEventListener("submit", onBodySubmit);
  document.addEventListener("paste", onPaste);

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("./sw.js").catch(() => {});
  }
}

function setView(view) {
  state.view = view;
  if (view === "capture") {
    state.capture = { file: null, dataUrl: null, analyzing: false, result: null, error: null, note: "" };
  }
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
    case "capture": app.innerHTML = renderCapture(); break;
    case "history": app.innerHTML = renderHistory(); break;
    case "settings": app.innerHTML = renderSettings(); break;
    default: app.innerHTML = renderHome();
  }
}

// ---------------- イベントハンドラ ----------------

function onBodyClick(e) {
  const tab = e.target.closest(".tab-btn");
  if (tab) { setView(tab.dataset.view); return; }

  const actionEl = e.target.closest("[data-action]");
  if (!actionEl) return;
  const action = actionEl.dataset.action;

  switch (action) {
    case "open-camera": $("#file-camera").click(); break;
    case "open-library": $("#file-library").click(); break;
    case "retake": state.capture = { file: null, dataUrl: null, analyzing: false, result: null, error: null, note: "" }; render(); break;
    case "analyze": doAnalyze(); break;
    case "add-log": doAddLog(); break;
    case "delete-log": doDeleteLog(actionEl.dataset.date, actionEl.dataset.id); break;
    case "toggle-detail": state.showDetail = !state.showDetail; render(); break;
    case "toggle-history-day": {
      const d = actionEl.dataset.date;
      state.historyOpenDate = state.historyOpenDate === d ? null : d;
      render();
      break;
    }
    case "edit-profile": state.editingProfile = true; render(); break;
    case "clear-logs": {
      if (confirm("すべての食事記録を削除します。よろしいですか？（元に戻せません）")) {
        Storage.clearAllLogs();
        showToast("記録を削除しました");
        render();
      }
      break;
    }
    case "toggle-key-visibility": {
      const input = $("#f-apikey");
      input.type = input.type === "password" ? "text" : "password";
      actionEl.textContent = input.type === "password" ? "表示" : "隠す";
      break;
    }
  }
}

function onBodyChange(e) {
  if (e.target.id === "file-camera" || e.target.id === "file-library") {
    handleFileSelected(e.target.files[0]);
  }
}

// 外部(スマホのカメラ・スクリーンショットなど)で撮った写真をクリップボードから貼り付け
function onPaste(e) {
  if (state.view !== "capture") return;
  const items = e.clipboardData?.items;
  if (!items) return;
  for (const item of items) {
    if (item.type && item.type.startsWith("image/")) {
      const file = item.getAsFile();
      if (file) {
        e.preventDefault();
        handleFileSelected(file);
      }
      return;
    }
  }
}

function onBodySubmit(e) {
  if (e.target.id === "onboarding-form") {
    e.preventDefault();
    submitProfileForm(e.target, true);
  } else if (e.target.id === "settings-form") {
    e.preventDefault();
    submitProfileForm(e.target, false);
  }
}

// ---------------- プロフィール ----------------

function readProfileForm(form) {
  const fd = new FormData(form);
  return {
    age: parseInt(fd.get("age"), 10),
    sex: fd.get("sex"),
    height: parseFloat(fd.get("height")),
    weight: parseFloat(fd.get("weight")),
    activity: fd.get("activity"),
    goal: fd.get("goal") || "maintain",
    apiKey: (fd.get("apiKey") || "").trim(),
    model: fd.get("model") || "claude-sonnet-5",
  };
}

function validateProfile(p) {
  if (!Number.isFinite(p.age) || p.age < 18 || p.age > 120) {
    return "本アプリは18歳以上の成人を対象としています。年齢を正しく入力してください。";
  }
  if (!p.sex) return "性別を選択してください。";
  if (!Number.isFinite(p.height) || p.height < 100 || p.height > 230) return "身長を正しく入力してください(100〜230cm)。";
  if (!Number.isFinite(p.weight) || p.weight < 25 || p.weight > 300) return "体重を正しく入力してください(25〜300kg)。";
  if (!p.activity) return "活動レベルを選択してください。";
  return null;
}

function submitProfileForm(form, isOnboarding) {
  const p = readProfileForm(form);
  const err = validateProfile(p);
  const errBox = $("#form-error", form);
  if (err) {
    if (errBox) { errBox.textContent = err; errBox.classList.remove("hidden"); }
    return;
  }
  state.profile = p;
  state.editingProfile = false;
  Storage.saveProfile(p);
  showToast(isOnboarding ? "設定を保存しました" : "プロフィールを更新しました");
  setView("home");
}

// ---------------- 写真解析 ----------------

async function handleFileSelected(file) {
  if (!file) return;
  try {
    const dataUrl = await resizeImageToBase64(file, 1024, 0.82);
    state.capture.file = file;
    state.capture.dataUrl = dataUrl;
    state.capture.result = null;
    state.capture.error = null;
    render();
  } catch (e) {
    showToast("画像の読み込みに失敗しました");
  }
}

async function doAnalyze() {
  if (!state.capture.dataUrl) return;
  if (!state.profile?.apiKey) {
    state.capture.error = "APIキーが未設定です。「設定」タブでAnthropic APIキーを登録してください。";
    render();
    return;
  }
  state.capture.analyzing = true;
  state.capture.error = null;
  render();
  try {
    const result = await analyzeFoodPhoto({
      dataUrl: state.capture.dataUrl,
      apiKey: state.profile.apiKey,
      model: state.profile.model,
    });
    state.capture.result = result;
  } catch (e) {
    state.capture.error = e.message || "解析中にエラーが発生しました";
  } finally {
    state.capture.analyzing = false;
    render();
  }
}

function doAddLog() {
  const form = $("#result-form");
  const fd = new FormData(form);
  const nutrients = {};
  Object.keys(NUTRIENT_META).forEach((k) => {
    nutrients[k] = Math.round((parseFloat(fd.get(k)) || 0) * 10) / 10;
  });
  const name = (fd.get("mealName") || "食事").trim() || "食事";
  const note = (fd.get("memo") || "").trim();

  const now = new Date();
  const entry = {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    time: `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`,
    name,
    items: state.capture.result?.items || [],
    nutrients,
    note,
    thumb: state.capture.dataUrl,
  };
  Storage.addLog(todayKey(), entry);
  showToast("記録に追加しました");
  setView("home");
}

function doDeleteLog(dateKey, id) {
  Storage.deleteLog(dateKey, id);
  showToast("記録を削除しました");
  render();
}

// ---------------- 描画: オンボーディング / 設定フォーム ----------------

function profileFormFields(p, prefix) {
  p = p || { age: "", sex: "", height: "", weight: "", activity: "normal", goal: "maintain", apiKey: "", model: "claude-sonnet-5" };
  const activityOptions = Object.entries(ACTIVITY_LEVELS)
    .map(([k, v]) => `<option value="${k}" ${p.activity === k ? "selected" : ""}>${v.label} — ${v.desc}</option>`)
    .join("");
  const goalOptions = Object.entries(GOALS)
    .map(([k, v]) => `<option value="${k}" ${(p.goal || "maintain") === k ? "selected" : ""}>${v.label} — ${v.desc}</option>`)
    .join("");
  const modelOptions = MODEL_OPTIONS
    .map((m) => `<option value="${m.value}" ${p.model === m.value ? "selected" : ""}>${m.label}</option>`)
    .join("");

  return `
    <div id="form-error" class="notice hidden"></div>
    <div class="row">
      <div class="col">
        <label for="${prefix}-age">年齢</label>
        <input type="number" id="${prefix}-age" name="age" min="18" max="120" placeholder="例: 30" value="${p.age ?? ""}" required>
      </div>
      <div class="col">
        <label for="${prefix}-sex">性別(計算に使用)</label>
        <select id="${prefix}-sex" name="sex" required>
          <option value="">選択</option>
          <option value="male" ${p.sex === "male" ? "selected" : ""}>男性</option>
          <option value="female" ${p.sex === "female" ? "selected" : ""}>女性</option>
        </select>
      </div>
    </div>
    <div class="row">
      <div class="col">
        <label for="${prefix}-height">身長(cm)</label>
        <input type="number" id="${prefix}-height" name="height" min="100" max="230" step="0.1" placeholder="例: 165" value="${p.height ?? ""}" required>
      </div>
      <div class="col">
        <label for="${prefix}-weight">体重(kg)</label>
        <input type="number" id="${prefix}-weight" name="weight" min="25" max="300" step="0.1" placeholder="例: 58" value="${p.weight ?? ""}" required>
      </div>
    </div>
    <label for="${prefix}-activity">日常の活動レベル</label>
    <select id="${prefix}-activity" name="activity" required>${activityOptions}</select>

    <label for="${prefix}-goal">目標</label>
    <select id="${prefix}-goal" name="goal">${goalOptions}</select>
    <p class="muted">増量・減量を選ぶと、カロリーとたんぱく質の目標量が調整されます。トレーニング内容や体質によって適切な量は変わるため、あくまで一般的な目安としてご利用ください。</p>

    <label for="f-apikey">Anthropic APIキー <span class="muted">(写真解析に使用・任意)</span></label>
    <div class="row">
      <div class="col"><input type="password" id="f-apikey" name="apiKey" placeholder="sk-ant-..." value="${escapeHtml(p.apiKey || "")}" autocomplete="off"></div>
      <button type="button" class="btn btn-outline btn-sm" data-action="toggle-key-visibility" style="width:auto;">表示</button>
    </div>
    <p class="muted">console.anthropic.com で取得できます。この端末のブラウザ内にのみ保存されます。後から「設定」タブで追加・変更できます。</p>

    <label for="${prefix}-model">解析に使うモデル</label>
    <select id="${prefix}-model" name="model">${modelOptions}</select>
  `;
}

function renderOnboarding() {
  return `
    <div class="card" style="text-align:center; padding:28px 16px;">
      <div style="width:56px;height:56px;border-radius:18px;background:var(--green-light);display:flex;align-items:center;justify-content:center;margin:0 auto 14px;color:var(--green-deep);">${iconHtml("meal", 26)}</div>
      <h1>ようこそ</h1>
      <p class="muted">まずはあなたに合った1日の必要栄養量を計算するために、いくつか教えてください。</p>
    </div>
    <form id="onboarding-form" class="card" novalidate>
      ${profileFormFields(null, "ob")}
      <button type="submit" class="btn btn-primary" style="margin-top:16px;">はじめる</button>
    </form>
    <p class="footer-note">本アプリの目安摂取量やAIによる栄養推定は一般的な参考値であり、医学的なアドバイスではありません。妊娠・授乳中の方や持病のある方は医師・管理栄養士にご相談ください。</p>
  `;
}

function renderSettings() {
  const p = state.profile;
  if (state.editingProfile) {
    return `
      <div class="top-header"><h1>プロフィール編集</h1></div>
      <form id="settings-form" class="card" novalidate>
        ${profileFormFields(p, "st")}
        <button type="submit" class="btn btn-primary" style="margin-top:16px;">保存する</button>
      </form>
    `;
  }

  const t = calcTargets(p);
  return `
    <div class="top-header"><h1>設定</h1></div>
    <div class="card">
      <h2>プロフィール</h2>
      <p class="muted">${p.age}歳・${p.sex === "male" ? "男性" : "女性"}・身長${p.height}cm・体重${p.weight}kg・活動レベル「${ACTIVITY_LEVELS[p.activity].label}」・目標「${(GOALS[p.goal] || GOALS.maintain).label}」</p>
      <p class="muted">1日の目標エネルギー: <strong>${t.calories} kcal</strong>(たんぱく質 ${t.protein}g)</p>
      <button class="btn btn-secondary" data-action="edit-profile">編集する</button>
    </div>
    <div class="card">
      <h2>APIキー / モデル</h2>
      <p class="muted">${p.apiKey ? "APIキーは設定済みです。" : "APIキーが未設定です。写真解析にはAPIキーが必要です。"}</p>
      <p class="muted">使用モデル: ${MODEL_OPTIONS.find((m) => m.value === p.model)?.label || p.model}</p>
      <button class="btn btn-secondary" data-action="edit-profile">変更する</button>
    </div>
    <div class="card">
      <h2>データ管理</h2>
      <p class="muted">記録はすべてこの端末のブラウザ内(localStorage)に保存されています。外部には送信されません。</p>
      <button class="btn btn-danger" data-action="clear-logs">すべての食事記録を削除</button>
    </div>
    <p class="footer-note">栄養の目安値・AIによる推定値は一般的な参考情報です。医療・栄養に関する専門的な判断が必要な場合は、医師や管理栄養士にご相談ください。</p>
  `;
}

// ---------------- 描画: ホーム ----------------

function nutrientBar(key, consumed, target) {
  const meta = NUTRIENT_META[key];
  const pct = target > 0 ? Math.min(100, Math.round((consumed / target) * 100)) : 0;
  const over = consumed > target;
  const remain = Math.max(0, Math.round((target - consumed) * 10) / 10);
  const remainLabel = meta.isLimit
    ? (over ? `${Math.round((consumed - target) * 10) / 10}${meta.unit} 超過` : `あと${remain}${meta.unit}`)
    : (over ? "達成" : `あと${remain}${meta.unit}`);
  return `
    <div class="nutrient-row">
      <div class="nutrient-head">
        <span class="label">${nutrientIconHtml(key)} ${meta.label}</span>
        <span class="val">${Math.round(consumed * 10) / 10}/${target}${meta.unit} ・ ${remainLabel}</span>
      </div>
      <div class="bar ${over ? "over" : ""}"><div style="width:${pct}%"></div></div>
    </div>
  `;
}

function renderHome() {
  const p = state.profile;
  const targets = calcTargets(p);
  const logs = Storage.getLogsForDate(todayKey());
  const consumed = sumNutrients(logs);
  const kcalPct = Math.round((consumed.calories / targets.calories) * 100);
  const kcalOver = consumed.calories > targets.calories;

  const advice = buildAdvice(consumed, targets);

  const mealsHtml = logs.length
    ? logs.map((e) => `
        <div class="meal-item">
          ${e.thumb ? `<img class="meal-thumb" src="${e.thumb}" alt="">` : `<div class="meal-thumb">${iconHtml("meal", 18)}</div>`}
          <div class="meal-info">
            <div class="name">${escapeHtml(e.name)}</div>
            <div class="meta">${e.time} ・ ${Math.round(e.nutrients.calories)} kcal</div>
          </div>
          <button class="del" data-action="delete-log" data-date="${todayKey()}" data-id="${e.id}">${iconHtml("close")}</button>
        </div>
      `).join("")
    : `<p class="muted">まだ今日の記録はありません。「記録」タブから食事の写真を撮ってみましょう。</p>`;

  const detailHtml = state.showDetail
    ? SECONDARY_NUTRIENTS.map((k) => nutrientBar(k, consumed[k], targets[k])).join("")
    : "";

  return `
    <div class="top-header">
      <h1>今日の栄養</h1>
      <span class="date">${formatDateLabel(todayKey())}</span>
    </div>

    <div class="card">
      <div class="calorie-hero">
        <div class="num ${kcalOver ? "over" : ""}">${Math.round(consumed.calories)}</div>
        <div class="label">/ ${targets.calories} kcal (${kcalPct}%)</div>
      </div>
      <div class="bar ${kcalOver ? "over" : ""}" style="margin-top:8px;"><div style="width:${Math.min(100, kcalPct)}%"></div></div>
    </div>

    <div class="card">
      <h2>主要な栄養素</h2>
      ${PRIMARY_NUTRIENTS.filter((k) => k !== "calories").map((k) => nutrientBar(k, consumed[k], targets[k])).join("")}
      <button class="link-btn" data-action="toggle-detail">${state.showDetail ? "▲ その他の栄養素を隠す" : "▼ その他の栄養素も見る"}</button>
      ${detailHtml}
    </div>

    <div class="card">
      <h2>アドバイス</h2>
      ${advice.map((a) => `<div class="advice-item ${a.warn ? "warn" : ""}"><span class="ic-chip">${nutrientIconHtml(a.iconKey)}</span><span>${a.text}</span></div>`).join("")}
    </div>

    <div class="card">
      <h2>今日の記録</h2>
      ${mealsHtml}
    </div>
  `;
}

// ---------------- 描画: 撮影・解析 ----------------

function renderCapture() {
  const c = state.capture;

  let previewInner = `<div class="placeholder">${iconHtml("camera", 40)}写真を撮る、選ぶ、または貼り付け(⌘V)</div>`;
  if (c.dataUrl) previewInner = `<img src="${c.dataUrl}" alt="撮影した写真">`;

  let actionArea = "";
  if (!c.dataUrl) {
    actionArea = `
      <div class="stack">
        <button class="btn btn-primary" data-action="open-camera">${iconHtml("camera", 17)} カメラで撮影</button>
        <button class="btn btn-outline" data-action="open-library">${iconHtml("gallery", 17)} ライブラリから選ぶ</button>
      </div>
      <p class="muted" style="text-align:center; margin-top:12px;">スマホやカメラで撮った写真は、コピーしてこの画面で<strong>⌘V(Ctrl+V)</strong>で貼り付けもできます。</p>
      <input type="file" id="file-camera" accept="image/*" capture="environment">
      <input type="file" id="file-library" accept="image/*">
    `;
  } else if (c.analyzing) {
    actionArea = `
      <div class="row" style="align-items:center; justify-content:center; padding:14px 0;">
        <div class="spinner"></div>
        <span style="margin-left:10px;">AIが解析中です…</span>
      </div>
    `;
  } else if (c.result) {
    const r = c.result;
    const itemsHtml = r.items.length
      ? `<p class="muted">${r.items.map((it) => `${escapeHtml(it.name)}(${escapeHtml(it.amount || "")})`).join("・")}</p>`
      : "";
    const sourceBadge = r.source === "label"
      ? `<span class="badge">${iconHtml("label", 13)} 成分表示を読み取りました</span>`
      : (r.confidence === "low"
          ? `<span class="badge warn">推定の確度: 低め</span>`
          : `<span class="badge">推定の確度: ${r.confidence === "high" ? "高い" : "普通"}</span>`);

    actionArea = `
      <form id="result-form" class="stack">
        <div class="row" style="align-items:center; justify-content:space-between;">
          ${sourceBadge}
          <button type="button" class="link-btn" data-action="retake">撮り直す</button>
        </div>
        ${r.source === "label" ? `<p class="muted">写真の中の成分表示(パッケージ・アプリ画面など)の数値を使いました。表示のない栄養素はおおよその推定値です。</p>` : ""}
        ${itemsHtml}
        ${r.note ? `<p class="muted">${escapeHtml(r.note)}</p>` : ""}

        <label for="mealName">食事の名前</label>
        <input type="text" id="mealName" name="mealName" value="${escapeHtml(r.items[0]?.name || "食事")}">

        <div class="section-title">栄養素(必要に応じて修正できます)</div>
        ${Object.keys(NUTRIENT_META).map((k) => `
          <div class="result-item">
            <label class="label">${nutrientIconHtml(k)} ${NUTRIENT_META[k].label}</label>
            <input type="number" step="0.1" name="${k}" value="${r.nutrients[k]}"> <span class="muted">${NUTRIENT_META[k].unit}</span>
          </div>
        `).join("")}

        <label for="memo">メモ(任意)</label>
        <input type="text" id="memo" name="memo" placeholder="例: 外食・自炊など">

        <button type="button" class="btn btn-primary" data-action="add-log" style="margin-top:6px;">記録に追加</button>
      </form>
    `;
  } else {
    actionArea = `
      ${c.error ? `<div class="notice">${escapeHtml(c.error)}</div>` : ""}
      <div class="stack">
        <button class="btn btn-primary" data-action="analyze">この写真を解析する</button>
        <button class="btn btn-outline" data-action="retake">撮り直す</button>
      </div>
    `;
  }

  return `
    <div class="top-header"><h1>食事を記録</h1></div>
    <div class="photo-preview" tabindex="0">${previewInner}</div>
    ${actionArea}
  `;
}

// ---------------- 描画: 履歴 ----------------

function renderHistory() {
  const all = Storage.getAllLogs();
  const dates = Object.keys(all).sort((a, b) => (a < b ? 1 : -1));

  if (dates.length === 0) {
    return `
      <div class="top-header"><h1>履歴</h1></div>
      <div class="card"><p class="muted">まだ記録がありません。</p></div>
    `;
  }

  const targets = calcTargets(state.profile);

  const daysHtml = dates.map((d) => {
    const entries = all[d];
    const sum = sumNutrients(entries);
    const open = state.historyOpenDate === d;
    const mealsHtml = entries.map((e) => `
      <div class="meal-item">
        ${e.thumb ? `<img class="meal-thumb" src="${e.thumb}" alt="">` : `<div class="meal-thumb"></div>`}
        <div class="meal-info">
          <div class="name">${escapeHtml(e.name)}</div>
          <div class="meta">${e.time} ・ ${Math.round(e.nutrients.calories)} kcal</div>
        </div>
        <button class="del" data-action="delete-log" data-date="${d}" data-id="${e.id}">${iconHtml("close")}</button>
      </div>
    `).join("");

    return `
      <div class="history-day" data-action="toggle-history-day" data-date="${d}">
        <span class="d">${formatDateLabel(d)}</span>
        <span class="kcal">${Math.round(sum.calories)} / ${targets.calories} kcal ${open ? "▲" : "▼"}</span>
      </div>
      ${open ? `<div>${mealsHtml}</div>` : ""}
    `;
  }).join("");

  return `
    <div class="top-header"><h1>履歴</h1></div>
    <div class="card">${daysHtml}</div>
  `;
}

document.addEventListener("DOMContentLoaded", init);
