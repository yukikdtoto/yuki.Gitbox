"""runner の敵対的テスト: 初日の全額投げ売り・資産の混同・丸め・キルスイッチ・HALT。"""
import json
from datetime import date, timedelta

import pytest

from fxbot import runner as R
from fxbot.broker import BrokerRefused, PaperBroker
from fxbot.data import DataError
from fxbot.instruments import PAIRS
from fxbot.state import load_state, save_state


def zero():
    return {p: 0 for p in PAIRS}


# ---------- 初日 ----------
def test_first_day_with_existing_positions_does_not_dump(trending, paths, notifier, today):
    b = PaperBroker(nav=3_000_000, positions={"USD_JPY": 5000, "EUR_USD": -3000})
    with pytest.raises(R.SafetyStop):
        R.run(b, trending, paths, notifier, today=today)
    assert b.orders == [] and b.closed_all == 0
    assert b.positions()["USD_JPY"] == 5000


def test_first_day_demo_balance_not_used_as_capital(trending, aggressive_params, notifier, today):
    paths = aggressive_params
    b = PaperBroker(nav=3_000_000)  # デモ口座の初期残高
    res = R.run(b, trending, paths, notifier, today=today)
    assert res.bot_equity == 30_000
    assert res.drawdown_30d == 0
    prices = b.prices()
    gross = sum(abs(u) * (prices[p] if p == "USD_JPY" else prices[p] * prices["USD_JPY"])
                for p, u in b.positions().items())
    assert 0 < gross <= 25 * 30_000  # 300万円ではなく3万円基準
    st = load_state(paths.state)
    assert st["nav_at_start"] == 3_000_000 and st["capital_jpy"] == 30_000


def test_first_day_no_kill_switch_on_empty_history(trending, paths, notifier, today):
    b = PaperBroker(nav=30_000)
    res = R.run(b, trending, paths, notifier, today=today)
    assert res.status == "ok" and b.closed_all == 0
    assert not paths.halt.exists()


def test_switching_account_is_refused(trending, paths, notifier, today):
    R.run(PaperBroker(nav=30_000, id_hash="A"), trending, paths, notifier, today=today)
    b = PaperBroker(nav=30_000, id_hash="B")
    with pytest.raises(R.SafetyStop):
        R.run(b, trending, paths, notifier, today=today + timedelta(days=1))
    assert b.closed_all == 0 and b.orders == []


def test_usd_account_refused(trending, paths, notifier, today):
    with pytest.raises(BrokerRefused):
        R.run(PaperBroker(currency="USD"), trending, paths, notifier, today=today)


def test_foreign_position_refused(trending, paths, notifier, today):
    b = PaperBroker(positions={"XAU_USD": 1})
    with pytest.raises(R.SafetyStop):
        R.run(b, trending, paths, notifier, today=today)
    assert b.orders == []


def test_demo_balance_reset_is_cash_flow_not_loss(trending, paths, notifier, today):
    """建玉ゼロの間にデモ残高が300万→100万に変わっても、損失(キルスイッチ)扱いしない。"""
    R.run(PaperBroker(nav=3_000_000), trending, paths, notifier, today=today)
    st = load_state(paths.state)
    assert not any(st["positions"].values())
    b = PaperBroker(nav=1_000_000)
    res = R.run(b, trending, paths, notifier, today=today + timedelta(days=1))
    assert res.status == "ok" and res.bot_equity == pytest.approx(30_000)
    assert b.closed_all == 0 and not paths.halt.exists()
    assert any("入出金" in n for n in res.notes)


# ---------- データ失敗 ----------
def test_data_failure_stops_before_touching_broker(paths, notifier, today):
    b = PaperBroker(nav=30_000, positions={"USD_JPY": 1000})

    def bad():
        raise DataError("stooq down")
    save_state(paths.state, {"schema": 1, "nav_at_start": 30_000, "account_id_hash": "paper",
                             "positions": {**zero(), "USD_JPY": 1000}, "equity_history": []})
    with pytest.raises(DataError):
        R.run(b, bad, paths, notifier, today=today)
    assert b.orders == [] and b.closed_all == 0


# ---------- 丸め ----------
def test_rounding_to_zero_places_no_order_and_records(trending, paths, notifier, today):
    paths.params.write_text(json.dumps({"params": {"target_vol": 0.05, "er_min": 0.0}}))
    b = PaperBroker(nav=30_000)
    res = R.run(b, trending, paths, notifier, today=today)
    assert b.orders == []
    assert any("1000通貨未満" in n for n in res.notes)


def test_compute_orders_rounds_and_reduces_first():
    cur = {**zero(), "USD_JPY": 1500, "EUR_USD": 2000}
    tgt = {**zero(), "USD_JPY": 3000, "EUR_USD": 0, "GBP_USD": 999}
    orders = R.compute_orders(cur, tgt, 1000)
    assert orders[0] == ("EUR_USD", -2000)        # 減らす注文が先
    assert ("USD_JPY", 1000) in orders            # 1500→3000 の差1500は1000に
    assert all(p != "GBP_USD" for p, _ in orders)  # 999は注文しない
    assert all(u % 1000 == 0 for _, u in orders)


# ---------- キルスイッチ / HALT ----------
def _seed(paths, today, peak=30_000, positions=None):
    hist = [{"date": (today - timedelta(days=d)).isoformat(), "equity": peak} for d in range(10, 1, -1)]
    save_state(paths.state, {"schema": 1, "nav_at_start": 30_000, "capital_jpy": 30_000,
                             "account_id_hash": "paper", "positions": positions or {**zero(), "USD_JPY": 5000},
                             "equity_history": hist, "last_nav": 30_000})


def test_kill_switch_closes_all_and_halts(trending, paths, notifier, today):
    _seed(paths, today)
    b = PaperBroker(nav=22_400, positions={"USD_JPY": 5000})  # -25.3%
    res = R.run(b, trending, paths, notifier, today=today)
    assert res.status == "killed" and b.closed_all == 1
    assert not any(b.positions().values())
    assert paths.halt.exists() and load_state(paths.state)["halted"] is True
    assert any("キルスイッチ" in s for s in notifier.sent)
    # 翌日: 何もしない
    b2 = PaperBroker(nav=22_400)
    assert R.run(b2, trending, paths, notifier, today=today + timedelta(days=1)).status == "halted"
    assert b2.orders == [] and b2.closed_all == 0


def test_kill_switch_boundary(trending, paths, notifier, today):
    _seed(paths, today)
    b = PaperBroker(nav=22_600, positions={"USD_JPY": 5000})  # -24.7% → 発動しない
    assert R.run(b, trending, paths, notifier, today=today).status != "killed"


def test_kill_uses_bot_equity_not_demo_nav(trending, paths, notifier, today):
    """デモ口座NAV 300万のうち 8000円の損失 = 運用資産3万の -26.7% → 発動する。"""
    hist = [{"date": (today - timedelta(days=3)).isoformat(), "equity": 30_000}]
    save_state(paths.state, {"schema": 1, "nav_at_start": 3_000_000, "account_id_hash": "paper",
                             "positions": {**zero(), "USD_JPY": 5000}, "equity_history": hist, "last_nav": 3_000_000})
    b = PaperBroker(nav=2_992_000, positions={"USD_JPY": 5000})
    assert R.run(b, trending, paths, notifier, today=today).status == "killed"


def test_halt_file_alone_keeps_halted(trending, paths, notifier, today):
    paths.halt.parent.mkdir(parents=True, exist_ok=True)
    paths.halt.write_text("x")
    b = PaperBroker()
    assert R.run(b, trending, paths, notifier, today=today).status == "halted"
    assert b.orders == []


def test_state_flag_alone_keeps_halted(trending, paths, notifier, today):
    save_state(paths.state, {"schema": 1, "halted": True})
    b = PaperBroker()
    assert R.run(b, trending, paths, notifier, today=today).status == "halted"
    assert b.orders == []
    assert not paths.halt.exists() and load_state(paths.state)["halted"] is True


def test_weekend_no_orders(trending, aggressive_params, notifier, today):
    sat = today + timedelta(days=(5 - today.weekday()))
    b = PaperBroker()
    res = R.run(b, trending, aggressive_params, notifier, today=sat)
    assert res.status == "weekend" and b.orders == []


def test_dry_run_places_nothing(trending, aggressive_params, notifier, today):
    b = PaperBroker()
    res = R.run(b, trending, aggressive_params, notifier, today=today, dry_run=True)
    assert res.orders and b.orders == []


def test_second_day_rebalance_uses_broker_positions(trending, aggressive_params, notifier, today):
    b = PaperBroker()
    R.run(b, trending, aggressive_params, notifier, today=today)
    n = len(b.orders)
    R.run(b, trending, aggressive_params, notifier, today=today + timedelta(days=1))
    assert len(b.orders) == n  # 同じシグナル・同じ価格なら追加注文なし


# ---------- main() ----------
def test_main_failure_notifies_and_records(monkeypatch, paths, notifier):
    monkeypatch.setattr(R, "Paths", lambda: paths)
    sent = []
    monkeypatch.setattr(R.Notifier, "send", lambda self, lvl, t, b="": sent.append((lvl, t, b)) or False)

    def bad():
        raise DataError("network down")
    rc = R.main([], broker_factory=lambda: PaperBroker(), fetch=bad)
    assert rc == 1
    assert sent and sent[0][0] == "error" and "network down" in sent[0][2]
    assert "network down" in load_state(paths.state)["last_error"]


def test_main_kill_returns_3_and_notifies(monkeypatch, trending, paths, today):
    _seed(paths, date.today())
    monkeypatch.setattr(R, "Paths", lambda: paths)
    sent = []
    monkeypatch.setattr(R.Notifier, "send", lambda self, lvl, t, b="": sent.append(lvl) or False)
    rc = R.main([], broker_factory=lambda: PaperBroker(nav=20_000, positions={"USD_JPY": 5000}),
                fetch=lambda: __import__("tests.conftest", fromlist=["x"]).make_data(date.today() - timedelta(days=1)))
    assert rc == 3 and "kill" in sent


def test_require_webhook(monkeypatch):
    from fxbot.notify import NotifyConfigError, Notifier
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    with pytest.raises(NotifyConfigError):
        Notifier(required=True)
    with pytest.raises(NotifyConfigError):
        Notifier(url="https://evil.example/api/webhooks/x")
