"""最小ロットと資本の切り分け分析(分析専用。採用判断・params・policy には使わない)。

    python -m fxbot.lot_analysis --offline

- 通貨ペアごとの「1000通貨時のレバレッジ」「1ATR逆行時の損失率(対3万円)」
- 現行の戦略・WF設定のまま仮想資本だけ 30万円(参考で300万円)にして WF を再実行し、
  成績不振が「ロット制約」由来か「戦略に優位性が無い」由来かを切り分ける。
- practice 口座の最小取引単位は API で確認する。環境変数が無い/接続できない場合は「未確認」。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import pandas as pd

from .instruments import PAIRS, quote_to_jpy, unit_value_jpy
from .policy import POLICY
from .strategy import ATR_WINDOW
from .walkforward import run_walkforward


def min_size_from_api() -> tuple[dict, str]:
    if not (os.environ.get("OANDA_API_KEY") and os.environ.get("OANDA_ACCOUNT_ID")):
        return {}, "未確認(OANDA_API_KEY / OANDA_ACCOUNT_ID 未設定のためAPI未接続)"
    try:
        from .broker import OandaPracticeBroker
        b = OandaPracticeBroker()
        return {p: b.instrument_min_size(p) for p in PAIRS}, "API で確認"
    except Exception as e:  # noqa: BLE001
        return {}, f"未確認(API接続失敗: {type(e).__name__})"


def lot_table(closes: pd.DataFrame, capital: float = POLICY.capital_jpy, lot: int = POLICY.unit_step):
    prices = closes.iloc[-1].to_dict()
    rows = []
    for p in PAIRS:
        c = closes[p]
        atr = float(c.diff().abs().rolling(ATR_WINDOW).mean().iloc[-1])
        notional = lot * unit_value_jpy(p, prices)
        loss = lot * atr * quote_to_jpy(p, prices)
        vol = float(c.pct_change().std() * (252 ** 0.5))
        rows.append({"pair": p, "price": prices[p], "notional_jpy": notional, "leverage": notional / capital,
                     "atr": atr, "atr_loss_jpy": loss, "atr_loss_pct": loss / capital,
                     "ann_vol": vol, "pos_ann_vol_pct": notional / capital * vol})
    return rows


def render(rows, wf: dict, min_sizes: dict, min_note: str, data_range: str) -> str:
    L = ["# 最小ロットと資本の切り分け分析\n",
         "**分析専用**: この結果は採用判断・params・policy に使わない。ボットの資本は3万円のまま。\n",
         f"- データ: ECB参照レート日足 {data_range}(高値・安値が無いため ATR は |終値の前日差| の{ATR_WINDOW}日平均で近似)",
         f"- practice口座の最小取引単位: {min_note}"]
    if min_sizes:
        L.append("  - " + ", ".join(f"{k}: {v:g}" for k, v in min_sizes.items()))
    L.append(f"- ボットの注文単位(policy.unit_step): {POLICY.unit_step}通貨\n")
    L.append("## 1000通貨あたりのリスク(対3万円、直近値)\n")
    L.append("| ペア | 価格 | 想定元本(円) | レバレッジ | 1ATR | 1ATR逆行の損失 | 対3万円 | 年率ボラ寄与 |\n|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        L.append(f"| {r['pair']} | {r['price']:.4f} | {r['notional_jpy']:,.0f} | {r['leverage']:.1f}倍 | "
                 f"{r['atr']:.5f} | {r['atr_loss_jpy']:,.0f}円 | {r['atr_loss_pct']:.2%} | {r['pos_ann_vol_pct']:.0%} |")
    tot = sum(r["leverage"] for r in rows)
    L.append(f"\n4ペアすべて1000通貨ずつ持つと総レバ約 {tot:.1f}倍。\n")
    L.append("## 仮想資本を変えたウォークフォワード(戦略・WF設定・コストは同一)\n")
    L.append("| 仮想資本 | WF Calmar | WF リターン | WF 最大DD | 現行 Calmar | 現行 リターン | WFの丸め0件(合計) | WFキルスイッチ | 判定 |\n|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for cap, res in wf.items():
        s = res["stitched"]
        r0 = sum(w["wf_rounded_to_zero"] for w in res["windows"])
        k = sum(1 for w in res["windows"] if w["wf_halted"])
        L.append(f"| {cap:,.0f}円 | {s['walkforward']['calmar']:.3f} | {s['walkforward']['total_return']:.1%} | "
                 f"{s['walkforward']['max_dd']:.1%} | {s['baseline']['calmar']:.3f} | {s['baseline']['total_return']:.1%} | "
                 f"{r0} | {k} | {'採用条件を満たす' if res['adopt'] else '不採用'} |")
    L.append("\n### 固定パラメータ別・検証期間成績(仮想資本ごと、Calmar 上位5)\n")
    for cap, res in wf.items():
        top = sorted(res["fixed_params_oos"].items(), key=lambda kv: -kv[1]["calmar"])[:5]
        L.append(f"- {cap:,.0f}円: " + " / ".join(f"{k} Calmar {m['calmar']:.2f} (ret {m['total_return']:.1%})" for k, m in top))
    return "\n".join(L) + "\n"


def conclusions(wf: dict) -> str:
    L = ["\n## 結論\n", "### 確認できたこと(上の数値から)\n"]
    caps = sorted(wf)
    for cap in caps:
        s = wf[cap]["stitched"]
        L.append(f"- 仮想資本 {cap:,.0f}円: WF選択の検証期間 Calmar {s['walkforward']['calmar']:.3f}、"
                 f"リターン {s['walkforward']['total_return']:.1%}。現行は Calmar {s['baseline']['calmar']:.3f}、"
                 f"リターン {s['baseline']['total_return']:.1%}")
    base30 = wf[caps[0]]["stitched"]["baseline"]
    L.append(f"- 3万円では現行パラメータの検証期間リターンが {base30['total_return']:.1%}(丸めで取引がほぼ発生しない)")
    L.append("\n### 推測\n")
    pos = [c for c in caps[1:] if wf[c]["stitched"]["walkforward"]["calmar"] > 0
           and wf[c]["stitched"]["walkforward"]["total_return"] > 0]
    if pos:
        L.append(f"- 資本を増やすと WF 選択が正の成績({', '.join(f'{c:,.0f}円' for c in pos)})になるため、"
                 "3万円での不振はロット制約の影響が大きい可能性がある(推測)。ただし検証期間は5年・6窓のみ。")
    else:
        L.append("- 資本を増やしてロット制約を緩めても WF 選択の検証期間成績は正にならない。"
                 "不振の主因はロット制約ではなく、この戦略(60日モメンタム+ER+ボラ目標)に"
                 "この期間・コスト前提での優位性が見られないこと、と考えられる(推測)。")
    bl = {c: wf[c]["stitched"]["baseline"] for c in caps[1:]}
    if bl:
        L.append("- 現行パラメータ(固定)は資本を増やすと取引が発生し、検証期間で "
                 + " / ".join(f"{c:,.0f}円: Calmar {m['calmar']:.2f}, リターン {m['total_return']:.1%}" for c, m in bl.items())
                 + "。3万円で現行が0%なのはロット制約によるもの(確認できた事実)。"
                 "ただし資本によって値が大きく変わり、優位性は小さく不安定(推測)。")
    L.append("- WF の『学習期間でCalmar最大を選ぶ』手順は、どの資本でも高い target_vol を選んで大きく負けており、"
             "パラメータ最適化そのものに再現性が無い(推測)。")
    L.append("- 3万円・1000通貨では1ペア持つだけでレバ約3〜6倍・1ATR逆行で約1〜2%の損失となり、"
             "ボラ目標による細かなサイズ調整はできない(上表から確認できる事実)。")
    return "\n".join(L) + "\n"


def main(argv=None):
    from .data import closes_frame, fetch_all, five_years_ago, load_cache, save_cache
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="data_cache")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--out", default="reports/lot_constraint_analysis.md")
    a = ap.parse_args(argv)
    data = load_cache(Path(a.cache)) if a.offline else fetch_all(five_years_ago(), min_rows=1000)
    if not a.offline:
        save_cache(data, Path(a.cache))
    closes = closes_frame(data)
    rows = lot_table(closes)
    mins, note = min_size_from_api()
    wf = {cap: run_walkforward(closes, initial=cap) for cap in (30_000.0, 300_000.0, 3_000_000.0)}
    md = render(rows, wf, mins, note, f"{closes.index[0].date()}..{closes.index[-1].date()}") + conclusions(wf)
    md += "\n※ 300万円は「丸めがほぼ効かない」状態の参考値。分析専用で、運用資本は3万円のまま。\n"
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(md)
    print(md)


if __name__ == "__main__":
    main()
