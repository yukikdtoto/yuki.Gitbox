"""Discord Webhook 通知。URL は環境変数 DISCORD_WEBHOOK_URL。"""
from __future__ import annotations

import os
import sys
from urllib.parse import urlparse

import requests

ALLOWED_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}


class NotifyConfigError(RuntimeError):
    pass


def check_webhook_url(url: str) -> str:
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in ALLOWED_HOSTS or not u.path.startswith("/api/webhooks/"):
        raise NotifyConfigError("DISCORD_WEBHOOK_URL must be https://discord.com/api/webhooks/...")
    return url


class Notifier:
    def __init__(self, url: str | None = None, session: requests.Session | None = None,
                 required: bool = False):
        url = url if url is not None else os.environ.get("DISCORD_WEBHOOK_URL", "")
        if not url and required:
            raise NotifyConfigError("DISCORD_WEBHOOK_URL is required")
        self.url = check_webhook_url(url) if url else ""
        self.s = session or requests.Session()
        self.sent: list[str] = []

    def send(self, level: str, title: str, body: str = "") -> bool:
        icon = {"error": "🔴", "kill": "🛑", "warn": "🟡", "info": "🟢"}.get(level, "")
        text = f"{icon} **[fxbot/practice] {title}**\n{body}"[:1900]
        self.sent.append(text)
        if not self.url:
            print(f"[notify:no-webhook] {text}", file=sys.stderr)
            return False
        try:
            r = self.s.post(self.url, json={"content": text}, timeout=15)
            if r.status_code >= 300:
                print(f"[notify] HTTP {r.status_code}", file=sys.stderr)
                return False
            return True
        except requests.RequestException as e:
            print(f"[notify] failed: {e}", file=sys.stderr)
            return False


class HealthPing:
    """外部 heartbeat(healthchecks.io 互換)。HEALTHCHECK_URL が無ければ何もしない。
    ping の失敗は取引判断に影響させない(通知のみ)。"""

    def __init__(self, url: str | None = None, notifier: "Notifier | None" = None,
                 session: requests.Session | None = None):
        url = url if url is not None else os.environ.get("HEALTHCHECK_URL", "")
        if url and urlparse(url).scheme != "https":
            raise NotifyConfigError("HEALTHCHECK_URL must be https")
        self.url = url.rstrip("/")
        self.notifier = notifier
        self.s = session or requests.Session()
        self.pinged: list[str] = []

    def ping(self, kind: str = "") -> bool:
        if not self.url:
            return False
        u = self.url + (f"/{kind}" if kind else "")
        self.pinged.append(kind or "success")
        try:
            r = self.s.get(u, timeout=10)
            if r.status_code < 300:
                return True
            msg = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            msg = str(e)
        print(f"[healthcheck] ping failed: {msg}", file=sys.stderr)
        if self.notifier:
            self.notifier.send("warn", "外部heartbeatのping失敗", msg[:300])
        return False
