"""古い state を持つ push との競合: 上書きせず StateConflict で止まる。"""
import json
import subprocess

import pytest

from fxbot.runner import StateConflict, git_commit_push


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repos(tmp_path):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    a, b = tmp_path / "a", tmp_path / "b"
    git(tmp_path, "clone", str(remote), str(a))
    for r in (a,):
        git(r, "config", "user.email", "t@example.com"); git(r, "config", "user.name", "t")
        git(r, "checkout", "-b", "main")
    (a / "state").mkdir()
    (a / "state" / "state.json").write_text(json.dumps({"v": 0, "halted": False}))
    git(a, "add", "."); git(a, "commit", "-m", "init"); git(a, "push", "-u", "origin", "main")
    git(tmp_path, "clone", str(remote), str(b))
    git(b, "config", "user.email", "t@example.com"); git(b, "config", "user.name", "t")
    return remote, a, b


def test_push_ok(repos):
    _, a, _ = repos
    f = a / "state" / "state.json"
    f.write_text(json.dumps({"v": 1, "halted": False}))
    git_commit_push([f], "s", a)
    assert "v\": 1" in git(a, "show", "origin/main:state/state.json")


def test_stale_state_conflict_does_not_overwrite_remote_halt(repos, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    _, a, b = repos
    # A(新しい): HALT して push
    (a / "state" / "state.json").write_text(json.dumps({"v": 1, "halted": True}))
    (a / "state" / "HALT").write_text("halted")
    git_commit_push([a / "state" / "state.json", a / "state" / "HALT"], "halt", a)
    # B(古い state のまま): halted=False の state を push しようとする
    fb = b / "state" / "state.json"
    fb.write_text(json.dumps({"v": 2, "halted": False}))
    with pytest.raises(StateConflict):
        git_commit_push([fb], "stale", b)
    git(a, "fetch")
    remote_state = json.loads(git(a, "show", "origin/main:state/state.json"))
    assert remote_state["halted"] is True
    assert "halted" in git(a, "show", "origin/main:state/HALT")


def test_no_change_no_commit(repos):
    _, a, _ = repos
    before = git(a, "rev-parse", "HEAD")
    git_commit_push([a / "state" / "state.json"], "noop", a)
    assert git(a, "rev-parse", "HEAD") == before
