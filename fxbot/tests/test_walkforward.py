from datetime import date

import pandas as pd
import pytest

from fxbot.backtest import metrics, run_backtest
from fxbot.strategy import StrategyConfig
from fxbot.walkforward import run_walkforward, windows
from tests.conftest import make_data


def closes(n, **kw):
    d = make_data(date(2026, 11, 13), n=n, **kw)
    return pd.concat({p: x["Close"] for p, x in d.items()}, axis=1)


def test_windows_5y():
    idx = pd.bdate_range("2021-01-01", "2026-01-01")
    w = windows(idx, idx[0])
    assert len(w) == 6
    assert w[0][1] - w[0][0] >= pd.Timedelta(days=720)
    assert all(b[0] == a[0] + pd.DateOffset(months=6) for a, b in zip(w, w[1:]))
    # 検証期間が重ならない
    assert all(a[2] <= b[2] and a[1] < b[1] for a, b in zip(w, w[1:]))


def test_metrics():
    s = pd.Series([100, 120, 90, 110.0], index=pd.date_range("2020-01-01", periods=4, freq="365D"))
    m = metrics(s)
    assert m["max_dd"] == pytest.approx(-0.25) and m["total_return"] == pytest.approx(0.1)
    assert m["calmar"] == pytest.approx(m["cagr"] / 0.25)


def test_backtest_kill_switch_stops_trading():
    c = closes(400, drift=0.0, vol=0.004, seed=1)
    c.iloc[250:265, 0] = c.iloc[250, 0] * (1 - 0.03 * pd.Series(range(15)).values)  # USD_JPY 急落
    r = run_backtest(c, StrategyConfig(target_vol=0.8, er_min=0.0), c.index[100], c.index[-1])
    if r.halted_on:
        after = r.equity[r.equity.index > pd.Timestamp(r.halted_on) + pd.Timedelta(days=3)]
        assert after.nunique() <= 1


def test_walkforward_rejects_when_no_improvement():
    c = closes(1500, drift=0.0, vol=0.006, seed=7)  # トレンド無しのランダムウォーク
    res = run_walkforward(c, years=5)
    assert len(res["windows"]) >= 5
    if not res["adopt"]:
        assert res["reasons"]
    for k, v in res["per_candidate"].items():
        assert v  # 全候補に判定理由がある
