"""シグナル(トレンドフォロー)とポジションサイズ。

- シグナル: lookback 日のモメンタムの符号
- フィルタ: Kaufman の効率比 ER(lookback) が er_min 未満なら取引しない
- サイズ : 年率ボラ目標 target_vol をペア数で分散(1/sqrt(N))して
           ウェイト = sig * (target_vol/sqrt(N)) / 実現ボラ
- 上限  : 総レバレッジ(円換算想定元本 / 運用資産)を min(cfg上限, policy上限=25) に制限
- 丸め  : 1000通貨単位に「ゼロ方向へ」切り捨て(上限を超えないため)
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .instruments import PAIRS, unit_value_jpy
from .policy import POLICY, Policy


@dataclass(frozen=True)
class StrategyConfig:
    target_vol: float = 0.10
    er_min: float = 0.20
    lookback: int = 60
    vol_window: int = 20
    max_gross_leverage: float = 25.0

    def __post_init__(self):
        if self.target_vol <= 0 or self.target_vol > 1:
            raise ValueError("target_vol out of range")
        if not (0 <= self.er_min < 1):
            raise ValueError("er_min out of range")

    def to_dict(self):
        return asdict(self)


BASELINE = StrategyConfig()


def load_config(path: Path) -> StrategyConfig:
    if not path.exists():
        return BASELINE
    d = json.loads(path.read_text())
    keys = StrategyConfig.__dataclass_fields__.keys()
    return StrategyConfig(**{k: v for k, v in d.get("params", d).items() if k in keys})


def efficiency_ratio(close: pd.Series, n: int) -> pd.Series:
    change = (close - close.shift(n)).abs()
    path = close.diff().abs().rolling(n).sum()
    return (change / path).where(path > 0)


def signal_weights(closes: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    """各日の終値時点で決まる目標ウェイト(想定元本/資産)。未来の値は使わない。"""
    n = len(closes.columns)
    out = {}
    for p in closes.columns:
        c = closes[p]
        logr = np.log(c).diff()
        vol = logr.rolling(cfg.vol_window).std() * math.sqrt(252)
        mom = np.sign(c / c.shift(cfg.lookback) - 1.0)
        er = efficiency_ratio(c, cfg.lookback)
        sig = mom.where(er >= cfg.er_min, 0.0)
        w = sig * (cfg.target_vol / math.sqrt(n)) / vol
        out[p] = w.where(vol > 0)
    return pd.DataFrame(out).fillna(0.0)


def round_units(units: float, step: int) -> int:
    """ゼロ方向に step 単位で切り捨て。"""
    if not math.isfinite(units):
        raise ValueError(f"non-finite units {units}")
    return int(math.trunc(units / step)) * step


def gross_leverage(units: dict[str, int], prices: dict[str, float], equity_jpy: float) -> float:
    if equity_jpy <= 0:
        return math.inf
    notional = sum(abs(u) * unit_value_jpy(p, prices) for p, u in units.items() if u)
    return notional / equity_jpy


def weights_to_units(weights: dict[str, float], prices: dict[str, float], equity_jpy: float,
                     cfg: StrategyConfig, policy: Policy = POLICY) -> tuple[dict[str, int], dict]:
    """ウェイト → 1000通貨単位の発注数量。info に丸めで消えた分などを記録。"""
    cap = min(cfg.max_gross_leverage, policy.max_leverage)
    info = {"rounded_to_zero": [], "scaled": 1.0, "cap": cap}
    if equity_jpy <= 0:
        return {p: 0 for p in weights}, info
    w = {p: float(weights.get(p, 0.0)) for p in weights}
    gross = sum(abs(x) for x in w.values())
    if gross > cap:
        s = cap / gross
        w = {p: x * s for p, x in w.items()}
        info["scaled"] = s
    units: dict[str, int] = {}
    for p, x in w.items():
        raw = x * equity_jpy / unit_value_jpy(p, prices)
        u = round_units(raw, policy.unit_step)
        if u == 0 and abs(raw) > 0:
            info["rounded_to_zero"].append({"pair": p, "raw_units": round(raw, 1)})
        units[p] = u
    # 丸め後も上限を超えないことを保証(切り捨てなので通常は超えないが念のため)
    while gross_leverage(units, prices, equity_jpy) > cap + 1e-9:
        big = max(units, key=lambda k: abs(units[k]) * unit_value_jpy(k, prices))
        units[big] -= int(math.copysign(policy.unit_step, units[big]))
    return units, info


def latest_targets(closes: pd.DataFrame, prices: dict[str, float], equity_jpy: float,
                   cfg: StrategyConfig, policy: Policy = POLICY):
    w = signal_weights(closes[list(PAIRS)], cfg).iloc[-1].to_dict()
    units, info = weights_to_units(w, prices, equity_jpy, cfg, policy)
    info["weights"] = {k: round(v, 4) for k, v in w.items()}
    return units, info
