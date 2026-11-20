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
    """HALT解除は人間のみ: unhalt.py 以外に HALT を消す/halted を False にするコードが無い(静的検査)。"""
    forbidden = [r"\.unlink\(", r"os\.remove", r"rmtree", r"halted\"?\]?\s*=\s*False",
                 r"pop\(\s*[\"']halted", r"del\s+\w+\[\s*[\"']halted",
                 r"(from\s+\.?\s*unhalt|import\s+.*unhalt|\.unhalt\s+import)"]
    for f in PKG.glob("*.py"):
        if f.name == "unhalt.py":
            continue
        src = f.read_text()
        for pat in forbidden:
            assert not re.search(pat, src), (f.name, pat)
        # "halted": False は runner の初期化(INIT)ブロックで1回だけ許可
        n = len(re.findall(r"[\"']halted[\"']\s*:\s*False", src))
        if f.name == "runner.py":
            assert n == 1
            k = src.index('"halted": False')
            assert "# INIT:" in src[k - 400:k]
        else:
            assert n == 0, f.name


def test_policy_source_literals():
    """policy.py のソース上の定数をCIで固定(実行時の値だけでなく文字列としても)。"""
    src = (PKG / "policy.py").read_text()
    assert "live_trading: bool = False" in src
    assert "max_leverage: float = 25.0" in src
    assert "max_drawdown_30d: float = -0.25" in src


def test_live_endpoint_only_mentioned_in_broker_denylist():
    for f in PKG.glob("*.py"):
        if "api-fxtrade" in f.read_text():
            assert f.name == "broker.py", f.name
    assert "api-fxtrade.oanda.com" in __import__("fxbot.broker").broker.FORBIDDEN_HOSTS


def test_workflows_default_to_dry_run():
    wf = (PKG.parent.parent / ".github" / "workflows" / "fxbot-daily.yml").read_text()
    assert '"${FXBOT_PRACTICE_ORDERS}" != "true"' in wf and "--dry-run" in wf
    assert "fxbot.smoke" not in wf and "unhalt" not in wf
    assert "pytest" in wf  # テスト失敗時は runner を動かさない
