import math
import random

import pandas as pd
import pytest

from fxbot.strategy import (StrategyConfig, efficiency_ratio, gross_leverage, round_units,
                            signal_weights, weights_to_units)
from tests.conftest import make_data

P = {"USD_JPY": 150.0, "EUR_USD": 1.10, "GBP_USD": 1.30, "AUD_USD": 0.65}


@pytest.mark.parametrize("raw,exp", [(999, 0), (1000, 1000), (1999.9, 1000), (-999, 0),
                                     (-1500, -1000), (0, 0), (2000, 2000)])
def test_round_toward_zero(raw, exp):
    assert round_units(raw, 1000) == exp


def test_round_rejects_nan():
    with pytest.raises(ValueError):
        round_units(float("nan"), 1000)


def test_small_order_disappears_but_is_recorded():
    # 3万円 × ウェイト1.0 = 3万円 → USD_JPY 200通貨 → 0。黙って消さずに記録する
    units, info = weights_to_units({"USD_JPY": 1.0, "EUR_USD": 0, "GBP_USD": 0, "AUD_USD": 0},
                                   P, 30_000, StrategyConfig())
    assert units["USD_JPY"] == 0
    assert info["rounded_to_zero"] and info["rounded_to_zero"][0]["pair"] == "USD_JPY"


def test_leverage_cap_never_exceeded_after_rounding():
    rnd = random.Random(1)
    cfg = StrategyConfig()
    for _ in range(500):
        w = {p: rnd.uniform(-40, 40) for p in P}
        eq = rnd.uniform(5_000, 60_000)
        units, info = weights_to_units(w, P, eq, cfg)
        assert gross_leverage(units, P, eq) <= 25.0 + 1e-9
        assert all(u % 1000 == 0 for u in units.values())


def test_zero_or_negative_equity_means_no_position():
    units, _ = weights_to_units({p: 5.0 for p in P}, P, 0, StrategyConfig())
    assert not any(units.values())
    units, _ = weights_to_units({p: 5.0 for p in P}, P, -100, StrategyConfig())
    assert not any(units.values())


def test_no_lookahead():
    from datetime import date
    d = make_data(date(2026, 11, 13), n=300)
    c = pd.concat({p: x["Close"] for p, x in d.items()}, axis=1)
    cfg = StrategyConfig(er_min=0.0)
    w1 = signal_weights(c, cfg)
    c2 = c.copy()
    c2.iloc[-20:] *= 1.5
    w2 = signal_weights(c2, cfg)
    pd.testing.assert_frame_equal(w1.iloc[:-20], w2.iloc[:-20])


def test_efficiency_ratio_bounds():
    s = pd.Series([1, 2, 3, 4, 5, 6], dtype=float)
    assert efficiency_ratio(s, 5).iloc[-1] == pytest.approx(1.0)
    s = pd.Series([1, 2, 1, 2, 1, 2], dtype=float)
    assert efficiency_ratio(s, 5).iloc[-1] == pytest.approx(0.2)


def test_config_bounds():
    with pytest.raises(ValueError):
        StrategyConfig(target_vol=0)
    with pytest.raises(ValueError):
        StrategyConfig(er_min=1.0)
