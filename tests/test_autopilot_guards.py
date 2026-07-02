"""Regression tests for the 2026-06-13 autopilot graph-machinery guards (G1–G6).

Incident: docs/incidents/2026-06-13-autopilot-shared-db-corruption.md

Each test pins one durable invariant so the incident's defect chain cannot
recur. All tests use isolated temp DBs (tmp_path) — never the shared prod DB.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as t  # noqa: E402
from dbops import graph_guards as gg  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    d._set_session_key_external("session_id", "sessA")
    return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project(db, pid="proj1") -> str:
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at,last_active) "
            "VALUES(?,?,?,?,?)",
            (pid, "Proj One", "active", _now(), _now()),
        )
        conn.commit()
    return pid


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _make_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


# ---------------------------------------------------------------------------
# G1 — verified ⟺ merged to main
# ---------------------------------------------------------------------------

def _bind_topic_to_branch(db, pid, topic_id, repo: Path, branch: str):
    """Create a topic bound to a thread whose worktree_branch = branch."""
    thread_id = db.create_thread(topic="work", session_id="sessA")
    db.update_thread(thread_id, worktree_branch=branch,
                     main_repo_path=str(repo))
    t.create_topic(db, topic_id=topic_id, project_id=pid, title="Feat")
    t.set_topic_thread(db, topic_id, thread_id)
    # Drive the topic up to 'integrating' (the pre-verified state).
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start"):
        t.topic_transition(db, topic_id, ev)
    return thread_id


def test_g1_verify_refused_when_branch_unmerged(db, tmp_path):
    pid = _project(db)
    repo = _make_repo(tmp_path, "repo1")
    # Unmerged feature branch with a commit NOT on main.
    _git(repo, "checkout", "-q", "-b", "cyc_XU")
    (repo / "f.txt").write_text("feature\n")
    _git(repo, "commit", "-aqm", "wip")
    _git(repo, "checkout", "-q", "main")
    _bind_topic_to_branch(db, pid, "T-feat", repo, "cyc_XU")

    with pytest.raises(t.UnmergedVerifyRefused):
        t.topic_transition(db, "T-feat", "integrate_ok")
    assert t.get_topic(db, "T-feat")["state"] == "integrating"


def test_g1_verify_allowed_when_branch_merged(db, tmp_path):
    pid = _project(db)
    repo = _make_repo(tmp_path, "repo2")
    _git(repo, "checkout", "-q", "-b", "cyc_XU")
    (repo / "f.txt").write_text("feature\n")
    _git(repo, "commit", "-aqm", "wip")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "cyc_XU")
    _bind_topic_to_branch(db, pid, "T-feat", repo, "cyc_XU")
    # T-verified-merged-sha single gate: record the merged branch tip (now an
    # ancestor of main) as the topic's merged_sha.
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "cyc_XU"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    t.set_topic_merged_sha(db, "T-feat", sha)

    assert t.topic_transition(db, "T-feat", "integrate_ok") == "verified"


# ---------------------------------------------------------------------------
# G2 — agents must not migrate the shared prod DB
# ---------------------------------------------------------------------------

def test_g2_refuses_shared_db_migration_from_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(gg, "SHARED_PROD_DB",
                        (tmp_path / "shared.db").resolve())
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    with pytest.raises(gg.SharedDBMigrationRefused):
        gg.assert_migration_allowed(str(tmp_path / "shared.db"))


def test_g2_allows_isolated_db_from_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(gg, "SHARED_PROD_DB",
                        (tmp_path / "shared.db").resolve())
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    # An isolated agent DB is fine to migrate.
    gg.assert_migration_allowed(str(tmp_path / "isolated.db"))


def test_g2_allows_shared_db_from_orchestrator(monkeypatch, tmp_path):
    monkeypatch.setattr(gg, "SHARED_PROD_DB",
                        (tmp_path / "shared.db").resolve())
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    monkeypatch.delenv("JUGGLE_AGENT_WORKTREE", raising=False)
    # Neutralize the cwd-based worktree signal: is_agent_context() also returns
    # True when cwd contains "juggle-juggle-" (defence-in-depth). Tests must
    # chdir to a neutral path so the test is deterministic regardless of where
    # pytest is invoked (e.g. from inside a juggle integrate worktree).
    monkeypatch.chdir(tmp_path)
    gg.assert_migration_allowed(str(tmp_path / "shared.db"))


def test_g2_orchestrator_marker_wins_over_cwd_and_agent_flag(monkeypatch, tmp_path):
    """2026-06-13 watchdog-g2-crashloop: JUGGLE_ORCHESTRATOR=1 must override
    cwd heuristic AND JUGGLE_IS_AGENT=1 — watchdog spawned from worktree cwd."""
    monkeypatch.setattr(gg, "SHARED_PROD_DB", (tmp_path / "shared.db").resolve())
    # Simulate worst case: agent env flag set AND cwd is a juggle worktree
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")
    monkeypatch.chdir(tmp_path / "..")  # chdir to non-worktree parent first
    # Patch cwd to look like a worktree even with chdir neutralized
    monkeypatch.setattr(gg.Path, "cwd", classmethod(lambda cls: Path("/tmp/juggle-juggle-ABC")))
    # With orchestrator marker: must NOT be agent context
    assert gg.is_agent_context() is False


def test_g2_orchestrator_marker_allows_shared_db_migration(monkeypatch, tmp_path):
    """2026-06-13 watchdog-g2-crashloop: assert_migration_allowed must not raise
    for shared DB when JUGGLE_ORCHESTRATOR=1, even with agent env + worktree cwd."""
    monkeypatch.setattr(gg, "SHARED_PROD_DB", (tmp_path / "shared.db").resolve())
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")
    monkeypatch.setattr(gg.Path, "cwd", classmethod(lambda cls: Path("/tmp/juggle-juggle-XYZ")))
    # Must not raise — orchestrator is authoritative
    gg.assert_migration_allowed(str(tmp_path / "shared.db"))


def test_g2_agent_refusal_still_enforced_without_orchestrator_marker(monkeypatch, tmp_path):
    """2026-06-13 watchdog-g2-crashloop: removing JUGGLE_ORCHESTRATOR must keep
    the agent-refusal guard active (no regression)."""
    monkeypatch.setattr(gg, "SHARED_PROD_DB", (tmp_path / "shared.db").resolve())
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    monkeypatch.delenv("JUGGLE_ORCHESTRATOR", raising=False)
    with pytest.raises(gg.SharedDBMigrationRefused):
        gg.assert_migration_allowed(str(tmp_path / "shared.db"))


# ---------------------------------------------------------------------------
# G2 follow-up — 2026-07-01 blocker item 5079: assert_migration_allowed
# over-blocked EVERY init_db(init=True) call (mark-task, add-task, runs,
# projects, selfheal, ...) from an agent/worktree context, not just real
# migration runners. Plain state writes must work; only doctor/db-init keep
# the hard refusal.
# ---------------------------------------------------------------------------

def test_agent_plain_write_skips_migration_on_shared_db(monkeypatch, tmp_path):
    """graph mark-task (get_db(init=True) -> init_db()) must NOT be refused
    from an agent/worktree context — it is a plain state write, not a
    migration. Tables are still created; init_db() just skips the migration
    runner instead of raising."""
    shared = (tmp_path / "shared.db").resolve()
    monkeypatch.setattr(gg, "SHARED_PROD_DB", shared)
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    monkeypatch.setattr(
        gg.Path, "cwd", classmethod(lambda cls: Path("/tmp/juggle-juggle-VB"))
    )

    d = JuggleDB(db_path=str(shared))
    d.init_db()  # require_migrate defaults False — must not raise

    with d._connect() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "threads" in tables and "graph_tasks" in tables


def test_agent_require_migrate_still_refused_on_shared_db(monkeypatch, tmp_path):
    """doctor / db init (require_migrate=True) must still refuse to touch the
    shared prod DB from an agent/worktree context — the actual migration
    runner never runs from an agent."""
    shared = (tmp_path / "shared.db").resolve()
    monkeypatch.setattr(gg, "SHARED_PROD_DB", shared)
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    monkeypatch.setattr(
        gg.Path, "cwd", classmethod(lambda cls: Path("/tmp/juggle-juggle-VB"))
    )

    d = JuggleDB(db_path=str(shared))
    with pytest.raises(gg.SharedDBMigrationRefused):
        d.init_db(require_migrate=True)


def test_agent_init_db_paths_consistent_regardless_of_init_flag(monkeypatch, tmp_path):
    """2026-07-01 heuristic-inconsistency finding: one agent's completion
    ('agent complete' -> get_db(), no init) never touched
    assert_migration_allowed, while a sibling agent's 'graph mark-task' ->
    get_db(init=True) -> init_db() did and was refused — same agent context,
    different outcome purely because of which CLI command happened to pass
    init=True. Both plain-write shapes must now behave identically: neither
    raises when require_migrate is left at its default."""
    shared = (tmp_path / "shared.db").resolve()
    monkeypatch.setattr(gg, "SHARED_PROD_DB", shared)
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    monkeypatch.setattr(
        gg.Path, "cwd", classmethod(lambda cls: Path("/tmp/juggle-juggle-VB"))
    )

    # "agent complete" shape: get_db() with no init — never called init_db().
    no_init_db = JuggleDB(db_path=str(shared))  # constructing alone never raised

    # "graph mark-task" shape: get_db(init=True) -> init_db().
    mark_task_db = JuggleDB(db_path=str(shared))
    mark_task_db.init_db()  # must not raise — same outcome as the no-init shape

    assert no_init_db.db_path == mark_task_db.db_path == shared


# ---------------------------------------------------------------------------
# G3 — claimable invariant (no empty/blocked topic promoted to ready)
# ---------------------------------------------------------------------------

def test_g3_empty_topic_never_eligible(db):
    pid = _project(db)
    t.create_topic(db, topic_id="T-empty", project_id=pid, title="Empty")
    # No tasks at all → must not be eligible/claimable.
    assert "T-empty" not in t.topic_ready_eligible(db, pid)
    assert "T-empty" not in t.recompute_topic_ready(db, pid)


def test_g3_all_terminal_topic_never_eligible(db):
    pid = _project(db)
    t.create_topic(db, topic_id="T-done", project_id=pid, title="Done")
    g.create_task(db, task_id="n1", project_id=pid, title="x", prompt="x")
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET parent_id='T-done', state='verified' "
                     "WHERE id='n1'")
        conn.commit()
    assert "T-done" not in t.topic_ready_eligible(db, pid)


def test_g3_topic_with_pending_task_is_eligible(db):
    pid = _project(db)
    t.create_topic(db, topic_id="T-go", project_id=pid, title="Go")
    g.create_task(db, task_id="n1", project_id=pid, title="x", prompt="x")
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET parent_id='T-go' WHERE id='n1'")
        conn.commit()
    assert "T-go" in t.topic_ready_eligible(db, pid)


# ---------------------------------------------------------------------------
# G4a — reconcile must not demote a topic with a live bound agent
# ---------------------------------------------------------------------------

def test_g4a_reconcile_keeps_running_with_live_agent(db, monkeypatch):
    pid = _project(db)
    thread_id = db.create_thread(topic="w", session_id="sessA")
    t.create_topic(db, topic_id="T-run", project_id=pid, title="Run")
    t.set_topic_thread(db, "T-run", thread_id)
    for ev in ("deps_ready", "claim", "dispatch"):  # → running
        t.topic_transition(db, "T-run", ev)
    # A pending member task would otherwise derive target 'open' (demote).
    g.create_task(db, task_id="n1", project_id=pid, title="x", prompt="x")
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET parent_id='T-run' WHERE id='n1'")
        conn.commit()
    # Live, healthy (busy) agent bound to the thread.
    monkeypatch.setattr(db, "get_agent_by_thread",
                        lambda tid: {"id": "ag1", "status": "busy"})
    assert t.reconcile_topic_state(db, "T-run") == "running"


def test_g4a_reconcile_demotes_when_no_agent(db, monkeypatch):
    pid = _project(db)
    thread_id = db.create_thread(topic="w", session_id="sessA")
    t.create_topic(db, topic_id="T-run", project_id=pid, title="Run")
    t.set_topic_thread(db, "T-run", thread_id)
    for ev in ("deps_ready", "claim", "dispatch"):
        t.topic_transition(db, "T-run", ev)
    g.create_task(db, task_id="n1", project_id=pid, title="x", prompt="x")
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET parent_id='T-run' WHERE id='n1'")
        conn.commit()
    monkeypatch.setattr(db, "get_agent_by_thread", lambda tid: None)
    assert t.reconcile_topic_state(db, "T-run") == "open"


# ---------------------------------------------------------------------------
# G4b — decommission-agent never triggers topic verification
# ---------------------------------------------------------------------------

def test_g4b_decommission_does_not_verify_topic(db, tmp_path, monkeypatch):
    pid = _project(db)
    repo = _make_repo(tmp_path, "repo4b")
    _git(repo, "checkout", "-q", "-b", "cyc_XU")
    (repo / "f.txt").write_text("wip\n")
    _git(repo, "commit", "-aqm", "wip")
    _git(repo, "checkout", "-q", "main")  # NOT merged
    thread_id = _bind_topic_to_branch(db, pid, "T-feat", repo, "cyc_XU")
    # Register a busy agent bound to the thread, then decommission it.
    agent_id = db.create_agent(role="coder", pane_id="%1", harness="claude")
    db.update_agent(agent_id, assigned_thread=thread_id, status="busy")

    from juggle_tmux import JuggleTmuxManager
    monkeypatch.setattr(JuggleTmuxManager, "kill_pane", lambda self, p: None)
    JuggleTmuxManager().decommission_agent(db, agent_id)

    # Topic state must be unchanged (still integrating, never 'verified').
    assert t.get_topic(db, "T-feat")["state"] == "integrating"


# ---------------------------------------------------------------------------
# G5 — atomic add-task auto-creates its topic in one transaction
# ---------------------------------------------------------------------------

def test_g5_add_task_creates_topic_atomically(db):
    pid = _project(db)
    from juggle_graph_add import add_task

    # Topic does not exist beforehand.
    assert t.get_topic(db, "T-n1") is None
    add_task(db, pid, task_id="n1", title="First", prompt="do it",
             deps=[], required_by=[], verify_cmd=None,
             topic_id="T-n1", auto_create_topic=True)
    # Topic now exists AND already owns the task (no empty-topic window).
    topic = t.get_topic(db, "T-n1")
    assert topic is not None
    members = t.list_topic_tasks(db, "T-n1")
    assert [m["id"] for m in members] == ["n1"]


# ---------------------------------------------------------------------------
# G6 — cockpit graph panel populates user_label from the bound thread
# ---------------------------------------------------------------------------

def test_g6_graph_panel_uses_thread_user_label(db):
    pid = _project(db)
    thread_id = db.create_thread(topic="w", session_id="sessA")
    db.update_thread(thread_id, user_label="XW")
    t.create_topic(db, topic_id="T-x", project_id=pid, title="Feature X")
    t.set_topic_thread(db, "T-x", thread_id)
    for ev in ("deps_ready", "claim", "dispatch"):  # running → renders label
        t.topic_transition(db, "T-x", ev)
    # P8: _load_one reads from nodes; mirror the create_topic write into nodes
    # (in production, add_node dual-writes; legacy create_topic does not).
    with db._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO nodes "
            "(id, kind, title, objective, state, project_id, parent_id, "
            "created_at, updated_at) "
            "VALUES ('T-x','task','Feature X','','running',?,NULL,datetime('now'),datetime('now'))",
            (pid,),
        )
        conn.commit()

    from juggle_cockpit_graph_dag import _load_one
    with db._connect() as conn:
        dag = _load_one(conn, pid)
    task = next(n for n in dag.tasks if n.id == "T-x")
    assert task.user_label == "XW"
