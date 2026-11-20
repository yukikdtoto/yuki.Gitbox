import dataclasses
import re
from pathlib import Path

import pytest

from fxbot.policy import POLICY, Policy, PolicyViolation, validate

PKG = Path(__file__).resolve().parent.parent / "fxbot"


def test_policy_constants_are_fixed():
    assert POLICY.live_trading is False
    assert POLICY.max_leverage == 25.0
    assert POLICY.max_drawdown_30d == -0.25
    validate(POLICY)


def test_policy_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        POLICY.live_trading = True  # type: ignore[misc]


@pytest.mark.parametrize("kw", [{"live_trading": True}, {"max_leverage": 26}, {"max_leverage": 0},
                                {"max_drawdown_30d": -0.5}, {"max_drawdown_30d": -0.1},
                                {"account_currency": "USD"}])
def test_validate_rejects_changes(kw):
    with pytest.raises(PolicyViolation):
        validate(Policy(**kw))


def test_only_unhalt_can_remove_halt():
    """HALT解除は人間のみ: unhalt.py 以外に HALT を消す/halted=False にするコードが無い。"""
    for f in PKG.glob("*.py"):
        if f.name == "unhalt.py":
            continue
        src = f.read_text()
        assert ".unlink(" not in src, f.name
        assert not re.search(r"halted\"?\]?\s*=\s*False", src), f.name
        assert not re.search(r"(from\s+\.?\s*unhalt|import\s+.*unhalt|\.unhalt\s+import)", src), f.name


def test_live_endpoint_only_mentioned_in_broker_denylist():
    for f in PKG.glob("*.py"):
        if "api-fxtrade" in f.read_text():
            assert f.name == "broker.py", f.name
    assert "api-fxtrade.oanda.com" in __import__("fxbot.broker").broker.FORBIDDEN_HOSTS
