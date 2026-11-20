import pytest

from fxbot.broker import BrokerError, BrokerRefused, OandaPracticeBroker, check_practice_url


@pytest.mark.parametrize("url", [
    "https://api-fxtrade.oanda.com",
    "https://api-fxtrade.oanda.com/",
    "https://stream-fxtrade.oanda.com",
    "http://api-fxpractice.oanda.com",
    "https://api-fxpractice.oanda.com.evil.example",
    "https://api-fxpractice.oanda.com@api-fxtrade.oanda.com",
    "https://user:pw@api-fxpractice.oanda.com",
    "https://api-fxpractice.oanda.com:8443",
    "https://api-fxpractice.oanda.com/../v3",
    "https://API-FXTRADE.OANDA.COM",
    "",
])
def test_non_practice_urls_refused(url):
    with pytest.raises(BrokerRefused):
        check_practice_url(url)


def test_practice_url_ok():
    assert check_practice_url("https://api-fxpractice.oanda.com") == "https://api-fxpractice.oanda.com"
    assert check_practice_url("https://api-fxpractice.oanda.com/") == "https://api-fxpractice.oanda.com"


def test_env_live_url_refused(monkeypatch):
    monkeypatch.setenv("OANDA_BASE_URL", "https://api-fxtrade.oanda.com")
    with pytest.raises(BrokerRefused):
        OandaPracticeBroker(api_key="k", account_id="a")


def test_missing_credentials(monkeypatch):
    monkeypatch.delenv("OANDA_API_KEY", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("OANDA_BASE_URL", raising=False)
    with pytest.raises(BrokerError):
        OandaPracticeBroker()


class R:
    def __init__(self, js, status=200):
        self._js, self.status_code, self.text = js, status, str(js)

    def json(self):
        return self._js


class FakeSession:
    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        assert url.startswith("https://api-fxpractice.oanda.com/")
        for (m, frag), resp in self.routes.items():
            if m == method and frag in url:
                return resp
        return R({"errorMessage": "nf"}, 404)


def mk(routes):
    s = FakeSession(routes)
    return OandaPracticeBroker(api_key="k", account_id="101-001-1-001", session=s), s


def test_account_currency_must_be_jpy():
    b, _ = mk({("GET", "/summary"): R({"account": {"currency": "USD", "NAV": "1", "balance": "1"}})})
    with pytest.raises(BrokerRefused):
        b.account()


def test_account_parsed_and_id_hashed():
    b, _ = mk({("GET", "/summary"): R({"account": {"currency": "JPY", "NAV": "3000000.5", "balance": "3000000"}})})
    a = b.account()
    assert a["nav"] == 3000000.5 and "101-001" not in a["id_hash"]


def test_positions_net_units():
    b, _ = mk({("GET", "/openPositions"): R({"positions": [
        {"instrument": "USD_JPY", "long": {"units": "2000"}, "short": {"units": "0"}},
        {"instrument": "EUR_USD", "long": {"units": "0"}, "short": {"units": "-1000"}},
        {"instrument": "XAU_USD", "long": {"units": "1"}, "short": {"units": "0"}}]})})
    pos = b.positions()
    assert pos["USD_JPY"] == 2000 and pos["EUR_USD"] == -1000 and pos["XAU_USD"] == 1


CID = "fxbot-20261118-USD_JPY-o1000"


@pytest.mark.parametrize("units", [0, 500, 1500, -999])
def test_order_units_must_be_multiple_of_1000(units):
    b, s = mk({})
    with pytest.raises(BrokerRefused):
        b.market_order("USD_JPY", units, CID, stop_loss_distance=1.0)
    assert not s.calls


def test_order_unknown_pair_refused():
    b, s = mk({})
    with pytest.raises(BrokerRefused):
        b.market_order("XAU_USD", 1000, CID, stop_loss_distance=1.0)
    assert not s.calls


@pytest.mark.parametrize("sl", [None, 0, -1.0, float("nan")])
def test_opening_order_without_sl_refused(sl):
    b, s = mk({})
    with pytest.raises(BrokerRefused):
        b.market_order("USD_JPY", 1000, CID, stop_loss_distance=sl)
    assert not s.calls


@pytest.mark.parametrize("cid", ["", "x", "manual-1", "fxbot-" + "a" * 200])
def test_client_id_required(cid):
    b, s = mk({})
    with pytest.raises(BrokerRefused):
        b.market_order("USD_JPY", 1000, cid, stop_loss_distance=1.0)
    assert not s.calls


def test_order_not_filled_raises():
    b, _ = mk({("POST", "/orders"): R({"orderCancelTransaction": {"reason": "INSUFFICIENT_MARGIN"}})})
    with pytest.raises(BrokerError):
        b.market_order("USD_JPY", 1000, CID, stop_loss_distance=1.0)


def test_order_body_has_sl_and_client_id():
    b, s = mk({("POST", "/orders"): R({"orderFillTransaction": {"id": "1"}})})
    b.market_order("USD_JPY", -2000, CID, stop_loss_distance=1.23456)
    body = s.calls[-1][2]["json"]["order"]
    assert body["units"] == "-2000" and body["type"] == "MARKET" and body["timeInForce"] == "FOK"
    assert body["positionFill"] == "DEFAULT"
    assert body["stopLossOnFill"] == {"distance": "1.235", "timeInForce": "GTC"}
    assert body["clientExtensions"]["id"] == CID


def test_reduce_order_no_sl_reduce_only():
    b, s = mk({("POST", "/orders"): R({"orderFillTransaction": {"id": "1"}})})
    b.market_order("EUR_USD", -1000, "fxbot-20261118-EUR_USD-r-1000", reduce_only=True)
    body = s.calls[-1][2]["json"]["order"]
    assert body["positionFill"] == "REDUCE_ONLY" and "stopLossOnFill" not in body


def test_idempotent_existing_filled_order_not_resent():
    b, s = mk({("GET", "/orders/@"): R({"order": {"state": "FILLED", "id": "9"}})})
    r = b.market_order("USD_JPY", 1000, CID, stop_loss_distance=1.0)
    assert r["existing"] is True
    assert not [c for c in s.calls if c[0] == "POST"]


def test_existing_cancelled_order_not_resent():
    b, s = mk({("GET", "/orders/@"): R({"order": {"state": "CANCELLED"}})})
    with pytest.raises(BrokerError):
        b.market_order("USD_JPY", 1000, CID, stop_loss_distance=1.0)
    assert not [c for c in s.calls if c[0] == "POST"]


class TimeoutSession(FakeSession):
    def __init__(self, filled_after_timeout):
        super().__init__({})
        self.filled = filled_after_timeout
        self.posts = 0

    def request(self, method, url, **kw):
        import requests
        self.calls.append((method, url, kw))
        if method == "POST":
            self.posts += 1
            raise requests.Timeout("read timeout")
        if "/orders/@" in url:
            return R({"order": {"state": "FILLED"}}) if (self.filled and self.posts) else R({}, 404)
        return R({}, 404)


def test_timeout_checks_history_and_does_not_resend_when_filled():
    s = TimeoutSession(True)
    b = OandaPracticeBroker(api_key="k", account_id="a", session=s)
    assert b.market_order("USD_JPY", 1000, CID, stop_loss_distance=1.0)["existing"] is True
    assert s.posts == 1


def test_timeout_not_found_raises_ambiguous_without_resend():
    from fxbot.broker import BrokerAmbiguous
    s = TimeoutSession(False)
    b = OandaPracticeBroker(api_key="k", account_id="a", session=s)
    with pytest.raises(BrokerAmbiguous):
        b.market_order("USD_JPY", 1000, CID, stop_loss_distance=1.0)
    assert s.posts == 1
