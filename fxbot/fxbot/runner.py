"""日次 runner(cron / GitHub Actions 用)。

流れ: ポリシー検証 → HALT確認 → state検証(fail-closed) → データ取得 → 口座照合
      → キルスイッチ判定 → 目標ポジション計算 → 注文(SL同梱・clientID冪等)
      → state.json 更新 → (--git) commit/push
失敗時・キルスイッチ発動時は Discord に通知し、非0で終了する。

発注の二重ロック: --dry-run を付けない かつ 環境変数 FXBOT_PRACTICE_ORDERS=true の
ときだけ practice へ注文する。どちらかが欠ければドライラン。
初期化は --init を付けたときだけ。過去に初期化済み(state/INITIALIZED がある)のに
state.json が無い/壊れている/必須項目が欠けている場合は、新規初期化せず停止する。
"""
from __future__ import annotations

import argparse
import json
import os
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
from .notify import HealthPing, Notifier
from .policy import POLICY, validate
from .risk import drawdown_30d, kill_switch_triggered, write_halt
from .state import load_state, save_state
from .strategy import gross_leverage, latest_targets, load_config, round_units, stop_distance

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_KEYS = ("nav_at_start", "account_id_hash", "halted", "equity_history", "positions")


class SafetyStop(RuntimeError):
    """人間の確認が必要な状態。取引せずに止める。"""


class StateConflict(SafetyStop):
    pass


@dataclass
class Paths:
    state: Path = ROOT / "state" / "state.json"
    halt: Path = ROOT / "state" / "HALT"
    marker: Path = ROOT / "state" / "INITIALIZED"
    params: Path = ROOT / "config" / "params.json"


@dataclass
class Order:
    pair: str
    units: int
    reduce_only: bool

    def as_tuple(self):
        return (self.pair, self.units)


@dataclass
class RunResult:
    status: str
    orders: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    bot_equity: float | None = None
    drawdown_30d: float | None = None


def compute_orders(current: dict[str, int], target: dict[str, int], step: int) -> list[Order]:
    """差分注文。符号が反転する場合は「0まで減らす(REDUCE_ONLY)」と「新規(SL付き)」に分割。
    減らす注文を先に出す(証拠金を先に空ける)。"""
    reduces, opens = [], []
    for p in PAIRS:
        cur = int(current.get(p, 0))
        d = round_units(int(target.get(p, 0)) - cur, step)
        if not d:
            continue
        new = cur + d
        if cur and (new == 0 or (new > 0) != (cur > 0)):
            reduces.append(Order(p, -cur, True))
            if new:
                opens.append(Order(p, new, False))
        elif abs(new) < abs(cur):
            reduces.append(Order(p, d, True))
        else:
            opens.append(Order(p, d, False))
    return reduces + opens


def client_id(today: date, o: Order) -> str:
    return f"fxbot-{today:%Y%m%d}-{o.pair}-{'r' if o.reduce_only else 'o'}{o.units}"


def _load_checked(paths: Paths) -> dict | None:
    """fail-closed な state 読み込み。None = 未初期化(初回)。"""
    initialized = paths.marker.exists()
    if not paths.state.exists():
        if initialized:
            raise SafetyStop("state.json が無いが初期化済み(INITIALIZED あり)。新規初期化せず停止")
        return None
    try:
        st = load_state(paths.state)
    except (json.JSONDecodeError, RuntimeError, OSError) as e:
        raise SafetyStop(f"state.json が壊れている: {e}") from e
    if not isinstance(st, dict):
        raise SafetyStop("state.json の形式が不正")
    if initialized or "nav_at_start" in st:
        if not initialized:
            raise SafetyStop("state は初期化済みなのに INITIALIZED が無い(削除された?)")
        missing = [k for k in REQUIRED_KEYS if k not in st]
        if missing:
            raise SafetyStop(f"state.json に必須項目が無い: {missing}")
        if not isinstance(st["halted"], bool):
            raise SafetyStop("state.halted が bool でない")
    return st


def run(broker: Broker, fetch: Callable[[], dict], paths: Paths, notifier: Notifier,
        today: date | None = None, dry_run: bool = False, init: bool = False) -> RunResult:
    validate(POLICY)
    now = datetime.now(timezone.utc)
    today = today or now.date()

    # HALTファイルは state より先に確認(state が壊れていても停止を維持)
    if paths.halt.exists():
        try:
            st = _load_checked(paths)
            if st is not None:
                st["halted"] = True
                st["last_run"] = now.isoformat(timespec="seconds")
                save_state(paths.state, st)
        except SafetyStop:
            pass
        notifier.send("warn", "停止中(HALT)", "キルスイッチ等で永続停止中。解除は人間のみ(README参照)。")
        return RunResult("halted")

    st = _load_checked(paths)
    if st is not None and st.get("halted"):
        st["last_run"] = now.isoformat(timespec="seconds")
        save_state(paths.state, st)
        notifier.send("warn", "停止中(HALT)", "state.halted=True。解除は人間のみ。")
        return RunResult("halted")
    if st is None:
        st = {"schema": 1}
    if init and "nav_at_start" in st:
        raise SafetyStop("既に初期化済み。--init は使えない")

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

    if "nav_at_start" not in st:
        if not init:
            st["last_run"] = now.isoformat(timespec="seconds")
            st["initialized"] = False
            save_state(paths.state, st)
            notifier.send("warn", "未初期化", "データ取得と口座接続は成功。運用開始は --init を付けて人間が実行")
            return RunResult("uninitialized", notes=["未初期化: --init が必要"])
        if any(positions.values()):
            # 初日に既存建玉を「目標0」として全部投げ売りしない
            raise SafetyStop(f"初回起動なのに建玉がある: {positions}。人間が整理してから再実行")
        # INIT: 初期値の設定(halted の初期値はここでのみ設定)
        st.update({"nav_at_start": acct["nav"], "capital_jpy": POLICY.capital_jpy,
                   "account_id_hash": acct["id_hash"], "started": today.isoformat(),
                   "equity_history": [], "positions": positions, "halted": False, "initialized": True})
        paths.marker.parent.mkdir(parents=True, exist_ok=True)
        paths.marker.write_text(f"initialized {today.isoformat()} account {acct['id_hash']}\n")
        notes.append(f"初期化: 口座NAV {acct['nav']:,.0f}円のうち {POLICY.capital_jpy:,.0f}円分のみ運用")
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
            notes.append(f"建玉が前回記録と不一致(実建玉を正とする): 記録 {recorded} / 実際 {positions}")

    # 運用資産 = 元本3万円 + 開始以降の口座損益(デモ口座の残高そのものは使わない)
    bot_equity = min(POLICY.capital_jpy + (acct["nav"] - st["nav_at_start"]), acct["nav"])
    hist = st.get("equity_history", [])
    dd, peak = drawdown_30d(hist, today, bot_equity)
    res = RunResult("ok", notes=notes, bot_equity=bot_equity, drawdown_30d=dd)

    if kill_switch_triggered(dd, POLICY) or bot_equity <= 0:
        # キルスイッチはリスクを減らす操作なので dry-run でも実行する
        closed = broker.close_all()
        reason = f"30日DD {dd:.1%} (peak {peak:,.0f} → {bot_equity:,.0f}円) <= {POLICY.max_drawdown_30d:.0%}"
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
    log = st.setdefault("order_log", [])
    entry = {"date": today.isoformat(), "orders": [o.as_tuple() for o in orders], "dry_run": dry_run,
             "status": "pending"}
    for o in orders:
        cid = client_id(today, o)
        sl = None if o.reduce_only else stop_distance(closes[o.pair])
        if dry_run:
            continue
        broker.market_order(o.pair, o.units, cid, stop_loss_distance=sl, reduce_only=o.reduce_only)
    entry["status"] = "done"
    log.append(entry)
    st["order_log"] = log[-60:]
    # 注文後はブローカーの実建玉を正とする
    after = broker.positions() if not dry_run else positions
    st["positions"] = {p: int(after.get(p, 0)) for p in PAIRS}
    st["last_signal"] = {"weights": info["weights"], "target": target, "params": cfg.to_dict(),
                         "data_last_date": str(closes.index[-1].date())}
    _finish(st, acct, bot_equity, today, now, hist, paths)
    res.orders = [o.as_tuple() for o in orders]
    res.status = "dry_run" if dry_run else "ok"
    return res


def _finish(st, acct, bot_equity, today, now, hist, paths):
    hist = [h for h in hist if h["date"] != today.isoformat()]
    hist.append({"date": today.isoformat(), "equity": round(bot_equity, 2)})
    cutoff = (today - timedelta(days=45)).isoformat()
    st["equity_history"] = [h for h in hist if h["date"] >= cutoff]
    st["last_nav"] = acct["nav"]
    st["bot_equity"] = round(bot_equity, 2)
    st["last_run"] = now.isoformat(timespec="seconds")
    st["last_success"] = now.isoformat(timespec="seconds")
    st.pop("last_error", None)
    save_state(paths.state, st)


def git_commit_push(files: list[Path], message: str, cwd: Path, retries: int = 3) -> None:
    """state を commit/push。push が拒否されたら fetch → rebase。
    rebase が衝突したら(他から新しい state が push された)上書きせず StateConflict。"""
    def g(*a, check=True):
        return subprocess.run(["git", *a], cwd=cwd, check=check, capture_output=True, text=True)
    existing = [str(f) for f in files if f.exists()]
    if existing:
        g("add", "--", *existing)
    if g("diff", "--cached", "--quiet", check=False).returncode == 0:
        return
    g("commit", "-m", message)
    branch = g("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    for i in range(retries):
        if g("push", "origin", f"HEAD:{branch}", check=False).returncode == 0:
            return
        g("fetch", "origin", branch, check=False)
        if g("rebase", f"origin/{branch}", check=False).returncode != 0:
            g("rebase", "--abort", check=False)
            raise StateConflict("リモートに新しい state があり衝突。上書きせず停止(人間が確認)")
        time.sleep(2 ** i)
    raise RuntimeError("git push failed")


def main(argv=None, broker_factory=None, fetch=None, paths: Paths | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="注文を出さない")
    ap.add_argument("--init", action="store_true", help="初回のみ: 運用開始(人間が明示)")
    ap.add_argument("--paper", action="store_true", help="PaperBroker を使う(テスト用)")
    ap.add_argument("--git", action="store_true", help="state を commit/push")
    ap.add_argument("--require-webhook", action="store_true")
    a = ap.parse_args(argv)
    paths = paths or Paths()
    notifier = Notifier(required=a.require_webhook)
    ping = HealthPing(notifier=notifier)
    # 発注の二重ロック
    dry_run = a.dry_run or os.environ.get("FXBOT_PRACTICE_ORDERS", "") != "true"
    ping.ping("start")
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
        res = run(broker, fetcher, paths, notifier, dry_run=dry_run, init=a.init)
        print(f"status={res.status} dry_run={dry_run} equity={res.bot_equity} dd30={res.drawdown_30d} orders={res.orders}")
        for n in res.notes:
            print(" -", n)
        if res.status == "killed":
            rc = 3
    except Exception as e:  # noqa: BLE001
        rc = 1
        kind = "安全停止(人間の確認が必要)" if isinstance(e, (SafetyStop, BrokerRefused)) else "ジョブ失敗"
        print(traceback.format_exc(limit=3), file=sys.stderr)
        try:
            st = load_state(paths.state) if paths.state.exists() else None
            if st is not None and isinstance(st, dict):
                st["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                st["last_error"] = f"{type(e).__name__}: {e}"[:500]
                save_state(paths.state, st)
        except Exception:  # noqa: BLE001  壊れた state は上書きしない
            pass
        notifier.send("error", kind, f"{type(e).__name__}: {e}"[:1500])
    if a.git:
        try:
            git_commit_push([paths.state, paths.halt, paths.marker],
                            f"fxbot: daily state {date.today().isoformat()}", ROOT.parent)
        except Exception as e:  # noqa: BLE001
            notifier.send("error", "git push 失敗", f"{type(e).__name__}: {e}")
            rc = rc or 1
    ping.ping("" if rc in (0,) else "fail")
    return rc


if __name__ == "__main__":
    sys.exit(main())
