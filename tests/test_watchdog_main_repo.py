"""Watchdog must launch from the MAIN worktree, never a linked agent worktree.

Regression: `juggle integrate` runs inside an agent worktree, so a __file__-
relative script path launched the watchdog from /tmp/juggle-juggle-<thread>;
when that worktree was GC'd post-integrate, the daemon's module path vanished
and every later dispatch failed with `No module named juggle_cmd_agents`.

2026-06-20 (RCA §P2): the pidfile launcher (juggle_cmd_threads._start_watchdog /
_watchdog_script / _main_repo_root) was removed so the flock is the ONE
coordination primitive. The same "launch from the canonical main work-tree"
guarantee now lives in juggle_watchdog_singleton.canonical_repo_path(), which the
flock launcher (start_watchdog_detached) uses — these tests pin that seam.
"""
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _fake_worktree_list(primary: str, *others: str):
    """Build a fake `git worktree list --porcelain` stdout: primary entry first."""
    lines = [f"worktree {primary}", "HEAD 0" * 1, "branch refs/heads/main", ""]
    for o in others:
        lines += [f"worktree {o}", "HEAD 1" * 1, "branch refs/heads/cyc_x", ""]
    return "\n".join(lines)


def test_canonical_repo_path_uses_primary_worktree(monkeypatch):
    """canonical_repo_path resolves to the FIRST (primary) worktree entry — the
    main checkout — even when invoked from a linked agent worktree."""
    import juggle_watchdog_singleton as ws

    main = "/Users/x/github/juggle"
    agent_wt = "/tmp/juggle-juggle-AB"

    def fake_run(cmd, **kwargs):
        out = _fake_worktree_list(main, agent_wt)
        return MagicMock(returncode=0, stdout=out)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert ws.canonical_repo_path(start=agent_wt) == main


def test_canonical_repo_path_not_under_tmp_worktree(monkeypatch):
    """Even when started from a /tmp agent worktree, the resolved repo path must
    never be the transient worktree (which vanishes post-integrate)."""
    import juggle_watchdog_singleton as ws

    main = "/Users/x/github/juggle"

    def fake_run(cmd, **kwargs):
        return MagicMock(returncode=0, stdout=_fake_worktree_list(main, "/tmp/juggle-juggle-CP"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    resolved = ws.canonical_repo_path(start="/tmp/juggle-juggle-CP")
    assert "juggle-juggle" not in resolved
