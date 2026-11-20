"""smoke(practice 実API の手動スモーク)のモックテスト。実APIは呼ばない。"""
import argparse
import re
from pathlib import Path

import pytest

from fxbot import smoke
from fxbot.broker import BrokerError, BrokerRefused, OandaPracticeBroker
from tests.test_broker import FakeSession, R

PKG = Path(__file__).resolve().parent.parent / "fxbot"
ROOT = PKG.parent.parent


def args(**kw):
    return argparse.Namespace(**{"confirm_practice": True, "pair": "USD_JPY", **kw})


@pytest.mark.parametrize("kw,env,halt,tty", [
    ({"confirm_practice": False}, True, False, True),
    ({}, True, False, False),
    ({}, True, True, True),
    ({}, False, False, True),
    ({"pair": "XAU_USD"}, True, False, True),
])
def test_preflight_refusals(tmp_path, kw, env, halt, tty):
    h = tmp_path / "HALT"
    if halt:
        h.write_text("x")
    with pytest.raises(smoke.SmokeRefused):
        smoke.preflight(args(**kw), env, h, tty)


def test_preflight_ok(tmp_path):
    smoke.preflight(args(), True, tmp_path / "HALT", True)


def test_smoke_live_url_refused(monkeypatch):
    monkeypatch.setenv("OANDA_BASE_URL", "https://api-fxtrade.oanda.com")
    with pytest.raises(BrokerRefused):
        OandaPracticeBroker(api_key="k", account_id="a")


class SmokeSession(FakeSession):
    def __init__(self, close_ok=True):
        super().__init__({})
        self.pos = 0
        self.close_ok = close_ok
        self.order_state = None

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if url.endswith("/summary"):
            return R({"account": {"currency": "JPY", "NAV": "3000000", "balance": "3000000"}})
        if "/instruments" in url:
            return R({"instruments": [{"minimumTradeSize": "1"}]})
        if url.endswith("/openPositions"):
            return R({"positions": [] if not self.pos else
                      [{"instrument": "USD_JPY", "long": {"units": str(self.pos)}, "short": {"units": "0"}}]})
        if "/pricing" in url:
            return R({"prices": [{"instrument": p, "bids": [{"price": "150"}], "asks": [{"price": "150.01"}]}
                                 for p in ("USD_JPY", "EUR_USD", "GBP_USD", "AUD_USD")]})
        if "/orders/@" in url:
            return R({"order": {"state": self.order_state}}) if self.order_state else R({}, 404)
        if method == "POST" and url.endswith("/orders"):
            o = kw["json"]["order"]
            assert "stopLossOnFill" in o and o["clientExtensions"]["id"].startswith("smoke-")
            self.pos += int(o["units"])
            self.order_state = "FILLED"
            return R({"orderFillTransaction": {"tradeOpened": {"tradeID": "77"}}})
        if method == "PUT" and "/trades/77/close" in url:
            if self.close_ok:
                self.pos = 0
            return R({"orderFillTransaction": {}})
        return R({}, 404)


def test_smoke_round_trip(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    s = SmokeSession()
    b = OandaPracticeBroker(api_key="k", account_id="a", session=s)
    steps = smoke.run_smoke(b, "USD_JPY", out=lambda *a: None)
    assert steps["min_size"] == 1 and steps["flat"] == "ok"
    assert all(u.startswith("https://api-fxpractice.oanda.com/") for _, u, _ in s.calls)


def test_smoke_detects_position_not_flat(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    b = OandaPracticeBroker(api_key="k", account_id="a", session=SmokeSession(close_ok=False))
    with pytest.raises(BrokerError):
        smoke.run_smoke(b, "USD_JPY", out=lambda *a: None)


def test_smoke_not_called_from_runner_or_ci():
    for f in PKG.glob("*.py"):
        if f.name != "smoke.py":
            assert not re.search(r"(from\s+\.?\s*smoke|import\s+.*smoke|fxbot\.smoke)", f.read_text()), f.name
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        assert "fxbot.smoke" not in wf.read_text(), wf.name
