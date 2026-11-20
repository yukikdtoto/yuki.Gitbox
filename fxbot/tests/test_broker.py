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


@pytest.mark.parametrize("units", [0, 500, 1500, -999])
def test_order_units_must_be_multiple_of_1000(units):
    b, s = mk({})
    with pytest.raises(BrokerRefused):
        b.market_order("USD_JPY", units)
    assert not s.calls


def test_order_unknown_pair_refused():
    b, s = mk({})
    with pytest.raises(BrokerRefused):
        b.market_order("XAU_USD", 1000)


def test_order_not_filled_raises():
    b, _ = mk({("POST", "/orders"): R({"orderCancelTransaction": {"reason": "INSUFFICIENT_MARGIN"}})})
    with pytest.raises(BrokerError):
        b.market_order("USD_JPY", 1000)


def test_order_body():
    b, s = mk({("POST", "/orders"): R({"orderFillTransaction": {"id": "1"}})})
    b.market_order("USD_JPY", -2000)
    body = s.calls[-1][2]["json"]["order"]
    assert body == {"type": "MARKET", "instrument": "USD_JPY", "units": "-2000",
                    "timeInForce": "FOK", "positionFill": "DEFAULT"}
