"""手数料の前提と、資産(通貨)の混同を潰すテスト。"""
import pandas as pd
import pytest

from fxbot.backtest import run_backtest
from fxbot.instruments import (FINANCING_RATE_ANNUAL, INSTRUMENTS, financing_cost_jpy, quote_to_jpy,
                               trade_cost_jpy, unit_value_jpy)
from fxbot.strategy import StrategyConfig
from tests.conftest import make_data

P = {"USD_JPY": 150.0, "EUR_USD": 1.10, "GBP_USD": 1.30, "AUD_USD": 0.65}


def test_pip_sizes():
    assert INSTRUMENTS["USD_JPY"].pip == 0.01
    for p in ("EUR_USD", "GBP_USD", "AUD_USD"):
        assert INSTRUMENTS[p].pip == 0.0001


def test_unit_value_converted_to_jpy():
    assert unit_value_jpy("USD_JPY", P) == 150.0
    assert unit_value_jpy("EUR_USD", P) == pytest.approx(1.10 * 150)   # ドル建てを円に
    assert quote_to_jpy("AUD_USD", P) == 150.0
    with pytest.raises(ValueError):
        unit_value_jpy("EUR_USD", {"EUR_USD": 1.1, "USD_JPY": 0})


def test_trade_cost_one_way_in_jpy():
    # USD_JPY 1000通貨: (0.4/2 + 0.3)pips * 0.01 * 1000 = 5円
    assert trade_cost_jpy("USD_JPY", 1000, P) == pytest.approx(5.0)
    assert trade_cost_jpy("USD_JPY", -1000, P) == pytest.approx(5.0)
    # EUR_USD 1000通貨: (0.5/2+0.3)*0.0001*1000 ドル = 0.055ドル → ×150 = 8.25円
    assert trade_cost_jpy("EUR_USD", 1000, P) == pytest.approx(8.25)


def test_financing_cost_is_always_paid():
    a = financing_cost_jpy("USD_JPY", 1000, P, 365)
    b = financing_cost_jpy("USD_JPY", -1000, P, 365)
    assert a == b == pytest.approx(150_000 * FINANCING_RATE_ANNUAL)


def _bt(mult, cfg=StrategyConfig(target_vol=0.8, er_min=0.0)):
    from datetime import date
    d = make_data(date(2026, 11, 13), n=400, drift=0.0005, vol=0.004, seed=3)
    c = pd.concat({p: x["Close"] for p, x in d.items()}, axis=1)
    return run_backtest(c, cfg, c.index[100], c.index[-1] + pd.Timedelta(days=1), cost_multiplier=mult)


def test_costs_reduce_equity_and_scale():
    r0, r1, r2 = _bt(0.0), _bt(1.0), _bt(2.0)
    assert r1.trades > 0
    assert r0.costs_jpy == 0
    assert r1.costs_jpy > 0
    assert r0.equity.iloc[-1] > r1.equity.iloc[-1] > r2.equity.iloc[-1]


def test_backtest_pnl_usd_pair_converted_with_usdjpy():
    """EUR_USD だけ +0.01 動き、USD_JPY=150 固定: 1000通貨の損益は 10ドル=1500円(コスト除く)。"""
    idx = pd.bdate_range("2026-01-01", periods=3)
    c = pd.DataFrame({"USD_JPY": [150.0] * 3, "EUR_USD": [1.10, 1.11, 1.11],
                      "GBP_USD": [1.3] * 3, "AUD_USD": [0.65] * 3}, index=idx)
    from fxbot import backtest as bt

    orig = bt.signal_weights
    bt.signal_weights = lambda closes, cfg: pd.DataFrame(
        {"USD_JPY": 0.0, "EUR_USD": 1000 * 1.10 * 150 / 30000 + 1e-9, "GBP_USD": 0.0, "AUD_USD": 0.0}, index=closes.index)
    try:
        r = bt.run_backtest(c, StrategyConfig(), idx[0], idx[1] + pd.Timedelta(days=1), cost_multiplier=0.0)
    finally:
        bt.signal_weights = orig
    pnl = r.equity.iloc[-1] - 30000
    assert pnl == pytest.approx(10 * 150, rel=1e-6)
