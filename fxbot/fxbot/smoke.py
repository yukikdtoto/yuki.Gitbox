"""practice 実API スモークテスト(人間が手動で実行する専用)。

    python -m fxbot.smoke --confirm-practice [--pair USD_JPY]

runner・cron・CI からは呼ばない(呼ぶコードは無く、テストで静的に検査している)。
拒否条件: --confirm-practice なし / practice 以外のURL / HALT 中 / APIキー・口座ID欠落 /
          非対話端末(CI等)。
手順: 口座サマリ → 最小取引単位の取得 → 最小単位で成行(clientExtensions ID + SL)
      → 注文履歴で ID 照合 → その建玉(trade)だけ決済 → 建玉が実行前と同じか確認
smoke の小さな損益は口座NAVに入る。runner は建玉ゼロ時の NAV 変化を「入出金」として
補正する(キルスイッチの誤発動は起きない)。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .broker import BrokerError, OandaPracticeBroker
from .instruments import PAIRS

ROOT = Path(__file__).resolve().parent.parent


class SmokeRefused(RuntimeError):
    pass


def preflight(args, env_ok: bool, halt_file: Path, isatty: bool) -> None:
    if not args.confirm_practice:
        raise SmokeRefused("--confirm-practice が必要(人間による明示実行のみ)")
    if not isatty:
        raise SmokeRefused("対話端末からのみ実行可(CI/cron では実行しない)")
    if halt_file.exists():
        raise SmokeRefused(f"HALT 中のため実行しない: {halt_file}")
    if not env_ok:
        raise SmokeRefused("OANDA_API_KEY / OANDA_ACCOUNT_ID が未設定")
    if args.pair not in PAIRS:
        raise SmokeRefused(f"pair must be one of {PAIRS}")


def run_smoke(broker: OandaPracticeBroker, pair: str, out=print) -> dict:
    steps = {}
    acct = broker.account()
    out(f"[1] 口座サマリ: 通貨={acct['currency']} NAV={acct['nav']:,.0f}")
    steps["account"] = "ok"
    before = broker.positions().get(pair, 0)
    min_size = broker.instrument_min_size(pair)
    out(f"[2] 最小取引単位({pair}): {min_size:g} / 実行前の建玉 {before}")
    steps["min_size"] = min_size
    units = int(max(1, min_size))
    price = broker.prices()[pair]
    sl = round(price * 0.01, 3 if pair.endswith("JPY") else 5)  # 1%先に損切り
    cid = f"smoke-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{pair}"
    fill = broker.market_order(pair, units, cid, stop_loss_distance=sl, unit_step=1)
    trade_id = (fill.get("tradeOpened") or {}).get("tradeID")
    out(f"[3] 発注: {units} units, SL距離 {sl}, clientID {cid}, tradeID {trade_id}")
    steps["order"] = "ok"
    found = broker.find_order(cid)
    if not found or found.get("state") != "FILLED":
        raise BrokerError(f"注文履歴でIDが照合できない: {found}")
    out(f"[4] 注文履歴で照合: state={found.get('state')}")
    steps["lookup"] = "ok"
    if not trade_id:
        raise BrokerError("tradeOpened が無い(既存建玉の相殺になった?)手動で確認してください")
    broker.close_trade(trade_id)
    out(f"[5] 決済: tradeID {trade_id}")
    steps["close"] = "ok"
    time.sleep(1)
    after = broker.positions().get(pair, 0)
    if after != before:
        raise BrokerError(f"建玉が元に戻っていない: before={before} after={after}")
    out(f"[6] 建玉確認: {after}(実行前と一致)")
    steps["flat"] = "ok"
    return steps


def main(argv=None) -> int:
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm-practice", action="store_true")
    ap.add_argument("--pair", default="USD_JPY")
    a = ap.parse_args(argv)
    try:
        preflight(a, bool(os.environ.get("OANDA_API_KEY") and os.environ.get("OANDA_ACCOUNT_ID")),
                  ROOT / "state" / "HALT", sys.stdin.isatty())
        broker = OandaPracticeBroker()  # practice URL 以外はここで拒否
        steps = run_smoke(broker, a.pair)
        print(f"SMOKE OK: {steps}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"SMOKE FAILED/REFUSED: {type(e).__name__}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
