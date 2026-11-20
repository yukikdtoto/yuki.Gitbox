"""ウォークフォワード検証(2年学習 → 6か月検証 を6か月ずつずらす)。

比較対象: target_vol × er_min のグリッド。採用基準は Calmar(単純リターンも併記)。
採用ルール(すべて満たした時のみ採用。満たさない場合は不採用理由を記録):
  1. WF選択(各窓で学習期間のCalmar最大を選び、検証期間に適用)を連結した
     検証期間全体のCalmar が、現行(BASELINE)の検証期間Calmar を上回る
  2. 同じく検証期間全体の単純リターンが現行を下回らない
  3. 検証窓の過半数で現行以上のCalmar
  4. 検証期間全体で キルスイッチ発動なし
採用時に配備するパラメータは「最新の学習窓」で選ばれたもの。
"""
from __future__ import annotations

import itertools
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from .backtest import metrics, run_backtest
from .strategy import BASELINE, StrategyConfig

TARGET_VOLS = (0.10, 0.20, 0.40, 0.60, 0.80)
ER_MINS = (0.0, 0.1, 0.2, 0.3)


def grid(base: StrategyConfig = BASELINE) -> list[StrategyConfig]:
    return [replace(base, target_vol=tv, er_min=er) for tv, er in itertools.product(TARGET_VOLS, ER_MINS)]


def windows(index: pd.DatetimeIndex, start: pd.Timestamp, train_months=24, test_months=6):
    out = []
    t0 = start
    last = index[-1]
    while True:
        tr_end = t0 + pd.DateOffset(months=train_months)
        te_end = tr_end + pd.DateOffset(months=test_months)
        if tr_end >= last:
            break
        out.append((t0, tr_end, min(te_end, last + pd.Timedelta(days=1))))
        if te_end > last:
            break
        t0 = t0 + pd.DateOffset(months=test_months)
    return out


def _stitch(curves: list[pd.Series], initial: float) -> pd.Series:
    """各窓のリターンを連結(各窓は元本30,000円から開始して検証)。"""
    parts, eq = [], initial
    for c in curves:
        r = c / c.iloc[0]
        parts.append(r * eq)
        eq = float(parts[-1].iloc[-1])
    s = pd.concat(parts)
    return s[~s.index.duplicated(keep="last")]


def key(c: StrategyConfig) -> str:
    return f"tv={c.target_vol:.2f},er={c.er_min:.1f}"


def run_walkforward(closes: pd.DataFrame, years: int = 5, initial: float = 30_000.0,
                    base: StrategyConfig = BASELINE, cost_multiplier: float = 1.0) -> dict:
    end = closes.index[-1]
    start = end - pd.DateOffset(years=years)
    wins = windows(closes.index, start)
    if not wins:
        raise ValueError("not enough data for walk-forward")
    cands = grid(base)
    if key(base) not in {key(c) for c in cands}:
        cands.append(base)
    rows, wf_curves, base_curves = [], [], []
    fixed_curves: dict[str, list[pd.Series]] = {key(c): [] for c in cands}
    fixed_halts: dict[str, int] = {key(c): 0 for c in cands}
    chosen = None
    for tr_s, tr_e, te_e in wins:
        train = {}
        for c in cands:
            r = run_backtest(closes, c, tr_s, tr_e, initial, cost_multiplier=cost_multiplier)
            train[key(c)] = (c, r.metrics)
        # 学習期間のCalmar最大(同点は単純リターン → 低target_vol を優先)
        best_c, best_m = max(train.values(),
                             key=lambda cm: (cm[1]["calmar"], cm[1]["total_return"], -cm[0].target_vol))
        chosen = best_c
        test = {}
        for c in cands:
            r = run_backtest(closes, c, tr_e, te_e, initial, cost_multiplier=cost_multiplier)
            test[key(c)] = r
            fixed_curves[key(c)].append(r.equity)
            fixed_halts[key(c)] += int(r.halted_on is not None)
        wf_r, base_r = test[key(best_c)], test[key(base)]
        wf_curves.append(wf_r.equity)
        base_curves.append(base_r.equity)
        rows.append({
            "train": f"{tr_s.date()}..{tr_e.date()}", "test": f"{tr_e.date()}..{te_e.date()}",
            "chosen": key(best_c), "train_calmar": round(best_m["calmar"], 3),
            "wf_test": {k: round(v, 4) for k, v in wf_r.metrics.items()},
            "base_test": {k: round(v, 4) for k, v in base_r.metrics.items()},
            "wf_halted": wf_r.halted_on, "base_halted": base_r.halted_on,
            "wf_trades": wf_r.trades, "wf_rounded_to_zero": wf_r.rounded_to_zero,
            "wf_exposure_days": wf_r.exposure_days,
        })
    wf_m = metrics(_stitch(wf_curves, initial))
    base_m = metrics(_stitch(base_curves, initial))
    fixed = {}
    for k, cs in fixed_curves.items():
        m = metrics(_stitch(cs, initial))
        fixed[k] = {**{x: round(v, 4) for x, v in m.items()}, "halts": fixed_halts[k]}

    wins_beat = sum(1 for r in rows if r["wf_test"]["calmar"] >= r["base_test"]["calmar"])
    reasons = []
    if not wf_m["calmar"] > base_m["calmar"]:
        reasons.append(f"検証期間Calmarが現行以下 (WF {wf_m['calmar']:.3f} <= 現行 {base_m['calmar']:.3f})")
    if wf_m["total_return"] < base_m["total_return"]:
        reasons.append(f"検証期間リターンが現行未満 (WF {wf_m['total_return']:.1%} < 現行 {base_m['total_return']:.1%})")
    if wins_beat * 2 <= len(rows):
        reasons.append(f"現行以上の窓が過半数でない ({wins_beat}/{len(rows)})")
    if any(r["wf_halted"] for r in rows):
        reasons.append("検証期間中にキルスイッチ発動あり")
    adopt = not reasons and key(chosen) != key(base)
    if not reasons and key(chosen) == key(base):
        reasons.append("最新学習窓の選択が現行と同じ(変更不要)")

    # 個別案(固定パラメータ)の現行比較: 情報として記録(採用判断はWF手順のみ)
    per_candidate = {}
    bm = fixed[key(base)]
    for k, m in fixed.items():
        if k == key(base):
            per_candidate[k] = "現行"
            continue
        why = []
        if m["calmar"] <= bm["calmar"]:
            why.append("Calmar改善なし")
        if m["total_return"] < bm["total_return"]:
            why.append("リターン悪化")
        if m["halts"]:
            why.append(f"キルスイッチ{m['halts']}回")
        per_candidate[k] = "検証期間で改善(ただし事後選択のため単独では採用しない)" if not why else "不採用: " + "・".join(why)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_range": f"{closes.index[0].date()}..{closes.index[-1].date()}",
        "initial_jpy": initial, "cost_multiplier": cost_multiplier,
        "baseline": base.to_dict(), "windows": rows,
        "stitched": {"walkforward": {k: round(v, 4) for k, v in wf_m.items()},
                     "baseline": {k: round(v, 4) for k, v in base_m.items()}},
        "fixed_params_oos": fixed, "per_candidate": per_candidate,
        "windows_wf_ge_base": f"{wins_beat}/{len(rows)}",
        "latest_choice": chosen.to_dict(),
        "adopt": adopt, "reasons": reasons,
    }


def render_markdown(res: dict, stress: dict | None = None) -> str:
    L = []
    L.append(f"# ウォークフォワード検証レポート ({res['generated_at'][:10]})\n")
    L.append(f"- データ: ECB参照レート日足 {res['data_range']} / 元本 {res['initial_jpy']:,.0f}円 / 1000通貨丸め / レバ上限25倍 / 30日DD-25%でキルスイッチ")
    L.append("- 方式: 2年学習 → 6か月検証、6か月ずつスライド。学習期間のCalmar最大を検証期間に適用")
    L.append(f"- 現行(BASELINE): target_vol={res['baseline']['target_vol']}, er_min={res['baseline']['er_min']}\n")
    v = "**採用**" if res["adopt"] else "**不採用(現行パラメータ維持)**"
    L.append(f"## 判定: {v}\n")
    for r in res["reasons"]:
        L.append(f"- 理由: {r}")
    if res["adopt"]:
        lc = res["latest_choice"]
        L.append(f"- 配備: target_vol={lc['target_vol']}, er_min={lc['er_min']}")
    s = res["stitched"]
    L.append("\n## 検証期間(連結)\n")
    L.append("| 方式 | Calmar | 単純リターン | CAGR | 最大DD |\n|---|---:|---:|---:|---:|")
    for name, k in (("WF選択", "walkforward"), ("現行", "baseline")):
        m = s[k]
        L.append(f"| {name} | {m['calmar']:.3f} | {m['total_return']:.1%} | {m['cagr']:.1%} | {m['max_dd']:.1%} |")
    L.append(f"\n現行以上だった窓: {res['windows_wf_ge_base']}\n")
    L.append("## 窓ごと\n")
    L.append("| 検証期間 | 選択 | 学習Calmar | WF Calmar | WF リターン | 現行 Calmar | 現行 リターン | 丸め0件 | 停止 |\n|---|---|---:|---:|---:|---:|---:|---:|---|")
    for r in res["windows"]:
        L.append(f"| {r['test']} | {r['chosen']} | {r['train_calmar']:.2f} | {r['wf_test']['calmar']:.2f} | "
                 f"{r['wf_test']['total_return']:.1%} | {r['base_test']['calmar']:.2f} | {r['base_test']['total_return']:.1%} | "
                 f"{r['wf_rounded_to_zero']} | {r['wf_halted'] or '-'} |")
    L.append("\n## 固定パラメータ別の検証期間成績(参考・事後比較)\n")
    L.append("| 案 | Calmar | 単純リターン | 最大DD | キルスイッチ | 判定 |\n|---|---:|---:|---:|---:|---|")
    for k, m in sorted(res["fixed_params_oos"].items(), key=lambda kv: -kv[1]["calmar"]):
        L.append(f"| {k} | {m['calmar']:.3f} | {m['total_return']:.1%} | {m['max_dd']:.1%} | {m['halts']} | {res['per_candidate'][k]} |")
    if stress:
        L.append("\n## コスト2倍のストレス(WF手順を再実行)\n")
        ss = stress["stitched"]
        L.append(f"- WF: Calmar {ss['walkforward']['calmar']:.3f} / リターン {ss['walkforward']['total_return']:.1%}")
        L.append(f"- 現行: Calmar {ss['baseline']['calmar']:.3f} / リターン {ss['baseline']['total_return']:.1%}")
        L.append(f"- 判定(コスト2倍): {'採用' if stress['adopt'] else '不採用'} {' / '.join(stress['reasons'])}")
    L.append("\n## 前提と限界\n")
    L.append("- コスト: 片道 = 半スプレッド + スリッページ0.3pips(USD/JPY 0.4, EUR/USD 0.5, GBP/USD 1.0, AUD/USD 0.6pips)。"
             "保有コストは想定元本の年1.5%を方向によらず支払い扱い(受取スワップは計上しない)。")
    L.append("- 終値で判断・終値で約定。実際の約定は翌日のcron時点の価格なので、ずれが生じる。")
    L.append("- 3万円・1000通貨単位では1ペア1000通貨でもレバ約3〜6倍になり、ボラ目標が小さいと丸めで発注が0になる(上表『丸め0件』)。")
    L.append("- 過去成績は将来を保証しない。デモ口座のみで運用し、実弾は対象外。")
    return "\n".join(L) + "\n"


def main(argv=None):
    import argparse
    from .data import closes_frame, fetch_all, five_years_ago, load_cache, save_cache
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default="data_cache")
    ap.add_argument("--offline", action="store_true", help="キャッシュのみ使用")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--params-out", default="config/params.json")
    a = ap.parse_args(argv)
    cache = Path(a.cache)
    if a.offline:
        data = load_cache(cache)
    else:
        data = fetch_all(five_years_ago(), min_rows=1000)
        save_cache(data, cache)
    closes = closes_frame(data)
    res = run_walkforward(closes)
    stress = run_walkforward(closes, cost_multiplier=2.0)
    out = Path(a.reports)
    out.mkdir(parents=True, exist_ok=True)
    stamp = date.today().isoformat()
    (out / f"walkforward_{stamp}.json").write_text(json.dumps({"base": res, "cost_x2": stress}, ensure_ascii=False, indent=2))
    (out / f"walkforward_{stamp}.md").write_text(render_markdown(res, stress))
    params_path = Path(a.params_out)
    params_path.parent.mkdir(parents=True, exist_ok=True)
    # コスト2倍でも採用条件を満たす場合のみ配備
    deployed = res["latest_choice"] if (res["adopt"] and stress["adopt"]) else res["baseline"]
    params_path.write_text(json.dumps({
        "params": deployed, "decided_at": res["generated_at"],
        "adopted_new": bool(res["adopt"] and stress["adopt"]),
        "reasons": res["reasons"] + [f"[コスト2倍] {r}" for r in stress["reasons"]],
        "report": f"reports/walkforward_{stamp}.md"}, ensure_ascii=False, indent=2))
    print(render_markdown(res, stress))


if __name__ == "__main__":
    main()
