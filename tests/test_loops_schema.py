"""Phase-1 schema pins — loops table, projects.kind, nodes.role/delivery, run-seq.

Loop-entity V1 (plan 2026-07-04). Every column defaults to CURRENT behavior so
existing graphs/projects are byte-for-byte unchanged:
  - nodes.role     DEFAULT 'coder'   (existing tasks still dispatch coder)
  - nodes.delivery DEFAULT 'merge'   (existing topics still route merge→verified)
  - projects.kind  DEFAULT 'project' (existing projects unaffected)

The loops table + run_seq carry the loop entity and the atomic id-namespacing
counter (guards the guarded-upsert collision, critique Axis-2 Objection A).
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "loops-schema.db"))
    d.init_db()
    return d


def _mk_project(db) -> str:
    return db.create_project(name="P", objective="o")


# ── Defaults preserve current behavior ───────────────────────────────────────

def test_nodes_role_defaults_coder(db):
    """A node inserted with no role reads 'coder' (spec §4.3 default — zero
    change for existing graphs)."""
    from dbops.db_graph import create_task

    pid = _mk_project(db)
    create_task(db, task_id="t1", project_id=pid, title="T", prompt="p")
    with db._connect() as conn:
        row = conn.execute("SELECT role FROM nodes WHERE id='t1'").fetchone()
    assert row["role"] == "coder"


def test_nodes_delivery_defaults_merge(db):
    """A node inserted with no delivery reads 'merge' (existing topics still
    route merge→verified)."""
    from dbops.db_graph import create_task

    pid = _mk_project(db)
    create_task(db, task_id="t2", project_id=pid, title="T", prompt="p")
    with db._connect() as conn:
        row = conn.execute("SELECT delivery FROM nodes WHERE id='t2'").fetchone()
    assert row["delivery"] == "merge"


def test_existing_node_defaults_coder_merge(db):
    """REGRESSION PIN (loop-v1 Phase 1): an existing-style node (created via the
    unchanged create_task path, no role/delivery supplied) reads coder/merge —
    the schema seam that keeps every legacy graph dispatching coder + routing
    merge→verified exactly as before."""
    from dbops.db_graph import create_task

    pid = _mk_project(db)
    create_task(db, task_id="t3", project_id=pid, title="T", prompt="p")
    with db._connect() as conn:
        row = conn.execute(
            "SELECT role, delivery FROM nodes WHERE id='t3'"
        ).fetchone()
    assert (row["role"], row["delivery"]) == ("coder", "merge")


def test_projects_kind_defaults_project(db):
    """A project inserted with no kind reads 'project' (existing projects
    unaffected — they never claim the loop code path)."""
    pid = _mk_project(db)
    with db._connect() as conn:
        row = conn.execute(
            "SELECT kind FROM projects WHERE id=?", (pid,)
        ).fetchone()
    assert row["kind"] == "project"


# ── loops table CRUD + run-seq ───────────────────────────────────────────────

def test_loops_table_crud(db):
    """create/get/list_active/set_status round-trip on the loops table."""
    pid = _mk_project(db)
    loop_id = db.create_loop(project_id=pid, cadence="daily", next_run="2026-07-05T00:00:00+00:00")
    assert loop_id.startswith("L")

    got = db.get_loop(loop_id)
    assert got is not None
    assert got["project_id"] == pid
    assert got["cadence"] == "daily"
    assert got["status"] == "active"
    assert got["run_seq"] == 0
    assert got["consecutive_failures"] == 0
    assert got["next_run"] == "2026-07-05T00:00:00+00:00"

    active = db.list_active_loops()
    assert loop_id in {loop["id"] for loop in active}

    db.set_loop_status(loop_id, "paused")
    assert db.get_loop(loop_id)["status"] == "paused"
    assert loop_id not in {loop["id"] for loop in db.list_active_loops()}


def test_loop_ids_are_unique_sequential(db):
    """Allocated loop ids are distinct L-labels (mirrors project P-label wheel)."""
    pid = _mk_project(db)
    ids = {db.create_loop(project_id=pid, cadence="daily") for _ in range(3)}
    assert len(ids) == 3
    assert all(i.startswith("L") for i in ids)


def test_run_seq_monotonic_atomic(db):
    """advance_run_seq returns STRICTLY increasing values under repeated calls.

    REGRESSION PIN (critique Axis-2 Objection A, BLOCKER-adjacent): the run-seq
    is the id-namespace that keeps a re-fired loop iteration's node ids from
    colliding with the guarded-upsert refusal (juggle_graph_load.py:71-89).
    A non-monotonic counter would let two iterations mint the same id."""
    pid = _mk_project(db)
    loop_id = db.create_loop(project_id=pid, cadence="daily")
    seen = [db.advance_run_seq(loop_id) for _ in range(5)]
    assert seen == sorted(seen)
    assert len(set(seen)) == len(seen)  # all distinct
    assert seen[0] < seen[-1]  # strictly increasing overall
    # persisted
    assert db.get_loop(loop_id)["run_seq"] == seen[-1]


def test_stamp_last_run(db):
    """stamp_last_run records the iteration timestamp."""
    pid = _mk_project(db)
    loop_id = db.create_loop(project_id=pid, cadence="daily")
    assert db.get_loop(loop_id)["last_run_at"] is None
    db.stamp_last_run(loop_id, "2026-07-04T12:00:00+00:00")
    assert db.get_loop(loop_id)["last_run_at"] == "2026-07-04T12:00:00+00:00"


# ── migration idempotency + fresh/migrated parity ────────────────────────────

def test_migration_72_idempotent_reapply():
    """Applying the node/project column migration twice is a no-op (additive
    convention; mirrors migration 70/71 fail-soft)."""
    from dbops.migration_72_node_role_delivery import migrate_72_node_role_delivery

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, title TEXT, "
        "state TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, "
        "created_at TEXT, last_active TEXT)"
    )
    conn.commit()

    migrate_72_node_role_delivery(conn)
    migrate_72_node_role_delivery(conn)  # must not raise on re-run

    ncols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    pcols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
    assert {"role", "delivery"} <= ncols
    assert "kind" in pcols


def test_migration_72_defaults_applied_on_alter():
    """The ALTER path (already-migrated DB) also yields coder/merge/project — a
    pre-existing node/project row reads the new-column defaults after migrate."""
    from dbops.migration_72_node_role_delivery import migrate_72_node_role_delivery

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, title TEXT, "
        "state TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, "
        "created_at TEXT, last_active TEXT)"
    )
    conn.execute("INSERT INTO nodes(id, kind) VALUES ('n1', 'task')")
    conn.execute("INSERT INTO projects(id, name) VALUES ('P1', 'x')")
    conn.commit()

    migrate_72_node_role_delivery(conn)

    n = conn.execute("SELECT role, delivery FROM nodes WHERE id='n1'").fetchone()
    p = conn.execute("SELECT kind FROM projects WHERE id='P1'").fetchone()
    assert (n["role"], n["delivery"]) == ("coder", "merge")
    assert p["kind"] == "project"


def test_migration_73_loops_idempotent_reapply():
    """Applying the loops-table migration twice is a no-op (CREATE TABLE IF NOT
    EXISTS; mirrors migration 70/71 fail-soft)."""
    from dbops.migration_73_loops import migrate_73_loops

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, "
        "created_at TEXT, last_active TEXT)"
    )
    conn.commit()

    migrate_73_loops(conn)
    migrate_73_loops(conn)  # must not raise on re-run

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "loops" in tables
    cols = {r[1] for r in conn.execute("PRAGMA table_info(loops)")}
    assert {"run_seq", "consecutive_failures", "max_consecutive_failures",
            "status", "next_run", "last_run_at", "cadence"} <= cols


def test_fresh_init_has_all_phase1_schema(db):
    """A fresh `db init` (DDL, no ALTER path) already carries every Phase-1
    column/table — fresh vs migrated parity."""
    with db._connect() as conn:
        ncols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
        pcols = {r[1] for r in conn.execute("PRAGMA table_info(projects)")}
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"role", "delivery"} <= ncols
    assert "kind" in pcols
    assert "loops" in tables
