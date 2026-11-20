"""HALT 解除(人間専用)。

対話端末(TTY)で確認語を入力した場合のみ解除する。CI / cron(非TTY)からは
常に拒否する。runner からは import も呼び出しもしない。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

from .state import load_state, save_state

CONFIRM = "I AM HUMAN AND I UNDERSTAND THE RISK"


def main(argv=None, stdin=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state/state.json")
    ap.add_argument("--halt", default="state/HALT")
    a = ap.parse_args(argv)
    stdin = stdin or sys.stdin
    if not stdin.isatty():
        print("refused: unhalt requires an interactive terminal (human only)", file=sys.stderr)
        return 2
    halt = Path(a.halt)
    if halt.exists():
        print(halt.read_text())
    print(f"解除するには次を正確に入力: {CONFIRM}")
    if stdin.readline().strip() != CONFIRM:
        print("refused: confirmation mismatch", file=sys.stderr)
        return 2
    st = load_state(Path(a.state))
    st["halted"] = False
    st.setdefault("unhalt_log", []).append(datetime.now(timezone.utc).isoformat())
    # 解除後は30日DDの基準を作り直す(古い高値で即再発動しないように)
    st["equity_history"] = []
    save_state(Path(a.state), st)
    if halt.exists():
        halt.unlink()
    print("unhalted. state.json と HALT の削除を commit/push してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
