"""Migration 54 — conversation nodes.state reconciliation + live-label uniqueness
(P8 c4-write-cut defect, 2026-06-29).

Incident: archive/close edits were written to threads.status but the conversation
node's state was not always propagated, leaving ~45 archived threads whose node
read state='open'. Dropping threads with that divergence would permanently
resurrect archived/closed conversations as open. Migration 54 reconciles
nodes.state from threads.status (bijective map) and enforces the live-slug
uniqueness invariant on the node store.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dbops.migration_54_conv_state_parity import migrate_54_conv_state_parity  # noqa: E402
from dbops.node_translation import STATUS_TO_STATE  # noqa: E402


def _mk(conn):
    conn.execute("CREATE TABLE threads (id TEXT, status TEXT)")
    conn.execute(
        "CREATE TABLE nodes (id TEXT, kind TEXT, state TEXT, user_label TEXT, "
        "created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO threads VALUES "
        "('t1','archived'),('t2','closed'),('t3','active'),('t4','background')"
    )
    # The defect: every node still reads a LIVE state regardless of threads.status.
    conn.execute(
        "INSERT INTO nodes VALUES "
        "('t1','conversation','open','AA','1'),"
        "('t2','conversation','open','AB','2'),"
        "('t3','conversation','open','AC','3'),"
        "('t4','conversation','running','AD','4')"
    )
    conn.commit()


def _state(conn, nid):
    return conn.execute("SELECT state FROM nodes WHERE id=?", (nid,)).fetchone()[0]


def test_migration_54_reconciles_divergent_state():
    """archived->archived, closed->done, active->open, background->background.
    RED before migration 54 (the node keeps its stale 'open')."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk(conn)
    migrate_54_conv_state_parity(conn)
    assert _state(conn, "t1") == "archived"
    assert _state(conn, "t2") == "done"
    assert _state(conn, "t3") == "open"
    assert _state(conn, "t4") == "background"


def test_migration_54_zero_disagreements_acceptance():
    """Acceptance pin: after the migration, 0 rows where threads.status and
    nodes.state disagree per STATUS_TO_STATE."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk(conn)
    migrate_54_conv_state_parity(conn)
    rows = conn.execute(
        "SELECT t.status AS status, n.state AS state FROM threads t "
        "JOIN nodes n ON n.id=t.id WHERE n.kind='conversation'"
    ).fetchall()
    disagreements = [
        (r["status"], r["state"]) for r in rows
        if STATUS_TO_STATE.get(r["status"]) != r["state"]
    ]
    assert disagreements == [], f"state/status disagreements remain: {disagreements}"


def test_migration_54_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk(conn)
    migrate_54_conv_state_parity(conn)
    migrate_54_conv_state_parity(conn)
    assert _state(conn, "t1") == "archived"


def test_migration_54_repairs_duplicate_live_labels_and_builds_index():
    """A stale 'open' could mask a reused slug (two LIVE nodes sharing a label).
    Migration 54 repairs the duplicate then builds the unique idx_nodes_live_label
    so the duplicate-live-slug invariant (2026-06-21 db8dfb62) is DB-enforced on
    the node store."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE nodes (id TEXT, kind TEXT, state TEXT, user_label TEXT, "
        "created_at TEXT)"
    )
    conn.execute(
        "INSERT INTO nodes VALUES "
        "('a','conversation','open','ZZ','1'),"
        "('b','conversation','open','ZZ','2')"
    )
    conn.commit()
    migrate_54_conv_state_parity(conn)  # no threads table → reconcile skipped
    live = [
        r[0] for r in conn.execute(
            "SELECT user_label FROM nodes "
            "WHERE state IN ('open','running','background')"
        ).fetchall()
    ]
    assert len(live) == len(set(live)), f"duplicate live label survived: {live}"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE nodes SET user_label='ZZ' WHERE id='b'")
        conn.commit()
