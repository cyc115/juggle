"""Tests for juggle_graph_status — display aggregates + hook injection (Phase 4).

Covers: pure state-count aggregation, the cockpit progress string (DA m2),
and the UserPromptSubmit graph-status injection with its HARD 500-char
budget and deterministic truncation (DA m4), plus the armed-project LLM
directive carve-out (DA B5).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
import juggle_graph_status as gs  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


def _mk(db, task_id, state="open", title=None, thread_id=None):
    g.create_task(
        db,
        task_id=task_id,
        project_id="INBOX",
        title=title or f"Task {task_id}",
        prompt=f"do {task_id}",
    )
    if state != "open":
        with db._connect() as conn:  # test seam: force a display state directly
            # P8 c4-write-cut: nodes is the authoritative task store the display
            # aggregates read; force the state there (graph_tasks is no longer read).
            conn.execute(
                "UPDATE nodes SET state=? WHERE id=? AND kind='task'",
                (state, task_id),
            )
            conn.commit()


# ── counts ────────────────────────────────────────────────────────────────────


def test_counts_from_states_aggregates():
    c = gs.counts_from_states(
        ["verified"] * 3
        + ["open"] * 8
        + ["failed-verify"]
        + ["ready", "ready"]
        + ["running"]
    )
    assert c["total"] == 15
    assert c["verified"] == 3
    assert c["failed"] == 1
    assert c["ready"] == 2
    assert c["running"] == 1
    assert c["open"] == 8


def test_counts_running_includes_dispatching_and_integrating():
    c = gs.counts_from_states(["dispatching", "integrating", "running"])
    assert c["running"] == 3


def test_graph_counts_none_when_no_tasks(db):
    assert gs.graph_counts(db, "INBOX") is None


def test_graph_counts_none_on_pre_migration_db(tmp_path):
    """Pre-graph_tasks DBs must degrade to None, never raise."""
    import sqlite3

    class Stub:
        def _connect(self):
            return sqlite3.connect(str(tmp_path / "empty.db"))

    assert gs.graph_counts(Stub(), "INBOX") is None


def test_graph_counts_reads_db(db):
    _mk(db, "a", "verified")
    _mk(db, "b", "ready")
    _mk(db, "c")
    c = gs.graph_counts(db, "INBOX")
    assert c["total"] == 3 and c["verified"] == 1 and c["ready"] == 1


def test_graph_counts_reads_from_nodes_not_graph_tasks(db):
    """2026-06-29 P8 c4-write-cut: graph_counts must aggregate kind='task' nodes
    (the authoritative store), not the legacy graph_tasks table the write-cut
    stops populating. RED on the pre-flip code (it SELECTs FROM graph_tasks, so a
    nodes-only task is invisible and graph_counts returns None)."""
    now = "2026-06-29 00:00"
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO nodes (id, kind, title, objective, state, project_id, "
            "created_at, updated_at) "
            "VALUES ('x','task','X','do','verified','INBOX',?,?)",
            (now, now),
        )
        conn.commit()
    c = gs.graph_counts(db, "INBOX")
    assert c is not None and c["total"] == 1 and c["verified"] == 1


# ── progress string (cockpit row, DA m2) ──────────────────────────────────────


def test_format_progress_spec_example():
    c = gs.counts_from_states(
        ["verified"] * 3 + ["open"] * 8 + ["failed-exec"] + ["ready"] * 2
    )
    assert gs.format_progress(c) == "3/14 done, 1 failed, 2 ready"


def test_format_progress_omits_zero_segments():
    c = gs.counts_from_states(["verified", "open"])
    assert gs.format_progress(c) == "1/2 done"


def test_format_progress_blocked_and_running():
    c = gs.counts_from_states(["blocked-failed", "running", "verified"])
    assert "1 blocked" in gs.format_progress(c)
    assert "1 running" in gs.format_progress(c)


# ── hook injection (DA m4: HARD 500-char budget) ──────────────────────────────


def test_injection_contains_counts_and_titles(db):
    _mk(db, "a", "verified")
    _mk(db, "b", "ready", title="Build the parser")
    _mk(db, "c", "running", title="Wire the CLI")
    text = gs.build_graph_injection(db, "INBOX")
    assert "1/3 done" in text
    assert "Build the parser" in text
    assert "Wire the CLI" in text


def test_injection_hard_500_char_budget(db):
    """2026-06-10 DA m4: injection must NEVER exceed 500 chars, regardless of
    how many ready/running tasks exist or how long their titles are."""
    for i in range(30):
        _mk(db, f"n{i:02d}", "ready", title="An extremely long task title " * 8)
    text = gs.build_graph_injection(db, "INBOX")
    assert len(text) <= 500
    assert text  # still says something


def test_injection_truncation_deterministic(db):
    for i in range(30):
        _mk(db, f"n{i:02d}", "ready", title="Another very long task title " * 8)
    a = gs.build_graph_injection(db, "INBOX")
    b = gs.build_graph_injection(db, "INBOX")
    assert a == b


def test_injection_no_graph_loaded(db):
    text = gs.build_graph_injection(db, "INBOX")
    assert "no graph" in text.lower()
    assert len(text) <= 500


# ── armed directive carve-out (DA B5) ─────────────────────────────────────────


@pytest.fixture
def armed_hook_env(db, tmp_path, monkeypatch):
    import juggle_hooks_config
    import juggle_graph_dispatch as gd

    flag = tmp_path / "autopilot"
    flag.touch()
    monkeypatch.setattr(juggle_hooks_config, "AUTOPILOT_FLAG", flag)
    monkeypatch.setattr(juggle_hooks_config, "get_db", lambda: db)
    db.set_setting(gd.ARMED_PROJECT_KEY, "INBOX")
    return db


def test_autopilot_context_includes_carveout_when_armed(armed_hook_env):
    from juggle_hooks_autopilot import autopilot_context

    text = autopilot_context()
    assert "AUTOPILOT MODE: ON" in text
    assert "tick-owned" in text
    assert "NEVER dispatch them manually" in text
    assert "report status only" in text


def test_autopilot_context_includes_graph_status_when_armed(armed_hook_env):
    db = armed_hook_env
    _mk(db, "a", "verified")
    _mk(db, "b", "ready", title="Build the parser")
    from juggle_hooks_autopilot import autopilot_context

    text = autopilot_context()
    assert "1/2 done" in text
    assert "Build the parser" in text


def test_autopilot_context_no_carveout_when_disarmed(db, tmp_path, monkeypatch):
    """Default-armed (2026-06-30): disarming EVERY active project yields an empty
    derived armed set, so the directive injects with no per-project carve-out."""
    import juggle_hooks_config
    import juggle_autopilot_state as st

    # init_db seeds INBOX; disarm the whole active set to empty the armed set.
    st.disarm_all(db, [p["id"] for p in db.list_projects()])

    flag = tmp_path / "autopilot"
    flag.touch()
    monkeypatch.setattr(juggle_hooks_config, "AUTOPILOT_FLAG", flag)
    monkeypatch.setattr(juggle_hooks_config, "get_db", lambda: db)
    from juggle_hooks_autopilot import autopilot_context

    text = autopilot_context()
    assert "AUTOPILOT MODE: ON" in text
    assert "tick-owned" not in text


def test_autopilot_context_armed_db_error_degrades_to_directive(tmp_path, monkeypatch):
    import juggle_hooks_config

    flag = tmp_path / "autopilot"
    flag.touch()
    monkeypatch.setattr(juggle_hooks_config, "AUTOPILOT_FLAG", flag)

    def _boom():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(juggle_hooks_config, "get_db", _boom)
    from juggle_hooks_autopilot import autopilot_context

    text = autopilot_context()
    assert "AUTOPILOT MODE: ON" in text  # directive survives a broken DB
