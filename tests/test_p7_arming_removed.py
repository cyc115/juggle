"""P7 regression tests — arming concept removed.

Spec: specs/2026-06-18-unified-topic-graph.md §P7
Tests pin every invariant that P7 changes so no future refactor can
silently re-introduce arming semantics.
"""
from __future__ import annotations

import os
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as t  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    d._set_session_key_external("session_id", "sessP7")
    return d


def _project(db, pid="PROJ") -> str:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at,last_active) "
            "VALUES(?,?,?,?,?)",
            (pid, f"Project {pid}", "active", now, now),
        )
        conn.commit()
    return pid


# ---------------------------------------------------------------------------
# P7-1: --force-task flag is gone from the arg parser
# ---------------------------------------------------------------------------


def test_force_task_flag_removed():
    """--force-task must no longer be a recognized argument on send-task."""
    import subprocess

    r = subprocess.run(
        [
            sys.executable,
            os.path.join(os.path.dirname(__file__), "..", "src", "juggle_cli.py"),
            "agent",
            "send-task",
            "--force-task",
            "dummy-agent",
            "/dev/null",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "_JUGGLE_TEST_DB": "/tmp/does-not-exist.db"},
    )
    # argparse exits 2 for unrecognized argument
    assert r.returncode == 2, f"expected exit 2, got {r.returncode}: {r.stderr}"
    assert "unrecognized" in r.stderr.lower() or "error" in r.stderr.lower()


def test_force_node_alias_also_removed():
    """--force-node (deprecated alias of --force-task) must also be gone."""
    import subprocess

    r = subprocess.run(
        [
            sys.executable,
            os.path.join(os.path.dirname(__file__), "..", "src", "juggle_cli.py"),
            "agent",
            "send-task",
            "--force-node",
            "dummy-agent",
            "/dev/null",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "_JUGGLE_TEST_DB": "/tmp/does-not-exist.db"},
    )
    assert r.returncode == 2


# ---------------------------------------------------------------------------
# P7-2: R8 armed-project guard removed — check_task_guard no longer checks arming
# ---------------------------------------------------------------------------


def test_check_task_guard_no_longer_checks_armed_project(db):
    """R8 guard deleted: unbound thread in a project — even with old armed key set —
    must pass. We write the key directly to simulate a legacy DB."""
    from juggle_autopilot_state import ARMED_PROJECT_KEY
    from juggle_cmd_agents_graph import check_task_guard

    pid = _project(db)
    # Simulate old armed state by writing directly to settings (arm_project removed)
    db.set_setting(ARMED_PROJECT_KEY, pid)
    # Create an unbound thread for that project
    thread_id = db.create_thread(topic="manual work", session_id="sessP7")
    db.update_thread(thread_id, project_id=pid)

    # After P7 the guard must return None (no refusal) — R8 is gone
    result = check_task_guard(db, thread_id)
    assert result is None, f"R8 armed-project guard must be gone, got: {result!r}"


# ---------------------------------------------------------------------------
# P7-3: Protected-state guard (correctness) is intact — not bypassed
# ---------------------------------------------------------------------------


def test_protected_state_guard_refuses_running_task(db):
    """Thread bound to a running task must still be refused (correctness guard)."""
    from juggle_cmd_agents_graph import check_task_guard

    pid = _project(db)
    t.create_topic(db, topic_id="T-run", project_id=pid, title="Running topic")
    thread_id = db.create_thread(topic="topic work", session_id="sessP7")
    t.set_topic_thread(db, "T-run", thread_id)
    for ev in ("deps_ready", "claim", "dispatch"):  # → running
        t.topic_transition(db, "T-run", ev)

    result = check_task_guard(db, thread_id)
    assert result is not None, "protected-state guard must refuse a running topic thread"
    assert "running" in result.lower() or "tick" in result.lower()


def test_protected_state_guard_refuses_integrating_task(db):
    """Thread bound to an integrating topic must still be refused."""
    from juggle_cmd_agents_graph import check_task_guard

    pid = _project(db)
    t.create_topic(db, topic_id="T-int", project_id=pid, title="Integrating topic")
    thread_id = db.create_thread(topic="topic work", session_id="sessP7")
    db.update_thread(thread_id, worktree_branch="cyc_test", main_repo_path="/tmp")
    t.set_topic_thread(db, "T-int", thread_id)
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start"):  # → integrating
        t.topic_transition(db, "T-int", ev)

    result = check_task_guard(db, thread_id)
    assert result is not None, "correctness guard must refuse integrating topic thread"


def test_protected_state_guard_allows_pending_task(db):
    """Thread bound to a pending topic must be allowed (not tick-protected yet)."""
    from juggle_cmd_agents_graph import check_task_guard

    pid = _project(db)
    t.create_topic(db, topic_id="T-pend", project_id=pid, title="Pending topic")
    thread_id = db.create_thread(topic="topic work", session_id="sessP7")
    t.set_topic_thread(db, "T-pend", thread_id)
    # Don't drive it — topic stays pending

    result = check_task_guard(db, thread_id)
    assert result is None, f"pending topic must be allowed, got: {result!r}"


# ---------------------------------------------------------------------------
# P7-4: graph_tick processes ALL projects (no arming filter)
# ---------------------------------------------------------------------------


def test_graph_tick_processes_multiple_projects_without_arming(db):
    """graph_tick must process ready tasks across all projects, not just armed ones."""
    from juggle_graph_dispatch import graph_tick

    pid1 = _project(db, "PROJ1")
    pid2 = _project(db, "PROJ2")
    g.create_task(db, task_id="t1", project_id=pid1, title="Task1", prompt="do t1")
    g.create_task(db, task_id="t2", project_id=pid2, title="Task2", prompt="do t2")
    g.recompute_ready(db, pid1)
    g.recompute_ready(db, pid2)

    dispatched: list[str] = []

    def fake_dispatch(db_, thread_id, prompt, task):
        dispatched.append(task["id"])

    stats = graph_tick(db, dispatch_fn=fake_dispatch)
    assert "t1" in dispatched, "task from PROJ1 must be dispatched (no arming filter)"
    assert "t2" in dispatched, "task from PROJ2 must be dispatched (no arming filter)"


def test_graph_tick_empty_projects_returns_empty_stats(db):
    """graph_tick with no projects must return empty stats (not crash or skip early)."""
    from juggle_graph_dispatch import graph_tick

    dispatched: list = []

    def fake_dispatch(db_, thread_id, prompt, task):
        dispatched.append(task["id"])

    stats = graph_tick(db, dispatch_fn=fake_dispatch)
    assert stats["dispatched"] == []
    assert stats["errors"] == []


# ---------------------------------------------------------------------------
# P7-5: conversation nodes and legacy threads NOT dispatched by tick
# ---------------------------------------------------------------------------


def test_graph_tick_does_not_dispatch_conversation_node(db):
    """Conversation threads (kind=conversation / plain threads) must never be
    auto-dispatched by graph_tick — only task/research nodes are dispatchable."""
    from juggle_graph_dispatch import graph_tick

    pid = _project(db, "PROJ3")
    # Create a plain conversation thread (simulates a conversation node)
    thread_id = db.create_thread(topic="planning chat", session_id="sessP7")
    db.update_thread(thread_id, project_id=pid)
    # Do NOT create any graph_tasks for this project

    dispatched: list = []

    def fake_dispatch(db_, tid, prompt, task):
        dispatched.append(tid)

    graph_tick(db, dispatch_fn=fake_dispatch)
    assert thread_id not in dispatched, "conversation thread must not be auto-dispatched"
    assert dispatched == [], "no tasks → nothing dispatched"


def test_graph_tick_does_not_dispatch_task_with_unmet_deps(db):
    """A task whose dependencies are not met must not be dispatched."""
    from juggle_graph_dispatch import graph_tick

    pid = _project(db, "PROJ4")
    g.create_task(db, task_id="dep", project_id=pid, title="Dep", prompt="do dep")
    g.create_task(db, task_id="child", project_id=pid, title="Child", prompt="do child")
    g.replace_edges(db, "child", ["dep"])  # child depends on dep
    g.recompute_ready(db, pid)

    dispatched: list = []

    def fake_dispatch(db_, tid, prompt, task):
        dispatched.append(task["id"])

    graph_tick(db, dispatch_fn=fake_dispatch)
    # Only 'dep' has no deps — 'child' must NOT be dispatched
    assert "child" not in dispatched


# ---------------------------------------------------------------------------
# P7-6: cockpit DAG loads ALL projects — not gated on armed set
# ---------------------------------------------------------------------------


def test_cockpit_dag_loads_project_without_arming(db):
    """load_graph_dags must return DAGs for ALL projects, not just armed ones."""
    from juggle_cockpit_graph_dag import load_graph_dags

    pid = _project(db, "PROJ5")
    g.create_task(db, task_id="x1", project_id=pid, title="X", prompt="do x")

    # Explicitly ensure the project is NOT armed (empty/null setting)
    db.set_setting("autopilot_armed_project", None)

    with db._connect() as conn:
        dags = load_graph_dags(conn)

    dag_pids = [d.project_id for d in dags]
    assert pid in dag_pids, "project must appear in DAGs without being armed"


def test_cockpit_dag_loads_all_projects(db):
    """load_graph_dags returns dags for every project that has tasks."""
    from juggle_cockpit_graph_dag import load_graph_dags

    pid1 = _project(db, "PA")
    pid2 = _project(db, "PB")
    g.create_task(db, task_id="a1", project_id=pid1, title="A", prompt="do a")
    g.create_task(db, task_id="b1", project_id=pid2, title="B", prompt="do b")

    with db._connect() as conn:
        dags = load_graph_dags(conn)

    dag_pids = {d.project_id for d in dags}
    assert pid1 in dag_pids
    assert pid2 in dag_pids


# ---------------------------------------------------------------------------
# P7-7: cockpit graph panel renders with zero armed projects — no "arm" message
# ---------------------------------------------------------------------------


def test_cockpit_multi_graph_panel_no_armed_message(db):
    """build_multi_graph_panel with empty dags must NOT print the 'no armed graph'
    message — the arming concept is gone."""
    from juggle_cockpit_graph_panel import build_multi_graph_panel

    panel = build_multi_graph_panel(
        dags=[],
        selection=0,
        unread=0,
        width=80,
        height=20,
        pan_offset=0,
    )
    rendered = str(panel.renderable)
    assert "armed" not in rendered.lower(), (
        "the word 'armed' must not appear in the panel — arming concept is gone"
    )
    assert "toggle-autopilot" not in rendered.lower()


def test_cockpit_graph_panel_no_armed_message_single(db):
    """build_graph_panel with project_id=None must NOT show 'no armed graph'."""
    from juggle_cockpit_graph_panel import build_graph_panel

    panel = build_graph_panel(
        project_id=None,
        tasks=[],
        edges=[],
        selection=0,
        unread=0,
        width=80,
        height=20,
        pan_offset=0,
    )
    rendered = str(panel.renderable)
    assert "armed" not in rendered.lower()


# ---------------------------------------------------------------------------
# P7-8: autopilot arm/disarm subcommands neutralized
# ---------------------------------------------------------------------------


def test_autopilot_arm_subcommand_restored(tmp_path):
    """REGRESSION PIN (rewritten 2026-06-30, was P7 'arm neutralized'):
    per-project arm/disarm is RESTORED (user-approved, default-armed exclusion
    set). `juggle autopilot arm <pid>` exits 0 and re-arms the project."""
    import subprocess

    test_db = str(tmp_path / "p7restore.db")
    r = subprocess.run(
        [
            sys.executable,
            os.path.join(os.path.dirname(__file__), "..", "src", "juggle_cli.py"),
            "autopilot",
            "arm",
            "INBOX",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "_JUGGLE_TEST_DB": test_db},
    )
    assert r.returncode == 0, (
        f"'juggle autopilot arm' must exit 0 after the 2026-06-30 restore, "
        f"got returncode={r.returncode}, stderr={r.stderr}"
    )
    assert "ARMED" in r.stdout


def test_autopilot_disarm_subcommand_restored(tmp_path):
    """REGRESSION PIN (rewritten 2026-06-30, was P7 'disarm neutralized'):
    `juggle autopilot disarm <pid>` exits 0 and records the project in the
    disarmed exclusion set."""
    import subprocess

    test_db = str(tmp_path / "p7restore.db")
    r = subprocess.run(
        [
            sys.executable,
            os.path.join(os.path.dirname(__file__), "..", "src", "juggle_cli.py"),
            "autopilot",
            "disarm",
            "INBOX",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "_JUGGLE_TEST_DB": test_db},
    )
    assert r.returncode == 0, (
        f"'juggle autopilot disarm' must exit 0 after the 2026-06-30 restore, "
        f"got returncode={r.returncode}, stderr={r.stderr}"
    )
    assert "DISARMED INBOX" in r.stdout
