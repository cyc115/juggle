"""Regression pin — df-atomic-dispatch (incident 2026-07-03, defect 1).

Symptom: topic `T-rail-color-palette` wedged in `running` with NO worktree and
NO live agent after a worktree establishment failure during a concurrent
dispatch burst. `build_worktree_context` silently returned "" for a coder that
had no isolated worktree, so the dispatch "succeeded" and the topic transitioned
to `running` — a permanent wedge the stale sweep can't reclaim (it's thread-bound)
and `graph reconcile` protects (`running` is a protected state).

Invariant: a coder topic must NEVER reach an in-flight state (dispatching/running)
without a confirmed on-disk worktree. On worktree failure the dispatch must
refuse loudly so the tick rolls the topic back to a claimable state and records
the failure.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as tp  # noqa: E402
from juggle_graph_status import IN_FLIGHT_STATES  # noqa: E402
import juggle_graph_dispatch as gd  # noqa: E402


def _repo(tmp_path: Path) -> str:
    d = tmp_path / "repo"
    d.mkdir()

    def git(*a):
        subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t.t")
    git("config", "user.name", "T")
    (d / "f.txt").write_text("x\n")
    git("add", ".")
    git("commit", "-qm", "base")
    return str(d)


def _mk_topic(db, tid, project="INBOX"):
    tp.create_topic(db, topic_id=tid, project_id=project, title=f"Topic {tid}")
    nid = f"{tid}-k0"
    g.create_task(db, task_id=nid, project_id=project, title=nid, prompt="p")
    g.set_task_topic(db, nid, tid)
    tp.recompute_topic_ready(db, project)


def test_worktree_failure_rolls_topic_back_to_claimable(tmp_path, monkeypatch):
    """RED on pre-fix code: the topic ended in `running` with a bound thread and
    no worktree. Post-fix: the coder dispatch refuses loudly (no worktree
    established), the tick rolls the topic back to a claimable state, unbinds the
    thread, and files a dispatch-failure action item."""
    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    repo = _repo(tmp_path)

    # Mock tmux so agent spawn/send succeed without real panes.
    monkeypatch.setenv("JUGGLE_TMUX_MOCK_PANE", "%mock")
    monkeypatch.setenv("JUGGLE_TMUX_MOCK_SEND", "1")
    monkeypatch.setenv("JUGGLE_CLAUDE_JSON_PATH", str(tmp_path / ".claude.json"))
    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: repo)

    # Simulate a worktree establishment failure: no source repo resolves, so a
    # coder cannot get an isolated worktree (the burst-race symptom).
    import juggle_repo_binding as rb
    monkeypatch.setattr(rb, "resolve_worktree_base", lambda *a, **k: "")

    _mk_topic(db, "T-x")
    gd.graph_tick(db)

    topic = tp.get_topic(db, "T-x")
    assert topic["state"] not in IN_FLIGHT_STATES, (
        f"topic wedged in-flight ({topic['state']!r}) after worktree failure"
    )
    assert topic["state"] in ("open", "ready"), (
        f"topic must be claimable, got {topic['state']!r}"
    )
    assert topic["thread_id"] is None, "topic must be unbound after rollback"

    items = db.get_open_action_items()
    assert any("dispatch failed" in i["message"] and "T-x" in i["message"] for i in items), (
        "worktree-failure dispatch must record a failure action item"
    )
