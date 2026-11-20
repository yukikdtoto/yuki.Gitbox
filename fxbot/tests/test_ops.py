import io
from datetime import datetime, timedelta, timezone

from fxbot import heartbeat, unhalt
from fxbot.state import load_state, save_state


def test_heartbeat_stale(tmp_path):
    p = tmp_path / "s.json"
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
    assert heartbeat.check(p, 30, now)[:2] == (0, "info")      # 初回前の猶予
    (tmp_path / "INITIALIZED").write_text("x")
    assert heartbeat.check(p, 30, now)[0] == 1                  # 初期化済みで state 消失
    p.write_text("{broken")
    assert heartbeat.check(p, 30, now)[0] == 1
    save_state(p, {"schema": 1, "last_run": (now - timedelta(hours=31)).isoformat()})
    rc, lvl, msg = heartbeat.check(p, 30, now)
    assert rc == 1 and "停止" in msg
    save_state(p, {"schema": 1, "last_run": (now - timedelta(hours=2)).isoformat()})
    assert heartbeat.check(p, 30, now)[:2] == (0, "info")
    save_state(p, {"schema": 1, "last_run": now.isoformat(), "halted": True})
    assert heartbeat.check(p, 30, now)[1] == "warn"


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


def test_unhalt_refuses_non_tty(tmp_path):
    s, h = tmp_path / "s.json", tmp_path / "HALT"
    save_state(s, {"schema": 1, "halted": True})
    h.write_text("x")
    assert unhalt.main(["--state", str(s), "--halt", str(h)], stdin=io.StringIO(unhalt.CONFIRM + "\n")) == 2
    assert h.exists() and load_state(s)["halted"] is True


def test_unhalt_requires_exact_phrase(tmp_path):
    s, h = tmp_path / "s.json", tmp_path / "HALT"
    save_state(s, {"schema": 1, "halted": True})
    h.write_text("x")
    assert unhalt.main(["--state", str(s), "--halt", str(h)], stdin=FakeTTY("yes\n")) == 2
    assert h.exists()
    assert unhalt.main(["--state", str(s), "--halt", str(h)], stdin=FakeTTY(unhalt.CONFIRM + "\n")) == 0
    assert not h.exists() and load_state(s)["halted"] is False
