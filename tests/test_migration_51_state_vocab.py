import sqlite3

import pytest

from dbops.migration_51_state_vocab import migrate_51_state_vocab


def _mk(conn):
    conn.execute("CREATE TABLE graph_tasks (id TEXT, state TEXT)")
    conn.execute("CREATE TABLE graph_topics (id TEXT, state TEXT, is_mirror INTEGER DEFAULT 0)")
    conn.execute("CREATE TABLE nodes (id TEXT, kind TEXT, parent_id TEXT, state TEXT)")
    conn.execute("INSERT INTO graph_tasks VALUES ('t1','pending'),('t2','ready')")
    conn.execute("INSERT INTO graph_topics VALUES ('p1','pending',0)")
    conn.execute("INSERT INTO nodes VALUES ('t1','task',NULL,'pending'),('c1','conversation',NULL,'open')")
    conn.commit()   # R2-4: migrate now uses BEGIN IMMEDIATE — setup must be committed first


def test_migration_51_maps_pending_to_open():
    """2026-06-27 P8 C3: existing DBs store task state 'pending'; the unified
    engine only understands 'open'. Migration 51 must rewrite pending→open so
    the renamed engine never queries an un-modelled state."""
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    _mk(conn)
    migrate_51_state_vocab(conn)
    assert conn.execute("SELECT state FROM graph_tasks WHERE id='t1'").fetchone()[0] == "open"
    assert conn.execute("SELECT state FROM graph_topics WHERE id='p1'").fetchone()[0] == "open"
    assert conn.execute("SELECT state FROM nodes WHERE id='t1'").fetchone()[0] == "open"
    # conversation node untouched (its 'open' is the conversation entry state):
    assert conn.execute("SELECT state FROM nodes WHERE id='c1'").fetchone()[0] == "open"


def test_migration_51_idempotent():
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    _mk(conn)
    migrate_51_state_vocab(conn); migrate_51_state_vocab(conn)   # second run is a no-op
    assert conn.execute("SELECT COUNT(*) FROM graph_tasks WHERE state='pending'").fetchone()[0] == 0


def test_migration_51_fail_loud_on_lock(tmp_path):
    """2026-06-27 P8 R2-4: M51 must FAIL-LOUD (propagate) on write-lock contention,
    never silently skip — a swallowed skip strands 'pending' rows the renamed engine
    cannot process. RED on the v1 fail-soft code (it returns without raising)."""
    dbf = str(tmp_path / "m51.db")
    setup = sqlite3.connect(dbf)
    setup.execute("CREATE TABLE graph_tasks (id TEXT, state TEXT)")
    setup.execute("INSERT INTO graph_tasks VALUES ('t1','pending')")
    setup.commit()
    holder = sqlite3.connect(dbf, timeout=0); holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")                 # hold the write lock
    victim = sqlite3.connect(dbf, timeout=0)
    try:
        with pytest.raises(sqlite3.OperationalError):
            migrate_51_state_vocab(victim)            # must RAISE, not swallow
    finally:
        holder.execute("ROLLBACK")
    # the still-pending row proves the (failed) migration did NOT partially commit:
    assert setup.execute("SELECT state FROM graph_tasks WHERE id='t1'").fetchone()[0] == "pending"
