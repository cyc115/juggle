"""Migration 37 — graph_topics backfill (3-tier R9, 2026-06-11).

REGRESSION-CRITICAL: flat graph_nodes (task≡topic) must migrate to synthetic
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
from dbops import db_graph as g  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "m37.db"))
    d.init_db()  # init_db runs all migrations, incl. 37
    return d


def _flat_node(db, nid, state="pending", thread_id=None, updated_at=None):
    g.create_node(db, node_id=nid, project_id="INBOX", title=f"N {nid}", prompt="p")
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_nodes SET state=?, thread_id=?, topic_id=NULL, "
            "updated_at=COALESCE(?, updated_at) WHERE id=?",
            (state, thread_id, updated_at, nid),
        )
        conn.commit()


def _migrate(db):
    from dbops.migrations_recent import apply_recent_migrations
    with db._connect() as conn:
        apply_recent_migrations(conn)
        conn.commit()


def test_fresh_db_has_graph_topics_table(db):
    with db._connect() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='graph_topics'"
        ).fetchone()
    assert row, "graph_topics must exist after init_db"


def test_backfill_wraps_flat_node_in_synthetic_topic(db):
    """REGRESSION PIN (2026-06-11): flat→3-tier. Node state, thread binding,
    and updated_at must be ADOPTED by the synthetic topic."""
    _flat_node(db, "x", state="running", thread_id="th-1",
               updated_at="2026-06-01T00:00:00+00:00")
    _migrate(db)
    with db._connect() as conn:
        node = dict(conn.execute("SELECT * FROM graph_nodes WHERE id='x'").fetchone())
        topic = dict(conn.execute(
            "SELECT * FROM graph_topics WHERE id=?", (node["topic_id"],)
        ).fetchone())
    assert node["topic_id"] == "T-x"
    assert topic["state"] == "running"
    assert topic["thread_id"] == "th-1"
    assert topic["updated_at"] == "2026-06-01T00:00:00+00:00"
    assert topic["project_id"] == "INBOX"


def test_backfill_is_idempotent(db):
    _flat_node(db, "y")
    _migrate(db)
    _migrate(db)  # re-run must not duplicate or error
    with db._connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM graph_topics WHERE id='T-y'").fetchone()[0]
    assert n == 1


def test_backfill_collision_uses_alternate_id(db):
    """A node literally named 'T-z' must not abort the migration when node 'z'
    also exists — collision falls back to 'T#z' (spec DA weakest-item #4)."""
    _flat_node(db, "T-z")
    _flat_node(db, "z")
    _migrate(db)
    with db._connect() as conn:
        tz = conn.execute(
            "SELECT topic_id FROM graph_nodes WHERE id='z'").fetchone()[0]
        topics = {r[0] for r in conn.execute("SELECT id FROM graph_topics")}
    assert tz == "T#z"
    assert len(topics) == 2, "both nodes wrapped despite the name collision"
