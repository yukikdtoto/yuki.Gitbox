"""runner の敵対的テスト: 初日の全額投げ売り・資産の混同・丸め・二重発注・キルスイッチ・HALT・state保護。"""
import json
from datetime import date, timedelta

import pytest

from fxbot import runner as R
from fxbot.broker import BrokerError, BrokerRefused, PaperBroker
from fxbot.data import DataError
from fxbot.instruments import PAIRS
from fxbot.state import load_state, save_state


def zero():
    return {p: 0 for p in PAIRS}


def init_run(b, fetch, paths, notifier, today, **kw):
    return R.run(b, fetch, paths, notifier, today=today, init=True, **kw)


def _seed(paths, today, peak=30_000, positions=None, nav_at_start=30_000, **extra):
    hist = [{"date": (today - timedelta(days=d)).isoformat(), "equity": peak} for d in range(10, 1, -1)]
    st = {"schema": 1, "nav_at_start": nav_at_start, "capital_jpy": 30_000, "account_id_hash": "paper",
          "positions": positions if positions is not None else {**zero(), "USD_JPY": 5000},
          "equity_history": hist, "last_nav": nav_at_start, "halted": False}
    st.update(extra)
    save_state(paths.state, st)
    paths.marker.write_text("x")


# ---------- 初期化 ----------
def test_without_init_flag_no_orders_and_notifies(trending, aggressive_params, notifier, today):
    b = PaperBroker()
    res = R.run(b, trending, aggressive_params, notifier, today=today)
    assert res.status == "uninitialized" and b.orders == []
    assert not aggressive_params.marker.exists()
    assert any("未初期化" in s for s in notifier.sent)


def test_init_twice_refused(trending, paths, notifier, today):
    init_run(PaperBroker(), trending, paths, notifier, today)
    with pytest.raises(R.SafetyStop):
        init_run(PaperBroker(), trending, paths, notifier, today + timedelta(days=1))


def test_first_day_with_existing_positions_does_not_dump(trending, paths, notifier, today):
    b = PaperBroker(nav=3_000_000, positions={"USD_JPY": 5000, "EUR_USD": -3000})
    with pytest.raises(R.SafetyStop):
        init_run(b, trending, paths, notifier, today)
    assert b.orders == [] and b.closed_all == 0
    assert b.positions()["USD_JPY"] == 5000


def test_first_day_demo_balance_not_used_as_capital(trending, aggressive_params, notifier, today):
    paths = aggressive_params
    b = PaperBroker(nav=3_000_000)  # デモ口座の初期残高
    res = init_run(b, trending, paths, notifier, today)
    assert res.bot_equity == 30_000 and res.drawdown_30d == 0
    prices = b.prices()
    gross = sum(abs(u) * (prices[p] if p == "USD_JPY" else prices[p] * prices["USD_JPY"])
                for p, u in b.positions().items())
    assert 0 < gross <= 25 * 30_000  # 300万円ではなく3万円基準
    st = load_state(paths.state)
    assert st["nav_at_start"] == 3_000_000 and st["capital_jpy"] == 30_000 and st["halted"] is False


def test_first_day_no_kill_switch_on_empty_history(trending, paths, notifier, today):
    b = PaperBroker(nav=30_000)
    res = init_run(b, trending, paths, notifier, today)
    assert res.status == "ok" and b.closed_all == 0 and not paths.halt.exists()


def test_switching_account_is_refused(trending, paths, notifier, today):
    init_run(PaperBroker(nav=30_000, id_hash="A"), trending, paths, notifier, today)
    b = PaperBroker(nav=30_000, id_hash="B")
    with pytest.raises(R.SafetyStop):
        R.run(b, trending, paths, notifier, today=today + timedelta(days=1))
    assert b.closed_all == 0 and b.orders == []


def test_usd_account_refused(trending, paths, notifier, today):
    with pytest.raises(BrokerRefused):
        init_run(PaperBroker(currency="USD"), trending, paths, notifier, today)


def test_foreign_position_refused(trending, paths, notifier, today):
    b = PaperBroker(positions={"XAU_USD": 1})
    with pytest.raises(R.SafetyStop):
        init_run(b, trending, paths, notifier, today)
    assert b.orders == []


def test_demo_balance_reset_is_cash_flow_not_loss(trending, paths, notifier, today):
    init_run(PaperBroker(nav=3_000_000), trending, paths, notifier, today)
    assert not any(load_state(paths.state)["positions"].values())
    b = PaperBroker(nav=1_000_000)
    res = R.run(b, trending, paths, notifier, today=today + timedelta(days=1))
    assert res.status == "ok" and res.bot_equity == pytest.approx(30_000)
    assert b.closed_all == 0 and not paths.halt.exists()
    assert any("入出金" in n for n in res.notes)


# ---------- データ失敗 ----------
def test_data_failure_stops_before_touching_broker(paths, notifier, today):
    _seed(paths, today, positions={**zero(), "USD_JPY": 1000})
    b = PaperBroker(nav=30_000, positions={"USD_JPY": 1000})

    def bad():
        raise DataError("source down")
    with pytest.raises(DataError):
        R.run(b, bad, paths, notifier, today=today)
    assert b.orders == [] and b.closed_all == 0


# ---------- 丸め / 注文分割 / SL ----------
def test_rounding_to_zero_places_no_order_and_records(trending, paths, notifier, today):
    paths.params.write_text(json.dumps({"params": {"target_vol": 0.05, "er_min": 0.0}}))
    b = PaperBroker(nav=30_000)
    res = init_run(b, trending, paths, notifier, today)
    assert b.orders == [] and any("1000通貨未満" in n for n in res.notes)


def test_compute_orders_rounds_reduces_first_and_splits_flips():
    cur = {**zero(), "USD_JPY": 1500, "EUR_USD": 2000, "AUD_USD": -1000}
    tgt = {**zero(), "USD_JPY": 3000, "EUR_USD": 0, "GBP_USD": 999, "AUD_USD": 2000}
    orders = R.compute_orders(cur, tgt, 1000)
    reduces = [o for o in orders if o.reduce_only]
    assert orders[:len(reduces)] == reduces                    # 減らす注文が先
    assert R.Order("EUR_USD", -2000, True) in orders
    assert R.Order("AUD_USD", 1000, True) in orders            # 反転: まず0へ(REDUCE_ONLY)
    assert R.Order("AUD_USD", 2000, False) in orders           #       次に新規(SL付き)
    assert R.Order("USD_JPY", 1000, False) in orders           # 差1500→1000
    assert all(o.pair != "GBP_USD" for o in orders)            # 999は注文しない
    assert all(o.units % 1000 == 0 for o in orders)


def test_every_opening_order_has_stop_loss_and_client_id(trending, aggressive_params, notifier, today):
    b = PaperBroker()
    init_run(b, trending, aggressive_params, notifier, today)
    assert b.order_meta
    for m in b.order_meta:
        assert m["client_id"].startswith("fxbot-20261118-")
        if not m["reduce_only"]:
            assert m["sl"] and m["sl"] > 0


def test_paper_broker_refuses_order_without_sl():
    with pytest.raises(BrokerRefused):
        PaperBroker().market_order("USD_JPY", 1000, "fxbot-x", stop_loss_distance=None)


# ---------- 二重発注 / 途中失敗 ----------
def test_same_day_rerun_does_not_double_order(trending, aggressive_params, notifier, today):
    b = PaperBroker()
    init_run(b, trending, aggressive_params, notifier, today)
    n, pos = len(b.orders), b.positions()
    R.run(b, trending, aggressive_params, notifier, today=today)
    assert len(b.orders) == n and b.positions() == pos


def test_same_client_id_is_not_resent():
    b = PaperBroker()
    b.market_order("USD_JPY", 1000, "fxbot-a", stop_loss_distance=1.0)
    r = b.market_order("USD_JPY", 1000, "fxbot-a", stop_loss_distance=1.0)
    assert r["existing"] and b.orders == [("USD_JPY", 1000)]


def test_mid_order_failure_then_next_run_reconciles(trending, aggressive_params, notifier, today):
    paths = aggressive_params
    b = PaperBroker()
    b.fail_on_order = 1  # 2件目の注文で失敗
    with pytest.raises(BrokerError):
        init_run(b, trending, paths, notifier, today)
    first = list(b.orders)
    assert len(first) == 1
    # 初期化は保存されていないので、建玉ありの状態で再初期化は拒否(投げ売りしない)
    b.fail_on_order = None
    with pytest.raises(R.SafetyStop):
        init_run(b, trending, paths, notifier, today)


def test_mid_order_failure_after_init_reconciles_from_broker(trending, aggressive_params, notifier, today):
    paths = aggressive_params
    _seed(paths, today, positions=zero())
    b = PaperBroker()
    b.fail_on_order = 1
    with pytest.raises(BrokerError):
        R.run(b, trending, paths, notifier, today=today)
    b.fail_on_order = None
    res = R.run(b, trending, paths, notifier, today=today)
    assert any("不一致" in n for n in res.notes)
    assert load_state(paths.state)["positions"] == {p: b.positions()[p] for p in PAIRS}
    # 1件目は同じclientIDなので再送されない
    ids = [m["client_id"] for m in b.order_meta]
    assert len(ids) == len(set(ids))


def test_main_requires_env_for_orders(monkeypatch, trending, aggressive_params):
    monkeypatch.delenv("FXBOT_PRACTICE_ORDERS", raising=False)
    monkeypatch.setattr(R.Notifier, "send", lambda *a, **k: False)
    b = PaperBroker()
    _seed(aggressive_params, date.today(), positions=zero())
    rc = R.main([], broker_factory=lambda: b, paths=aggressive_params,
                fetch=lambda: __import__("tests.conftest", fromlist=["x"]).make_data(date.today() - timedelta(days=1)))
    assert rc == 0 and b.orders == []   # 変数が無ければドライラン


# ---------- キルスイッチ / HALT ----------
def test_kill_switch_closes_all_and_halts(trending, paths, notifier, today):
    _seed(paths, today)
    b = PaperBroker(nav=22_400, positions={"USD_JPY": 5000})  # -25.3%
    res = R.run(b, trending, paths, notifier, today=today)
    assert res.status == "killed" and b.closed_all == 1 and not any(b.positions().values())
    assert paths.halt.exists() and load_state(paths.state)["halted"] is True
    assert any("キルスイッチ" in s for s in notifier.sent)
    b2 = PaperBroker(nav=22_400)
    assert R.run(b2, trending, paths, notifier, today=today + timedelta(days=1)).status == "halted"
    assert b2.orders == [] and b2.closed_all == 0


def test_kill_switch_runs_even_in_dry_run(trending, paths, notifier, today):
    _seed(paths, today)
    b = PaperBroker(nav=20_000, positions={"USD_JPY": 5000})
    assert R.run(b, trending, paths, notifier, today=today, dry_run=True).status == "killed"
    assert b.closed_all == 1 and paths.halt.exists()


def test_kill_switch_boundary(trending, paths, notifier, today):
    _seed(paths, today)
    b = PaperBroker(nav=22_600, positions={"USD_JPY": 5000})  # -24.7%
    assert R.run(b, trending, paths, notifier, today=today).status != "killed"


def test_kill_uses_bot_equity_not_demo_nav(trending, paths, notifier, today):
    """デモ口座NAV 300万のうち 8000円の損失 = 運用資産3万の -26.7% → 発動する。"""
    _seed(paths, today, nav_at_start=3_000_000)
    b = PaperBroker(nav=2_992_000, positions={"USD_JPY": 5000})
    assert R.run(b, trending, paths, notifier, today=today).status == "killed"


def test_halt_file_alone_keeps_halted(trending, paths, notifier, today):
    _seed(paths, today)
    paths.halt.write_text("x")
    b = PaperBroker(positions={"USD_JPY": 5000})
    assert R.run(b, trending, paths, notifier, today=today).status == "halted"
    assert b.orders == [] and load_state(paths.state)["halted"] is True


def test_state_flag_alone_keeps_halted(trending, paths, notifier, today):
    _seed(paths, today, halted=True)
    b = PaperBroker(positions={"USD_JPY": 5000})
    assert R.run(b, trending, paths, notifier, today=today).status == "halted"
    assert b.orders == [] and load_state(paths.state)["halted"] is True


def test_weekend_no_orders(trending, aggressive_params, notifier, today):
    _seed(aggressive_params, today, positions=zero())
    sat = today + timedelta(days=(5 - today.weekday()))
    b = PaperBroker()
    assert R.run(b, trending, aggressive_params, notifier, today=sat).status == "weekend"
    assert b.orders == []


def test_dry_run_places_nothing(trending, aggressive_params, notifier, today):
    _seed(aggressive_params, today, positions=zero())
    b = PaperBroker()
    res = R.run(b, trending, aggressive_params, notifier, today=today, dry_run=True)
    assert res.orders and b.orders == []


# ---------- D: state 保護(fail-closed) ----------
def _assert_safe_stop(b, fetch, paths, notifier, today, halt_before):
    with pytest.raises(R.SafetyStop):
        R.run(b, fetch, paths, notifier, today=today)
    assert b.orders == [] and b.closed_all == 0
    assert paths.halt.exists() == halt_before


def test_corrupt_state_stops(trending, paths, notifier, today):
    _seed(paths, today)
    paths.state.write_text("{not json")
    _assert_safe_stop(PaperBroker(positions={"USD_JPY": 5000}), trending, paths, notifier, today, False)


def test_corrupt_state_with_halt_keeps_halted(trending, paths, notifier, today):
    _seed(paths, today)
    paths.state.write_text("{not json")
    paths.halt.write_text("x")
    b = PaperBroker(positions={"USD_JPY": 5000})
    assert R.run(b, trending, paths, notifier, today=today).status == "halted"
    assert b.orders == [] and paths.halt.exists()
    assert paths.state.read_text() == "{not json"   # 壊れた state を上書きしない


def test_deleted_state_after_init_stops_and_does_not_reinit(trending, paths, notifier, today):
    _seed(paths, today)
    paths.state.unlink()
    _assert_safe_stop(PaperBroker(), trending, paths, notifier, today, False)
    assert not paths.state.exists()
    with pytest.raises(R.SafetyStop):   # --init でも再初期化しない
        R.run(PaperBroker(), trending, paths, notifier, today=today, init=True)


@pytest.mark.parametrize("key", ["halted", "nav_at_start", "equity_history", "positions", "account_id_hash"])
def test_missing_required_key_stops(trending, paths, notifier, today, key):
    _seed(paths, today)
    st = load_state(paths.state)
    del st[key]
    save_state(paths.state, st)
    _assert_safe_stop(PaperBroker(positions={"USD_JPY": 5000}), trending, paths, notifier, today, False)


def test_halted_not_bool_stops(trending, paths, notifier, today):
    _seed(paths, today, halted="no")
    _assert_safe_stop(PaperBroker(positions={"USD_JPY": 5000}), trending, paths, notifier, today, False)


def test_marker_deleted_stops(trending, paths, notifier, today):
    _seed(paths, today)
    paths.marker.unlink()
    _assert_safe_stop(PaperBroker(positions={"USD_JPY": 5000}), trending, paths, notifier, today, False)


def test_main_safety_stop_notifies_and_keeps_corrupt_state(monkeypatch, trending, paths, today):
    _seed(paths, today)
    paths.state.write_text("garbage")
    sent = []
    monkeypatch.setattr(R.Notifier, "send", lambda self, lvl, t, b="": sent.append((lvl, t)) or False)
    rc = R.main([], broker_factory=lambda: PaperBroker(), fetch=trending, paths=paths)
    assert rc == 1 and sent and sent[0][0] == "error"
    assert paths.state.read_text() == "garbage"


# ---------- main() ----------
def test_main_failure_notifies_and_records(monkeypatch, paths):
    _seed(paths, date.today())
    sent = []
    monkeypatch.setattr(R.Notifier, "send", lambda self, lvl, t, b="": sent.append((lvl, t, b)) or False)

    def bad():
        raise DataError("network down")
    rc = R.main([], broker_factory=lambda: PaperBroker(), fetch=bad, paths=paths)
    assert rc == 1 and sent[0][0] == "error" and "network down" in sent[0][2]
    assert "network down" in load_state(paths.state)["last_error"]


def test_main_kill_returns_3_and_notifies(monkeypatch, paths):
    _seed(paths, date.today())
    sent = []
    monkeypatch.setattr(R.Notifier, "send", lambda self, lvl, t, b="": sent.append(lvl) or False)
    rc = R.main([], broker_factory=lambda: PaperBroker(nav=20_000, positions={"USD_JPY": 5000}), paths=paths,
                fetch=lambda: __import__("tests.conftest", fromlist=["x"]).make_data(date.today() - timedelta(days=1)))
    assert rc == 3 and "kill" in sent


def test_healthcheck_ping_failure_does_not_affect_result(monkeypatch, paths):
    from fxbot import notify
    _seed(paths, date.today(), positions=zero())
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc.example/ping/abc")
    sent = []
    monkeypatch.setattr(R.Notifier, "send", lambda self, lvl, t, b="": sent.append(t) or False)

    class Boom:
        def get(self, *a, **k):
            raise OSError("down")
    monkeypatch.setattr(notify.requests, "Session", lambda: Boom())
    rc = R.main([], broker_factory=lambda: PaperBroker(), paths=paths,
                fetch=lambda: __import__("tests.conftest", fromlist=["x"]).make_data(date.today() - timedelta(days=1)))
    assert rc == 0
    assert any("heartbeat" in t for t in sent)


def test_require_webhook(monkeypatch):
    from fxbot.notify import NotifyConfigError, Notifier
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    with pytest.raises(NotifyConfigError):
        Notifier(required=True)
    with pytest.raises(NotifyConfigError):
        Notifier(url="https://evil.example/api/webhooks/x")
