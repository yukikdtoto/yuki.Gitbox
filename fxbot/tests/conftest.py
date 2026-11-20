import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fxbot.instruments import PAIRS  # noqa: E402
from fxbot.notify import Notifier  # noqa: E402
from fxbot.runner import Paths  # noqa: E402

BASE = {"USD_JPY": 150.0, "EUR_USD": 1.08, "GBP_USD": 1.27, "AUD_USD": 0.66}


def make_data(end: date, n: int = 320, drift: float = 0.001, vol: float = 0.002, seed: int = 0):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    out = {}
    for i, p in enumerate(PAIRS):
        r = drift + vol * rng.standard_normal(n)
        c = BASE[p] * np.exp(np.cumsum(r))
        out[p] = pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c}, index=idx)
    return out


@pytest.fixture
def today():
    return date(2026, 11, 18)  # 水曜


@pytest.fixture
def paths(tmp_path):
    p = Paths(state=tmp_path / "state" / "state.json", halt=tmp_path / "state" / "HALT",
              marker=tmp_path / "state" / "INITIALIZED", params=tmp_path / "params.json")
    return p


@pytest.fixture
def aggressive_params(paths):
    paths.params.write_text(json.dumps({"params": {"target_vol": 0.8, "er_min": 0.0}}))
    return paths


@pytest.fixture
def notifier():
    return Notifier(url="")


@pytest.fixture
def trending(today):
    d = make_data(today - timedelta(days=1))
    return lambda: d
