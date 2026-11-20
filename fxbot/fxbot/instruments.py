"""通貨ペアの定義と、円換算・コストの前提。

資産の混同を防ぐため、金額はすべて「円」に換算してから比較・合算する。
  - XXX_JPY : 1通貨の価値(円) = 価格。損益の通貨 = 円。
  - XXX_USD : 1通貨の価値(円) = 価格 × USD_JPY。損益の通貨 = ドル → USD_JPYで円換算。

コストの前提(OANDA Japanは取引手数料なし、コストはスプレッドのみ。
ただしバックテストは保守的に、片道ごとに「半スプレッド + スリッページ」、
さらに建玉の想定元本に年率の金利(スワップ)コストを常に「支払い側」で課す。
キャリー(受け取りスワップ)は利益として計上しない。)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Instrument:
    name: str
    base: str
    quote: str
    pip: float
    spread_pips: float  # 典型スプレッド(保守寄り)
    slippage_pips: float = 0.3


INSTRUMENTS: dict[str, Instrument] = {
    "USD_JPY": Instrument("USD_JPY", "USD", "JPY", 0.01, 0.4),
    "EUR_USD": Instrument("EUR_USD", "EUR", "USD", 0.0001, 0.5),
    "GBP_USD": Instrument("GBP_USD", "GBP", "USD", 0.0001, 1.0),
    "AUD_USD": Instrument("AUD_USD", "AUD", "USD", 0.0001, 0.6),
}
PAIRS: tuple[str, ...] = tuple(INSTRUMENTS)

# 年率の保有コスト(想定元本に対して。方向に関係なく支払いとみなす保守的前提)
FINANCING_RATE_ANNUAL = 0.015


def quote_to_jpy(pair: str, prices: dict[str, float]) -> float:
    """クォート通貨1単位の円価値。"""
    q = INSTRUMENTS[pair].quote
    if q == "JPY":
        return 1.0
    if q == "USD":
        usdjpy = prices["USD_JPY"]
        if not usdjpy or usdjpy <= 0:
            raise ValueError("USD_JPY price required for USD-quoted conversion")
        return float(usdjpy)
    raise ValueError(f"unsupported quote currency {q}")


def unit_value_jpy(pair: str, prices: dict[str, float]) -> float:
    """ベース通貨1通貨(=1 unit)の円価値。想定元本の計算に使う。"""
    p = prices[pair]
    if not p or p <= 0:
        raise ValueError(f"invalid price for {pair}: {p}")
    return float(p) * quote_to_jpy(pair, prices)


def trade_cost_jpy(pair: str, units_traded: float, prices: dict[str, float]) -> float:
    """片道の取引コスト(円)。units_traded は絶対値で扱う。"""
    ins = INSTRUMENTS[pair]
    per_unit_price = (ins.spread_pips / 2.0 + ins.slippage_pips) * ins.pip
    return abs(units_traded) * per_unit_price * quote_to_jpy(pair, prices)


def financing_cost_jpy(pair: str, units: float, prices: dict[str, float], days: float) -> float:
    """保有コスト(円)。days は暦日数。"""
    return abs(units) * unit_value_jpy(pair, prices) * FINANCING_RATE_ANNUAL * days / 365.0
