import io
import zipfile
from datetime import date

import pandas as pd
import pytest
import requests

from fxbot import data
from fxbot.data import DataError, fetch_all, fetch_daily, parse_stooq_csv, validate_daily

TODAY = date(2026, 11, 20)


class Resp:
    def __init__(self, text="", status=200, content=None):
        self.text, self.status_code = text, status
        self.content = content if content is not None else text.encode()


class Sess:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc = resp, exc

    def get(self, *a, **k):
        if self.exc:
            raise self.exc
        return self.resp


def stooq_csv(n=200, end="2026-11-19"):
    idx = pd.bdate_range(end=end, periods=n)
    rows = ["Date,Open,High,Low,Close"] + [f"{d.date()},150,151,149,150.{i % 9}" for i, d in enumerate(idx)]
    return "\n".join(rows)


def test_stooq_ok():
    df = fetch_daily("USD_JPY", date(2025, 1, 1), session=Sess(Resp(stooq_csv())), today=TODAY)
    assert len(df) == 200 and df.index[-1].date() == date(2026, 11, 19)


def test_stooq_excludes_today_partial_bar():
    df = fetch_daily("USD_JPY", date(2025, 1, 1), session=Sess(Resp(stooq_csv(end="2026-11-20"))), today=TODAY)
    assert df.index[-1].date() < TODAY


@pytest.mark.parametrize("text", [
    "", "No data", "Exceeded the daily hits limit",
    "<!DOCTYPE html><html>This site requires JavaScript to verify your browser</html>",
    "Date,Open,Close\n2026-11-19,1,1",
])
def test_stooq_bad_responses_raise(text):
    with pytest.raises(DataError):
        fetch_daily("USD_JPY", date(2025, 1, 1), session=Sess(Resp(text)), today=TODAY)


def test_http_error_and_network_error_raise():
    with pytest.raises(DataError):
        fetch_daily("USD_JPY", date(2025, 1, 1), session=Sess(Resp("x", 503)), today=TODAY)
    with pytest.raises(DataError):
        fetch_daily("USD_JPY", date(2025, 1, 1), session=Sess(exc=requests.ConnectionError("x")), today=TODAY)


def test_stale_nan_jump_short():
    df = parse_stooq_csv(stooq_csv(end="2026-11-01"), "USD_JPY")
    with pytest.raises(DataError, match="stale"):
        validate_daily(df, "USD_JPY", today=TODAY)
    df = parse_stooq_csv(stooq_csv(), "USD_JPY")
    bad = df.copy(); bad.iloc[50, 3] = float("nan")
    with pytest.raises(DataError, match="NaN"):
        validate_daily(bad, "USD_JPY", today=TODAY)
    bad = df.copy(); bad.iloc[50, :] = 300.0
    with pytest.raises(DataError):
        validate_daily(bad, "USD_JPY", today=TODAY)
    with pytest.raises(DataError, match="too few"):
        validate_daily(df.iloc[-10:], "USD_JPY", today=TODAY)


def ecb_zip(n=200, end="2026-11-20"):
    idx = pd.bdate_range(end=end, periods=n)
    lines = ["Date,USD,JPY,GBP,AUD,"] + [f"{d.date()},1.10,165.0,0.85,1.65," for d in idx[::-1]]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("eurofxref-hist.csv", "\n".join(lines))
    return buf.getvalue()


def test_ecb_cross_rates():
    d = fetch_all(date(2025, 1, 1), source="ecb", session=Sess(Resp(content=ecb_zip())), today=TODAY)
    last = {p: df["Close"].iloc[-1] for p, df in d.items()}
    assert last["USD_JPY"] == pytest.approx(150.0)
    assert last["EUR_USD"] == pytest.approx(1.10)
    assert last["GBP_USD"] == pytest.approx(1.10 / 0.85)
    assert last["AUD_USD"] == pytest.approx(1.10 / 1.65)


def test_ecb_not_zip_raises():
    with pytest.raises(DataError):
        fetch_all(date(2025, 1, 1), source="ecb", session=Sess(Resp("<html>")), today=TODAY)


def test_fetch_all_one_failure_stops_all(monkeypatch):
    def fake(pair, *a, **k):
        if pair == "GBP_USD":
            raise DataError("boom")
        return pd.DataFrame()
    monkeypatch.setattr(data, "fetch_daily", fake)
    with pytest.raises(DataError):
        fetch_all(date(2025, 1, 1), source="stooq")
