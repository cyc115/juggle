"""Watchdog must launch from the MAIN worktree, never a linked agent worktree.

Regression: `juggle integrate` runs inside an agent worktree, so a __file__-
relative script path launched the watchdog from /tmp/juggle-juggle-<thread>;
when that worktree was GC'd post-integrate, the daemon's module path vanished
and every later dispatch failed with `No module named juggle_cmd_agents`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def test_watchdog_script_uses_main_worktree_root(monkeypatch, tmp_path):
    import juggle_cmd_agents_worktree as wt
    import juggle_cmd_threads as t

    main = tmp_path / "main-juggle"
    monkeypatch.setattr(wt, "_main_worktree_root", lambda start: str(main))

    assert t._main_repo_root() == main
    assert t._watchdog_script() == main / "scripts" / "juggle-agent-watchdog"


def test_watchdog_script_not_under_tmp_worktree(monkeypatch, tmp_path):
    import juggle_cmd_agents_worktree as wt
    import juggle_cmd_threads as t

    # Even if __file__ were a worktree, the helper redirects to the primary.
    monkeypatch.setattr(wt, "_main_worktree_root",
                        lambda start: "/Users/x/github/juggle")
    assert "juggle-juggle" not in str(t._watchdog_script())
