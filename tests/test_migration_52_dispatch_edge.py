"""Tests for Migration 52 (P8 M1/Q2): the task→dispatch-thread relation becomes a
typed ``kind='dispatch'`` row in ``node_edges``.

All tests run against an in-memory SQLite DB — never prod. Migration 52 adds
``node_edges.kind`` (DEFAULT 'dep') and backfills a ``kind='dispatch'`` edge
``(task_node_id, conversation_node_id)`` from the legacy ``nodes.dispatch_thread_id``
column that Migration 50 populated; the raw column is retired in Migration 53.
"""
import sqlite3

from dbops.migration_52_dispatch_edge import migrate_52_dispatch_edge


def _mk(conn):
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, state TEXT, "
        "dispatch_thread_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE node_edges (node_id TEXT, depends_on_id TEXT, "
        "PRIMARY KEY (node_id, depends_on_id))"
    )
    # task t2 depends on t1 (a dep edge) AND is dispatched to conversation c1.
    conn.execute("INSERT INTO nodes VALUES ('t1','task','verified',NULL)")
    conn.execute("INSERT INTO nodes VALUES ('t2','task','open','c1')")
    conn.execute("INSERT INTO nodes VALUES ('c1','conversation','background',NULL)")
    conn.execute("INSERT INTO node_edges (node_id, depends_on_id) VALUES ('t2','t1')")
    conn.commit()


def test_migration_52_adds_kind_and_backfills_dispatch_edge():
    """2026-06-29 P8 M1/Q2: node_edges gains a kind column; the legacy
    nodes.dispatch_thread_id becomes a typed kind='dispatch' edge while every
    pre-existing dependency edge defaults to kind='dep'."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk(conn)
    migrate_52_dispatch_edge(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(node_edges)")}
    assert "kind" in cols, "node_edges.kind column not added"

    # pre-existing dep edge defaulted to 'dep'
    dep = conn.execute(
        "SELECT kind FROM node_edges WHERE node_id='t2' AND depends_on_id='t1'"
    ).fetchone()
    assert dep[0] == "dep"

    # the dispatch binding is now a typed kind='dispatch' edge (t2 → c1)
    disp = conn.execute(
        "SELECT depends_on_id, kind FROM node_edges WHERE node_id='t2' AND kind='dispatch'"
    ).fetchone()
    assert disp is not None and disp[0] == "c1"


def test_migration_52_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk(conn)
    migrate_52_dispatch_edge(conn)
    migrate_52_dispatch_edge(conn)  # second run is a no-op
    n = conn.execute(
        "SELECT COUNT(*) FROM node_edges WHERE kind='dispatch'"
    ).fetchone()[0]
    assert n == 1


def test_migration_52_skips_dispatch_to_missing_node():
    """A dispatch_thread_id that points at no existing node creates no edge —
    node_edges stays referentially sane (the FK target must exist)."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, state TEXT, "
        "dispatch_thread_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE node_edges (node_id TEXT, depends_on_id TEXT, "
        "PRIMARY KEY (node_id, depends_on_id))"
    )
    conn.execute("INSERT INTO nodes VALUES ('t1','task','open','ghost')")
    conn.commit()
    migrate_52_dispatch_edge(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM node_edges WHERE kind='dispatch'"
    ).fetchone()[0] == 0


def test_migration_52_no_node_edges_table_is_noop():
    """Pre-Migration-44 DB without node_edges → migration is a cheap no-op."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate_52_dispatch_edge(conn)  # must not raise
