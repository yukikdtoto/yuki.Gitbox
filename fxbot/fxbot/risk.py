"""キルスイッチ(30日ドローダウン)と HALT 管理。

HALT はボットからは「作る」ことしかできない。解除関数はここには無い
(人間用の fxbot/unhalt.py のみ)。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .policy import POLICY, Policy


def drawdown_30d(history: list[dict], today: date, equity_now: float) -> tuple[float, float]:
    """直近30暦日(当日含む)の最高値からの下落率と、その最高値を返す。

    history: [{"date": "YYYY-MM-DD", "equity": float}, ...]
    履歴が無い初日は peak = equity_now (= 下落率 0)。
    """
    since = today - timedelta(days=30)
    vals = [float(h["equity"]) for h in history
            if date.fromisoformat(h["date"]) >= since]
    peak = max(vals + [equity_now])
    if peak <= 0:
        return -1.0, peak
    return equity_now / peak - 1.0, peak


def kill_switch_triggered(dd: float, policy: Policy = POLICY) -> bool:
    return dd <= policy.max_drawdown_30d


def is_halted(halt_file: Path, state: dict) -> bool:
    return halt_file.exists() or bool(state.get("halted"))


def write_halt(halt_file: Path, reason: str) -> None:
    halt_file.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat()
    halt_file.write_text(f"HALTED at {ts}\nreason: {reason}\n"
                         "解除は人間のみ: python -m fxbot.unhalt を対話端末で実行\n")
