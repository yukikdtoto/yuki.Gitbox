// ---------------------------------------------------------------
// storage.js
// 設定(クライアントIDなど)と、カレンダー予定に対する進捗の
// 上書き情報をlocalStorageに保存する薄いラッパー。
// カレンダー予定そのものはGoogleカレンダー側が正なので、
// ここではイベントIDに対する「実績」だけを持つ。
// ---------------------------------------------------------------

const LS_SETTINGS = "studytracker_settings_v1";
const LS_PROGRESS = "studytracker_progress_v1";

function safeParse(json, fallback) {
  try {
    const v = JSON.parse(json);
    return v == null ? fallback : v;
  } catch (e) {
    return fallback;
  }
}

const DEFAULT_SETTINGS = {
  clientId: "",
  calendarId: "primary",
  keyword: "勉強",
  goalWeeklyHours: 10,
};

const Storage = {
  getSettings() {
    return { ...DEFAULT_SETTINGS, ...safeParse(localStorage.getItem(LS_SETTINGS), {}) };
  },
  saveSettings(settings) {
    localStorage.setItem(LS_SETTINGS, JSON.stringify(settings));
  },

  getAllProgress() {
    return safeParse(localStorage.getItem(LS_PROGRESS), {});
  },
  getProgress(eventId) {
    return Storage.getAllProgress()[eventId] || null;
  },
  setProgress(eventId, data) {
    const all = Storage.getAllProgress();
    all[eventId] = data;
    localStorage.setItem(LS_PROGRESS, JSON.stringify(all));
  },
  clearProgress(eventId) {
    const all = Storage.getAllProgress();
    delete all[eventId];
    localStorage.setItem(LS_PROGRESS, JSON.stringify(all));
  },
};

function pad2(n) {
  return String(n).padStart(2, "0");
}

function dateKeyOf(date) {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

function todayKey() {
  return dateKeyOf(new Date());
}

function formatDateLabel(dateKey) {
  const [y, m, d] = dateKey.split("-").map(Number);
  const dt = new Date(y, m - 1, d);
  const wd = ["日", "月", "火", "水", "木", "金", "土"][dt.getDay()];
  return `${m}月${d}日(${wd})`;
}

// 月曜始まりの週の開始日(00:00)を返す
function startOfWeek(date) {
  const d = new Date(date);
  const day = d.getDay();
  const diff = (day === 0 ? -6 : 1) - day;
  d.setDate(d.getDate() + diff);
  d.setHours(0, 0, 0, 0);
  return d;
}

function formatMinutes(min) {
  const m = Math.round(min || 0);
  if (m < 60) return `${m}分`;
  const h = Math.floor(m / 60);
  const rest = m % 60;
  return rest === 0 ? `${h}時間` : `${h}時間${rest}分`;
}
