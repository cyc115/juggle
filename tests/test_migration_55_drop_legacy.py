"""Migration 55 (P8 TERMINAL drop) — pins for the irreversible legacy-table drop.

The terminal migration reconciles every conversation node's state from the
authoritative legacy ``threads.status`` (archived→archived, closed→done,
background→background; active leaves a live state as-is), asserts 0 rows remain
divergent, then DROPs threads/graph_topics/graph_tasks/graph_edges. These pins
guard the IRREVERSIBLE step: the ~45 archived-but-open conversations must survive
the drop (incident: a prior reconcile was retired in a merge conflict).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from helpers.node_seed import seed_node  # noqa: E402

_LEGACY = ("threads", "graph_topics", "graph_tasks", "graph_edges")


def _migrated_db(tmp_path):
    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    return db


def _recreate_legacy(conn):
    """init_db's terminal migration drops the legacy tables; re-establish minimal
    stand-ins so the drop migration can be exercised directly on this connection."""
    conn.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, status TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS graph_topics (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE IF NOT EXISTS graph_tasks (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE IF NOT EXISTS graph_edges (node_id TEXT, depends_on_id TEXT)")
    conn.commit()


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_reconcile_preserves_archived_state_then_drops_legacy(tmp_path):
    """A conversation node reading state='open' while threads.status='archived'
    must be reconciled to 'archived' (preserved) BEFORE the legacy tables drop."""
    db = _migrated_db(tmp_path)
    with db._connect() as conn:
        _recreate_legacy(conn)
        conn.execute("INSERT INTO threads(id,status) VALUES ('c1','archived')")
        seed_node(conn, id="c1", kind="conversation", title="C1", state="open")
        conn.commit()

        from dbops.migration_55_drop_legacy import migrate_55_drop_legacy
        migrate_55_drop_legacy(conn)

        assert conn.execute("SELECT state FROM nodes WHERE id='c1'").fetchone()[0] == "archived"
        tables = _tables(conn)
        for t in _LEGACY:
            assert t not in tables, f"{t} was not dropped"


def test_closed_and_background_reconcile_before_drop(tmp_path):
    """closed→done and background→background are reconciled onto the node too."""
    db = _migrated_db(tmp_path)
    with db._connect() as conn:
        _recreate_legacy(conn)
        conn.execute("INSERT INTO threads(id,status) VALUES ('d1','closed'),('b1','background')")
        seed_node(conn, id="d1", kind="conversation", title="D1", state="open")
        seed_node(conn, id="b1", kind="conversation", title="B1", state="open")
        conn.commit()

        from dbops.migration_55_drop_legacy import migrate_55_drop_legacy
        migrate_55_drop_legacy(conn)

        assert conn.execute("SELECT state FROM nodes WHERE id='d1'").fetchone()[0] == "done"
        assert conn.execute("SELECT state FROM nodes WHERE id='b1'").fetchone()[0] == "background"


def test_active_thread_leaves_live_node_state_as_is(tmp_path):
    """An 'active' thread must NOT clobber a live 'running' node state."""
    db = _migrated_db(tmp_path)
    with db._connect() as conn:
        _recreate_legacy(conn)
        conn.execute("INSERT INTO threads(id,status) VALUES ('r1','active')")
        seed_node(conn, id="r1", kind="conversation", title="R1", state="running")
        conn.commit()

        from dbops.migration_55_drop_legacy import migrate_55_drop_legacy
        migrate_55_drop_legacy(conn)

        assert conn.execute("SELECT state FROM nodes WHERE id='r1'").fetchone()[0] == "running"


def test_messages_fk_no_longer_references_threads(tmp_path):
    """The FK-bearing tables (messages etc.) are rebuilt to reference nodes, so no
    table dangles a FK at the dropped threads table."""
    db = _migrated_db(tmp_path)
    with db._connect() as conn:
        fks = {fk[2] for fk in conn.execute("PRAGMA foreign_key_list(messages)")}
        assert "threads" not in fks
        assert "nodes" in fks


def test_idempotent_second_init_is_noop(tmp_path):
    """Running init_db twice must not raise — the second terminal-drop pass sees
    the legacy tables already gone and no-ops before taking any lock."""
    db = _migrated_db(tmp_path)
    db.init_db()  # second pass — must not raise
    with db._connect() as conn:
        assert "threads" not in _tables(conn)
        from dbops.migration_55_drop_legacy import migrate_55_drop_legacy
        migrate_55_drop_legacy(conn)  # direct re-call is also a no-op


def test_static_legacy_refs_zero_after_terminal_drop():
    """Gate A static scan must read 0 once dead juggle_migrate_lifecycle.py is gone."""
    from pathlib import Path
    from dbops.p8_readiness import scan_legacy_refs
    src = Path(__file__).resolve().parent.parent / "src"
    assert len(scan_legacy_refs(src)) == 0
