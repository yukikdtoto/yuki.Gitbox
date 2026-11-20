"""heartbeat: runner が止まっていないか確認(GitHub Actions から定期実行)。

state.json の last_run が max_age_hours より古い → 「ジョブ停止」を通知して exit 1。
last_error がある / HALT 中 → 警告を通知(exit 0。runner側でも通知済み)。

初回の猶予: state.json も INITIALIZED も無い(runner が一度も走っていない)間は
誤報しない(info, exit 0)。INITIALIZED があるのに state.json が無いのは異常(exit 1)。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from .notify import Notifier
from .state import load_state


def check(state_path: Path, max_age_hours: float, now: datetime | None = None,
          marker_path: Path | None = None) -> tuple[int, str, str]:
    now = now or datetime.now(timezone.utc)
    marker_path = marker_path or state_path.parent / "INITIALIZED"
    if not state_path.exists():
        if marker_path.exists():
            return 1, "error", "初期化済みなのに state.json が無い"
        return 0, "info", "未稼働(初回実行前の猶予中)"
    try:
        st = load_state(state_path)
    except Exception as e:  # noqa: BLE001
        return 1, "error", f"state.json が読めない: {e}"
    lr = st.get("last_run")
    if not lr:
        return 1, "error", "last_run が無い"
    age = (now - datetime.fromisoformat(lr)).total_seconds() / 3600
    if age > max_age_hours:
        return 1, "error", f"ジョブ停止の疑い: 最終実行 {lr}({age:.1f}時間前 > {max_age_hours}h)"
    if st.get("halted"):
        return 0, "warn", f"HALT中: {st.get('halt_reason', '')}"
    if st.get("last_error"):
        return 0, "warn", f"直近の実行がエラー: {st['last_error']}"
    if st.get("initialized") is False:
        return 0, "warn", f"未初期化のまま稼働中(dry-run の確認段階)最終実行 {lr}"
    return 0, "info", f"OK 最終実行 {lr}({age:.1f}時間前)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=str(Path(__file__).resolve().parent.parent / "state" / "state.json"))
    ap.add_argument("--max-age-hours", type=float, default=30.0)
    a = ap.parse_args(argv)
    rc, level, msg = check(Path(a.state), a.max_age_hours)
    print(msg)
    if level in ("error", "warn"):
        Notifier().send(level, "heartbeat", msg)
    return rc


if __name__ == "__main__":
    sys.exit(main())
