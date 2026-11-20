"""ブローカー。OANDA practice(デモ)専用。

- 許可するURLは https://api-fxpractice.oanda.com のみ。本番(api-fxtrade)を含め
  それ以外は BrokerRefused で拒否する。
- APIキーは環境変数 OANDA_API_KEY、口座IDは OANDA_ACCOUNT_ID。
  キーは「出金権限なし」を前提とする(OANDAのv20 APIには出金エンドポイントが無い。
  さらにpractice口座専用キーを使うこと)。
- 実弾用ブローカーはこのコードベースに存在しない。
"""
from __future__ import annotations

import os
from typing import Protocol
from urllib.parse import urlparse

import requests

from .instruments import PAIRS
from .policy import POLICY, PolicyViolation, validate

PRACTICE_URL = "https://api-fxpractice.oanda.com"
FORBIDDEN_HOSTS = {"api-fxtrade.oanda.com", "stream-fxtrade.oanda.com"}


class BrokerRefused(RuntimeError):
    pass


class BrokerError(RuntimeError):
    pass


class BrokerAmbiguous(BrokerError):
    """注文が通ったか不明(タイムアウト等)。再送はしない。"""


class Broker(Protocol):
    def account(self) -> dict: ...          # {"nav": float, "balance": float, "currency": str, "id_hash": str}
    def positions(self) -> dict[str, int]: ...
    def prices(self) -> dict[str, float]: ...  # mid
    def market_order(self, pair: str, units: int, client_id: str,
                     stop_loss_distance: float | None = None, reduce_only: bool = False) -> dict: ...
    def find_order(self, client_id: str) -> dict | None: ...
    def close_all(self) -> list[dict]: ...


def check_order_args(pair: str, units: int, client_id: str, stop_loss_distance, reduce_only: bool,
                     unit_step: int | None = None) -> None:
    """全ブローカー共通の発注前チェック。SLなしの新規(増し)注文は拒否。"""
    step = POLICY.unit_step if unit_step is None else unit_step
    if pair not in PAIRS:
        raise BrokerRefused(f"pair not allowed: {pair}")
    if units == 0 or units % step:
        raise BrokerRefused(f"units must be non-zero multiple of {step}: {units}")
    if not client_id or len(client_id) > 120 or not client_id.startswith(("fxbot-", "smoke-")):
        raise BrokerRefused(f"client_id required (fxbot-*/smoke-*): {client_id!r}")
    if not reduce_only:
        if stop_loss_distance is None or not (stop_loss_distance > 0):
            raise BrokerRefused("stop loss is required for every opening order")


def format_distance(pair: str, d: float) -> str:
    return f"{d:.3f}" if pair.endswith("JPY") else f"{d:.5f}"


def check_practice_url(url: str) -> str:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if host in FORBIDDEN_HOSTS or "fxtrade" in host:
        raise BrokerRefused(f"live (fxtrade) endpoint refused: {url}")
    if u.scheme != "https" or host != "api-fxpractice.oanda.com" or u.port not in (None, 443) \
            or u.username or u.password or u.path not in ("", "/"):
        raise BrokerRefused(f"only {PRACTICE_URL} is allowed, got {url}")
    return PRACTICE_URL


def _hash_id(account_id: str) -> str:
    import hashlib
    return hashlib.sha256(account_id.encode()).hexdigest()[:12]


class OandaPracticeBroker:
    def __init__(self, api_key: str | None = None, account_id: str | None = None,
                 base_url: str | None = None, session: requests.Session | None = None,
                 timeout: float = 20.0):
        validate(POLICY)
        if POLICY.live_trading:
            raise PolicyViolation("live trading is disabled by policy")
        self.base = check_practice_url(base_url or os.environ.get("OANDA_BASE_URL", PRACTICE_URL))
        self.key = api_key or os.environ.get("OANDA_API_KEY", "")
        self.account_id = account_id or os.environ.get("OANDA_ACCOUNT_ID", "")
        if not self.key or not self.account_id:
            raise BrokerError("OANDA_API_KEY / OANDA_ACCOUNT_ID are required")
        self.s = session or requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {self.key}",
                               "Content-Type": "application/json",
                               "Accept-Datetime-Format": "RFC3339"})
        self.timeout = timeout

    def _req(self, method: str, path: str, allow_404: bool = False, **kw) -> dict | None:
        url = self.base + path
        check_practice_url(self.base)  # 念のため毎回
        try:
            r = self.s.request(method, url, timeout=self.timeout, **kw)
        except requests.Timeout as e:
            raise BrokerAmbiguous(f"{method} {path}: timeout: {e}") from e
        except requests.RequestException as e:
            raise BrokerError(f"{method} {path}: {e}") from e
        if allow_404 and r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise BrokerError(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
        return r.json()

    def account(self) -> dict:
        a = self._req("GET", f"/v3/accounts/{self.account_id}/summary")["account"]
        cur = a.get("currency")
        if cur != POLICY.account_currency:
            raise BrokerRefused(f"account currency must be {POLICY.account_currency}, got {cur}")
        return {"nav": float(a["NAV"]), "balance": float(a["balance"]), "currency": cur,
                "margin_available": float(a.get("marginAvailable", 0)),
                "id_hash": _hash_id(self.account_id)}

    def positions(self) -> dict[str, int]:
        out = {p: 0 for p in PAIRS}
        for pos in self._req("GET", f"/v3/accounts/{self.account_id}/openPositions")["positions"]:
            inst = pos["instrument"]
            u = int(float(pos["long"]["units"])) + int(float(pos["short"]["units"]))
            out[inst] = u  # 対象外ペアも返す(未知の建玉検知用)
        return out

    def prices(self) -> dict[str, float]:
        d = self._req("GET", f"/v3/accounts/{self.account_id}/pricing",
                      params={"instruments": ",".join(PAIRS)})
        out = {}
        for p in d["prices"]:
            bid, ask = float(p["bids"][0]["price"]), float(p["asks"][0]["price"])
            out[p["instrument"]] = (bid + ask) / 2.0
        missing = [p for p in PAIRS if p not in out]
        if missing:
            raise BrokerError(f"missing prices: {missing}")
        return out

    def find_order(self, client_id: str) -> dict | None:
        """clientExtensions ID で注文を照会(無ければ None)。"""
        d = self._req("GET", f"/v3/accounts/{self.account_id}/orders/@{client_id}", allow_404=True)
        return None if d is None else d.get("order")

    def instrument_min_size(self, pair: str) -> float:
        d = self._req("GET", f"/v3/accounts/{self.account_id}/instruments", params={"instruments": pair})
        return float(d["instruments"][0]["minimumTradeSize"])

    def market_order(self, pair: str, units: int, client_id: str,
                     stop_loss_distance: float | None = None, reduce_only: bool = False,
                     unit_step: int | None = None) -> dict:
        check_order_args(pair, units, client_id, stop_loss_distance, reduce_only, unit_step)
        # 冪等: 同じIDの注文が既にあれば再送しない
        existing = self.find_order(client_id)
        if existing is not None:
            if existing.get("state") == "FILLED":
                return {"existing": True, "order": existing}
            raise BrokerError(f"order {client_id} already exists in state {existing.get('state')}; not resending")
        order = {"type": "MARKET", "instrument": pair, "units": str(int(units)),
                 "timeInForce": "FOK", "positionFill": "REDUCE_ONLY" if reduce_only else "DEFAULT",
                 "clientExtensions": {"id": client_id, "tag": "fxbot"},
                 "tradeClientExtensions": {"id": client_id, "tag": "fxbot"}}
        if not reduce_only:
            order["stopLossOnFill"] = {"distance": format_distance(pair, stop_loss_distance),
                                       "timeInForce": "GTC"}
        try:
            r = self._req("POST", f"/v3/accounts/{self.account_id}/orders", json={"order": order})
        except BrokerAmbiguous:
            # タイムアウト: 再送せず、先にOANDA側の注文履歴を照合
            found = self.find_order(client_id)
            if found is not None and found.get("state") == "FILLED":
                return {"existing": True, "order": found}
            raise
        if "orderFillTransaction" not in r:
            raise BrokerError(f"order not filled: {str(r)[:300]}")
        return r["orderFillTransaction"]

    def close_trade(self, trade_id: str) -> dict:
        return self._req("PUT", f"/v3/accounts/{self.account_id}/trades/{trade_id}/close", json={"units": "ALL"})

    def close_all(self) -> list[dict]:
        out = []
        for pair, u in self.positions().items():
            if not u:
                continue
            side = {"longUnits": "ALL"} if u > 0 else {"shortUnits": "ALL"}
            out.append(self._req("PUT", f"/v3/accounts/{self.account_id}/positions/{pair}/close", json=side))
        return out


class PaperBroker:
    """テスト・ドライラン用のメモリ内ブローカー(円口座)。"""

    def __init__(self, nav: float = 30_000.0, prices: dict[str, float] | None = None,
                 positions: dict[str, int] | None = None, currency: str = "JPY", id_hash: str = "paper"):
        self._nav, self._currency, self._id = nav, currency, id_hash
        self._prices = dict(prices or {"USD_JPY": 150.0, "EUR_USD": 1.08, "GBP_USD": 1.27, "AUD_USD": 0.66})
        self._pos = {p: 0 for p in PAIRS}
        self._pos.update(positions or {})
        self.orders: list[tuple[str, int]] = []
        self.order_meta: list[dict] = []
        self.by_client_id: dict[str, dict] = {}
        self.closed_all = 0
        self.fail_on_order: int | None = None  # n番目の注文で例外(テスト用)

    def account(self):
        if self._currency != POLICY.account_currency:
            raise BrokerRefused(f"account currency must be JPY, got {self._currency}")
        return {"nav": self._nav, "balance": self._nav, "currency": self._currency,
                "margin_available": self._nav, "id_hash": self._id}

    def positions(self):
        return dict(self._pos)

    def prices(self):
        return dict(self._prices)

    def find_order(self, client_id):
        return self.by_client_id.get(client_id)

    def market_order(self, pair, units, client_id, stop_loss_distance=None, reduce_only=False):
        check_order_args(pair, units, client_id, stop_loss_distance, reduce_only)
        if client_id in self.by_client_id:
            return {"existing": True, "order": self.by_client_id[client_id]}
        if self.fail_on_order is not None and len(self.orders) == self.fail_on_order:
            raise BrokerError("simulated failure")
        self.orders.append((pair, units))
        self.order_meta.append({"pair": pair, "units": units, "client_id": client_id,
                                "sl": stop_loss_distance, "reduce_only": reduce_only})
        self.by_client_id[client_id] = {"state": "FILLED", "pair": pair, "units": units}
        self._pos[pair] = self._pos.get(pair, 0) + units
        return {"pair": pair, "units": units}

    def close_all(self):
        self.closed_all += 1
        closed = [{"pair": p, "units": u} for p, u in self._pos.items() if u]
        self._pos = {p: 0 for p in self._pos}
        return closed
