// ---------------------------------------------------------------
// calendar.js
// Google Calendar API 連携
// Google Identity Services(GIS)でアクセストークンを取得し、
// gapiクライアントでCalendar APIを呼び出す。
// アクセストークンはこのタブのsessionStorageにのみ保持する
// (ページを閉じる/再読み込みすると失効し、再ログインが必要)。
// ---------------------------------------------------------------

const GCAL_DISCOVERY_DOC = "https://www.googleapis.com/discovery/v1/apis/calendar/v3/rest";
const GCAL_SCOPE = "https://www.googleapis.com/auth/calendar";
const LS_TOKEN = "studytracker_token_v1";

const GCal = {
  _gapiReady: false,
  _gisReady: false,
  _tokenClient: null,

  loadScript(src) {
    return new Promise((resolve, reject) => {
      if (document.querySelector(`script[src="${src}"]`)) {
        resolve();
        return;
      }
      const s = document.createElement("script");
      s.src = src;
      s.async = true;
      s.onload = () => resolve();
      s.onerror = () => reject(new Error(`スクリプトの読み込みに失敗しました: ${src}`));
      document.head.appendChild(s);
    });
  },

  async ensureGapi() {
    if (this._gapiReady) return;
    await this.loadScript("https://apis.google.com/js/api.js");
    await new Promise((resolve) => window.gapi.load("client", resolve));
    await window.gapi.client.init({ discoveryDocs: [GCAL_DISCOVERY_DOC] });
    this._gapiReady = true;
  },

  async ensureGis() {
    if (this._gisReady) return;
    await this.loadScript("https://accounts.google.com/gsi/client");
    this._gisReady = true;
  },

  getSavedToken() {
    const t = safeParse(sessionStorage.getItem(LS_TOKEN), null);
    if (!t || !t.access_token || !t.expires_at) return null;
    if (Date.now() > t.expires_at - 60 * 1000) return null;
    return t;
  },

  saveToken(tokenResponse) {
    const expires_at = Date.now() + (Number(tokenResponse.expires_in) || 3600) * 1000;
    sessionStorage.setItem(LS_TOKEN, JSON.stringify({ access_token: tokenResponse.access_token, expires_at }));
  },

  clearToken() {
    sessionStorage.removeItem(LS_TOKEN);
    if (window.gapi?.client) window.gapi.client.setToken(null);
  },

  isConnected() {
    return !!this.getSavedToken();
  },

  // 保存済みトークンがあればそれを使い、なければGoogleのログイン/同意画面を開く
  async connect(clientId) {
    if (!clientId) throw new Error("クライアントIDが設定されていません");
    await this.ensureGapi();

    const saved = this.getSavedToken();
    if (saved) {
      window.gapi.client.setToken({ access_token: saved.access_token });
      return true;
    }

    await this.ensureGis();
    return new Promise((resolve, reject) => {
      this._tokenClient = google.accounts.oauth2.initTokenClient({
        client_id: clientId,
        scope: GCAL_SCOPE,
        callback: (resp) => {
          if (resp.error) {
            reject(new Error(resp.error_description || resp.error));
            return;
          }
          this.saveToken(resp);
          window.gapi.client.setToken({ access_token: resp.access_token });
          resolve(true);
        },
        error_callback: (err) => {
          reject(new Error(err?.message || "Googleへのログインがキャンセルされました"));
        },
      });
      this._tokenClient.requestAccessToken({ prompt: "consent" });
    });
  },

  disconnect() {
    const saved = this.getSavedToken();
    if (saved && window.google?.accounts?.oauth2) {
      google.accounts.oauth2.revoke(saved.access_token, () => {});
    }
    this.clearToken();
  },

  async listCalendars() {
    const res = await window.gapi.client.calendar.calendarList.list({ maxResults: 250 });
    return res.result.items || [];
  },

  async listEvents(calendarId, timeMinISO, timeMaxISO) {
    const res = await window.gapi.client.calendar.events.list({
      calendarId,
      timeMin: timeMinISO,
      timeMax: timeMaxISO,
      singleEvents: true,
      orderBy: "startTime",
      maxResults: 250,
    });
    return res.result.items || [];
  },

  async insertEvent(calendarId, event) {
    const res = await window.gapi.client.calendar.events.insert({ calendarId, resource: event });
    return res.result;
  },
};

function gcalErrorMessage(e) {
  return e?.result?.error?.message || e?.message || "Googleカレンダーとの通信に失敗しました";
}

function gcalErrorIsAuthExpired(e) {
  const code = e?.result?.error?.code || e?.status;
  return code === 401;
}
