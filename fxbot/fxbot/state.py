"""state.json の読み書き(アトミック書き込み)。口座IDは保存しない(ハッシュのみ)。"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

SCHEMA = 1


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"schema": SCHEMA}
    d = json.loads(path.read_text())
    if d.get("schema") != SCHEMA:
        raise RuntimeError(f"unsupported state schema {d.get('schema')}")
    return d


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
