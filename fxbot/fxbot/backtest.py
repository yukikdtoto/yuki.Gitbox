"""日次バックテスト。実運用と同じ sizing(30,000円・1000通貨丸め・レバ上限・
キルスイッチ)を使い、コストは円換算で差し引く。

約定: 終値 t で判断し終値 t で約定(片道コスト込み)、損益は t→t+1。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import pandas as pd

from .instruments import PAIRS, financing_cost_jpy, quote_to_jpy, trade_cost_jpy
from .policy import POLICY, Policy
from .risk import drawdown_30d, kill_switch_triggered
from .strategy import StrategyConfig, signal_weights, weights_to_units


@dataclass
class BTResult:
    equity: pd.Series
    trades: int = 0
    rounded_to_zero: int = 0
    halted_on: str | None = None
    costs_jpy: float = 0.0
    exposure_days: int = 0
    metrics: dict = field(default_factory=dict)


def metrics(equity: pd.Series) -> dict:
    eq = equity.dropna()
    if len(eq) < 2 or eq.iloc[0] <= 0:
        return {"total_return": 0.0, "cagr": 0.0, "max_dd": 0.0, "calmar": 0.0}
    total = eq.iloc[-1] / eq.iloc[0] - 1.0
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
    end = max(eq.iloc[-1], 1e-9)
    cagr = (end / eq.iloc[0]) ** (1 / years) - 1.0
    mdd = float((eq / eq.cummax() - 1.0).min())
    if mdd < 0:
        calmar = cagr / abs(mdd)
    else:
        calmar = 0.0 if cagr <= 0 else 99.0  # DD無し: 表示用の上限値
    return {"total_return": float(total), "cagr": float(cagr), "max_dd": mdd,
            "calmar": float(calmar)}


def run_backtest(closes: pd.DataFrame, cfg: StrategyConfig, start, end,
                 initial_equity: float | None = None, policy: Policy = POLICY,
                 cost_multiplier: float = 1.0) -> BTResult:
    closes = closes[list(PAIRS)].dropna()
    weights = signal_weights(closes, cfg)
    idx = closes.index[(closes.index >= pd.Timestamp(start)) & (closes.index < pd.Timestamp(end))]
    eq = float(initial_equity if initial_equity is not None else policy.capital_jpy)
    pos = {p: 0 for p in PAIRS}
    hist: list[dict] = []
    curve = {}
    res = BTResult(equity=pd.Series(dtype=float))
    stopped = False
    for t in idx:
        prices = closes.loc[t].to_dict()
        curve[t] = eq
        hist.append({"date": t.date().isoformat(), "equity": eq})
        hist = hist[-40:]
        if not stopped:
            dd, _ = drawdown_30d(hist[:-1], t.date(), eq)
            if kill_switch_triggered(dd, policy) or eq <= 0:
                stopped = True
                res.halted_on = t.date().isoformat()
                target = {p: 0 for p in PAIRS}
            else:
                target, info = weights_to_units(weights.loc[t].to_dict(), prices, eq, cfg, policy)
                res.rounded_to_zero += len(info["rounded_to_zero"])
        else:
            target = {p: 0 for p in PAIRS}
        for p in PAIRS:
            d = target[p] - pos[p]
            if d:
                c = trade_cost_jpy(p, d, prices) * cost_multiplier
                eq -= c
                res.costs_jpy += c
                res.trades += 1
        pos = target
        # 翌営業日までの損益
        loc = closes.index.get_loc(t)
        if loc + 1 >= len(closes.index):
            break
        t1 = closes.index[loc + 1]
        p1 = closes.loc[t1].to_dict()
        days = (t1 - t).days
        if any(pos.values()):
            res.exposure_days += 1
        for p, u in pos.items():
            if not u:
                continue
            eq += u * (p1[p] - prices[p]) * quote_to_jpy(p, p1)
            fc = financing_cost_jpy(p, u, prices, days) * cost_multiplier
            eq -= fc
            res.costs_jpy += fc
        if t1 >= pd.Timestamp(end):
            curve[t1] = eq
    res.equity = pd.Series(curve).sort_index()
    res.metrics = metrics(res.equity)
    return res
