"""日次 runner(cron / GitHub Actions 用)。

流れ: ポリシー検証 → HALT確認 → データ取得 → 口座照合 → キルスイッチ判定
      → 目標ポジション計算 → 注文 → state.json 更新 → (--git) commit/push
失敗時・キルスイッチ発動時は Discord に通知し、非0で終了する。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .broker import Broker, BrokerRefused
from .data import closes_frame, fetch_all
from .instruments import PAIRS
from .notify import Notifier
from .policy import POLICY, validate
from .risk import drawdown_30d, is_halted, kill_switch_triggered, write_halt
from .state import load_state, save_state
from .strategy import gross_leverage, latest_targets, load_config, round_units

ROOT = Path(__file__).resolve().parent.parent


class SafetyStop(RuntimeError):
    """人間の確認が必要な状態。取引せずに止める。"""


@dataclass
class Paths:
    state: Path = ROOT / "state" / "state.json"
    halt: Path = ROOT / "state" / "HALT"
    params: Path = ROOT / "config" / "params.json"


@dataclass
class RunResult:
    status: str
    orders: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    bot_equity: float | None = None
    drawdown_30d: float | None = None


def compute_orders(current: dict[str, int], target: dict[str, int], step: int) -> list[tuple[str, int]]:
    """差分注文。減らす注文を先に出す(証拠金を先に空ける)。"""
    orders = []
    for p in PAIRS:
        d = round_units(target.get(p, 0) - current.get(p, 0), step)
        if d:
            orders.append((p, d))
    reduces = [o for o in orders if abs(current.get(o[0], 0) + o[1]) < abs(current.get(o[0], 0))]
    others = [o for o in orders if o not in reduces]
    return reduces + others


def run(broker: Broker, fetch: Callable[[], dict], paths: Paths, notifier: Notifier,
        today: date | None = None, dry_run: bool = False) -> RunResult:
    validate(POLICY)
    now = datetime.now(timezone.utc)
    today = today or now.date()
    st = load_state(paths.state)
    st["last_run"] = now.isoformat(timespec="seconds")

    if is_halted(paths.halt, st):
        st["halted"] = True
        save_state(paths.state, st)
        notifier.send("warn", "停止中(HALT)", "キルスイッチ等で永続停止中。解除は人間のみ(README参照)。")
        return RunResult("halted")

    data = fetch()                       # 失敗時は DataError で停止
    closes = closes_frame(data)

    acct = broker.account()              # 円口座でなければ BrokerRefused
    if st.get("account_id_hash") and st["account_id_hash"] != acct["id_hash"]:
        raise SafetyStop("state.json と別の口座に接続している(資産の混同)。state を確認してください")
    positions = broker.positions()
    foreign = {k: v for k, v in positions.items() if k not in PAIRS and v}
    if foreign:
        raise SafetyStop(f"対象外の建玉がある(手動取引?): {foreign}")
    positions = {p: int(positions.get(p, 0)) for p in PAIRS}
    notes: list[str] = []

    first_run = "nav_at_start" not in st
    if first_run:
        if any(positions.values()):
            # 初日に既存建玉を「目標0」として全部投げ売りしない
            raise SafetyStop(f"初回起動なのに建玉がある: {positions}。人間が整理してから再実行")
        st.update({"nav_at_start": acct["nav"], "capital_jpy": POLICY.capital_jpy,
                   "account_id_hash": acct["id_hash"], "started": today.isoformat(),
                   "equity_history": [], "positions": positions})
        notes.append(f"初回起動: 口座NAV {acct['nav']:,.0f}円のうち {POLICY.capital_jpy:,.0f}円分のみ運用")
    else:
        # 建玉ゼロだったのにNAVが動いた = 入出金(デモの残高リセット等)。損益と混同しない
        last_nav, last_pos = st.get("last_nav"), st.get("positions", {})
        if last_nav is not None and not any(last_pos.values()) and not any(positions.values()):
            delta = acct["nav"] - last_nav
            if abs(delta) > 1.0:
                st["nav_at_start"] += delta
                notes.append(f"入出金を検出 {delta:+,.0f}円 → 運用資産の基準を補正")
        recorded = {p: int(st.get("positions", {}).get(p, 0)) for p in PAIRS}
        if recorded != positions:
            notes.append(f"建玉が前回記録と不一致: 記録 {st['positions']} / 実際 {positions}")

    # 運用資産 = 元本3万円 + 開始以降の口座損益(デモ口座の残高そのものは使わない)
    bot_equity = min(POLICY.capital_jpy + (acct["nav"] - st["nav_at_start"]), acct["nav"])
    hist = st.get("equity_history", [])
    dd, peak = drawdown_30d(hist, today, bot_equity)
    res = RunResult("ok", notes=notes, bot_equity=bot_equity, drawdown_30d=dd)

    if kill_switch_triggered(dd, POLICY) or bot_equity <= 0:
        closed = [] if dry_run else broker.close_all()
        reason = f"30日DD {dd:.1%} (peak {peak:,.0f} → {bot_equity:,.0f}円) <= {POLICY.max_drawdown_30d:.0%}"
        if not dry_run:
            write_halt(paths.halt, reason)
            st["halted"] = True
        st["halt_reason"] = reason
        st["positions"] = {p: 0 for p in PAIRS}
        _finish(st, acct, bot_equity, today, now, hist, paths)
        notifier.send("kill", "キルスイッチ発動: 全決済+永続停止", f"{reason}\n決済: {closed}")
        res.status = "killed"
        return res

    if today.weekday() >= 5:
        notes.append("週末のため注文なし")
        st["positions"] = positions
        _finish(st, acct, bot_equity, today, now, hist, paths)
        res.status = "weekend"
        return res

    cfg = load_config(paths.params)
    prices = broker.prices()
    target, info = latest_targets(closes, prices, bot_equity, cfg, POLICY)
    if gross_leverage(target, prices, bot_equity) > POLICY.max_leverage + 1e-9:
        raise SafetyStop("目標ポジションがレバ上限超過(バグ)")
    if info["rounded_to_zero"]:
        notes.append(f"1000通貨未満で発注なし: {info['rounded_to_zero']}")
    orders = compute_orders(positions, target, POLICY.unit_step)
    fills = []
    for pair, units in orders:
        if dry_run:
            fills.append({"pair": pair, "units": units, "dry_run": True})
            continue
        fills.append(broker.market_order(pair, units))
    new_pos = dict(positions)
    for pair, units in orders:
        new_pos[pair] += units
    st["positions"] = new_pos if not dry_run else positions
    st["last_signal"] = {"weights": info["weights"], "target": target, "params": cfg.to_dict(),
                         "data_last_date": str(closes.index[-1].date())}
    log = st.setdefault("order_log", [])
    log.append({"date": today.isoformat(), "orders": orders, "dry_run": dry_run})
    st["order_log"] = log[-60:]
    _finish(st, acct, bot_equity, today, now, hist, paths)
    res.orders, res.status = orders, ("dry_run" if dry_run else "ok")
    return res


def _finish(st, acct, bot_equity, today, now, hist, paths):
    hist = [h for h in hist if h["date"] != today.isoformat()]
    hist.append({"date": today.isoformat(), "equity": round(bot_equity, 2)})
    cutoff = (today - timedelta(days=45)).isoformat()
    st["equity_history"] = [h for h in hist if h["date"] >= cutoff]
    st["last_nav"] = acct["nav"]
    st["bot_equity"] = round(bot_equity, 2)
    st["last_success"] = now.isoformat(timespec="seconds")
    st.pop("last_error", None)
    save_state(paths.state, st)


def git_commit_push(files: list[Path], message: str, cwd: Path, retries: int = 4) -> None:
    def g(*a, check=True):
        return subprocess.run(["git", *a], cwd=cwd, check=check, capture_output=True, text=True)
    existing = [str(f) for f in files if f.exists()]
    if existing:
        g("add", "--", *existing)
    if g("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    g("commit", "-m", message)
    for i in range(retries):
        if g("push", check=False).returncode == 0:
            return
        g("pull", "--rebase", check=False)
        time.sleep(2 ** (i + 1))
    raise RuntimeError("git push failed")


def main(argv=None, broker_factory=None, fetch=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="注文を出さない")
    ap.add_argument("--paper", action="store_true", help="PaperBroker を使う(テスト用)")
    ap.add_argument("--git", action="store_true", help="state.json を commit/push")
    ap.add_argument("--require-webhook", action="store_true")
    a = ap.parse_args(argv)
    paths = Paths()
    notifier = Notifier(required=a.require_webhook)
    rc = 0
    try:
        if broker_factory:
            broker = broker_factory()
        elif a.paper:
            from .broker import PaperBroker
            broker = PaperBroker()
        else:
            from .broker import OandaPracticeBroker
            broker = OandaPracticeBroker()
        fetcher = fetch or (lambda: fetch_all(date.today() - timedelta(days=400)))
        res = run(broker, fetcher, paths, notifier, dry_run=a.dry_run)
        print(f"status={res.status} equity={res.bot_equity} dd30={res.drawdown_30d} orders={res.orders}")
        for n in res.notes:
            print(" -", n)
        if res.status == "killed":
            rc = 3
    except Exception as e:  # noqa: BLE001
        rc = 1
        kind = "安全停止(人間の確認が必要)" if isinstance(e, (SafetyStop, BrokerRefused)) else "ジョブ失敗"
        tb = traceback.format_exc(limit=3)
        print(tb, file=sys.stderr)
        try:
            st = load_state(paths.state)
            st["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            st["last_error"] = f"{type(e).__name__}: {e}"[:500]
            save_state(paths.state, st)
        except Exception:  # noqa: BLE001
            pass
        notifier.send("error", kind, f"{type(e).__name__}: {e}"[:1500])
    finally:
        if a.git:
            try:
                git_commit_push([paths.state, paths.halt],
                                f"fxbot: daily state {date.today().isoformat()}", ROOT.parent)
            except Exception as e:  # noqa: BLE001
                notifier.send("error", "git push 失敗", str(e))
                rc = rc or 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
