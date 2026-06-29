"""Migration 37 — graph_topics backfill (3-tier R9, 2026-06-11).

REGRESSION-CRITICAL: flat graph_tasks (task≡topic) must migrate to synthetic
single-task topics that ADOPT state/thread_id/updated_at so in-flight graphs
keep running (spec DA weakest-item #1: updated_at must be COPIED, not now(),
or the stale-claim sweep timing changes under migration)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "m37.db"))
    d.init_db()  # init_db runs all migrations, incl. 37
    return d


def _flat_task(db, nid, state="pending", thread_id=None, updated_at=None):
    # P8 c4-write-cut: db_graph.create_task no longer writes graph_tasks, so seed
    # the flat graph_tasks row (topic_id NULL) DIRECTLY — this test pins the legacy
    # Migration-37 backfill (graph_tasks → synthetic graph_topics), whose behavior
    # is unchanged; only the seeding seam moves off create_task.
    now = "2026-05-01T00:00:00+00:00"
    from helpers.node_seed import make_legacy_tables
    with db._connect() as conn:
        # P8 terminal: graph_tasks/graph_topics dropped on init_db; re-create them
        # so the Migration-37 backfill under test has its legacy source/target.
        make_legacy_tables(conn, "graph_tasks", "graph_topics")
        conn.execute(
            "INSERT INTO graph_tasks (id, project_id, title, prompt, state, "
            "thread_id, topic_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,NULL,?,?)",
            (nid, "INBOX", f"N {nid}", "p", state, thread_id, now,
             updated_at or now),
        )
        conn.commit()


def _migrate(db):
    # P8 terminal: apply_recent_migrations now ends with the legacy-table DROP
    # (Migration 55), which would erase the graph_topics this test inspects. Run
    # ONLY the graph chain (M35-37/39) so the Migration-37 synthetic-topic backfill
    # is exercised in isolation, on the legacy tables, without the terminal drop.
    from dbops.migrations_graph import apply_graph_migrations
    with db._connect() as conn:
        apply_graph_migrations(conn)
        conn.commit()


def test_fresh_db_has_graph_topics_table(db):
    """P8 terminal: a fresh DB has NO graph_topics — it is dropped (Migration 55).
    Topics are kind='topic' nodes in the unified store."""
    with db._connect() as conn:
        gone = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_topics'"
        ).fetchone() is None
        has_nodes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='nodes'"
        ).fetchone() is not None
    assert gone, "graph_topics must be dropped (terminal drop)"
    assert has_nodes, "the unified nodes store must exist"


def test_backfill_wraps_flat_task_in_synthetic_topic(db):
    """REGRESSION PIN (2026-06-11): flat→3-tier. Task state, thread binding,
    and updated_at must be ADOPTED by the synthetic topic."""
    _flat_task(db, "x", state="running", thread_id="th-1",
               updated_at="2026-06-01T00:00:00+00:00")
    _migrate(db)
    with db._connect() as conn:
        task = dict(conn.execute("SELECT * FROM graph_tasks WHERE id='x'").fetchone())
        topic = dict(conn.execute(
            "SELECT * FROM graph_topics WHERE id=?", (task["topic_id"],)
        ).fetchone())
    assert task["topic_id"] == "T-x"
    assert topic["state"] == "running"
    assert topic["thread_id"] == "th-1"
    assert topic["updated_at"] == "2026-06-01T00:00:00+00:00"
    assert topic["project_id"] == "INBOX"


def test_backfill_is_idempotent(db):
    _flat_task(db, "y")
    _migrate(db)
    _migrate(db)  # re-run must not duplicate or error
    with db._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM graph_topics WHERE id='T-y'").fetchone()[0]
    assert n == 1


def test_backfill_collision_uses_alternate_id(db):
    """A task literally named 'T-z' must not abort the migration when task 'z'
    also exists — collision falls back to 'T#z' (spec DA weakest-item #4)."""
    _flat_task(db, "T-z")
    _flat_task(db, "z")
    _migrate(db)
    with db._connect() as conn:
        tz = conn.execute(
            "SELECT topic_id FROM graph_tasks WHERE id='z'").fetchone()[0]
        topics = {r[0] for r in conn.execute("SELECT id FROM graph_topics")}
    assert tz == "T#z"
    assert len(topics) == 2, "both tasks wrapped despite the name collision"
