"""絶対に変えないリスクポリシー。

ここにある値はコードのどこからも書き換えない(frozen dataclass)。
テスト(tests/test_policy.py)が値そのものを固定しているので、
変更するとCIが落ちる。

- live_trading=False : 実弾取引は対象外。本番URLは broker 側でも拒否する。
- max_leverage=25    : 実効レバレッジの上限(これより上には絶対にしない)。
- max_drawdown_30d=-0.25 : 直近30日の高値からの下落率がこれ以下になったら
                       全決済 + 永続停止(HALT)。
- HALT解除は人間のみ : ボットのコードはHALTを「作る」だけで、消す経路を持たない。
                       解除は `python -m fxbot.unhalt`(対話端末で確認語の入力が必須)
                       または人間が手で HALT ファイルを削除する。
"""
from __future__ import annotations

from dataclasses import dataclass


class PolicyViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class Policy:
    live_trading: bool = False
    max_leverage: float = 25.0
    max_drawdown_30d: float = -0.25
    # ボットが運用してよい元本(円)。デモ口座の残高(数百万円等)とは混同しない。
    capital_jpy: float = 30_000.0
    # 注文単位(通貨)。これ未満の端数は切り捨て。
    unit_step: int = 1000
    # 口座通貨。円以外の口座は拒否(資産の混同防止)。
    account_currency: str = "JPY"


POLICY = Policy()


def validate(p: Policy = POLICY) -> Policy:
    """起動時に毎回呼ぶ。想定外の値なら即停止。"""
    if p.live_trading is not False:
        raise PolicyViolation("live_trading must be False (practice only)")
    if not (0 < p.max_leverage <= 25.0):
        raise PolicyViolation(f"max_leverage must be in (0, 25], got {p.max_leverage}")
    if p.max_drawdown_30d != -0.25:
        raise PolicyViolation(f"max_drawdown_30d must be -0.25, got {p.max_drawdown_30d}")
    if p.capital_jpy <= 0:
        raise PolicyViolation("capital_jpy must be positive")
    if p.unit_step <= 0:
        raise PolicyViolation("unit_step must be positive")
    if p.account_currency != "JPY":
        raise PolicyViolation("account_currency must be JPY")
    return p
