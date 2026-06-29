"""Migration 53 (P8 M2): graph topics become kind='topic' nodes.

Pins the kind-discriminator introduction: a graph_topics member's node flips
from kind='task' to kind='topic' so the topic/task distinction survives the
graph_topics drop (next node). Bare tasks, child tasks, and conversations stay.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from dbops.migration_53_kind_topic import migrate_53_kind_topic


def _mk(conn):
    conn.execute(
        "CREATE TABLE graph_topics (id TEXT, project_id TEXT, state TEXT, "
        "is_mirror INTEGER DEFAULT 0)"
    )
    conn.execute("CREATE TABLE nodes (id TEXT, kind TEXT, parent_id TEXT, state TEXT)")
    conn.execute(
        "INSERT INTO graph_topics VALUES ('top1','P','open',0),('top2','P','open',0)"
    )
    conn.executescript(
        "INSERT INTO nodes VALUES ('top1','task',NULL,'open');"
        "INSERT INTO nodes VALUES ('top2','task',NULL,'open');"
        "INSERT INTO nodes VALUES ('bare','task',NULL,'open');"   # bare root task, NOT a topic
        "INSERT INTO nodes VALUES ('child','task','top1','open');"  # child of a topic
        "INSERT INTO nodes VALUES ('conv','conversation',NULL,'open');"
    )
    conn.commit()  # M53 uses BEGIN IMMEDIATE — setup must be committed first


def test_migration_53_flips_graph_topics_to_kind_topic():
    """2026-06-29 P8 M2: graph_topics members must become kind='topic' so the
    discriminator survives the graph_topics drop. Bare tasks, child tasks, and
    conversation nodes are untouched."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk(conn)
    migrate_53_kind_topic(conn)
    kinds = {r["id"]: r["kind"] for r in conn.execute("SELECT id, kind FROM nodes")}
    assert kinds["top1"] == "topic"
    assert kinds["top2"] == "topic"
    assert kinds["bare"] == "task"   # bare root task is NOT a topic
    assert kinds["child"] == "task"
    assert kinds["conv"] == "conversation"


def test_migration_53_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _mk(conn)
    migrate_53_kind_topic(conn)
    migrate_53_kind_topic(conn)  # second run is a no-op
    assert conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE id IN ('top1','top2') AND kind='task'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE kind='topic'"
    ).fetchone()[0] == 2


def test_migration_53_noop_without_tables():
    conn = sqlite3.connect(":memory:")
    migrate_53_kind_topic(conn)  # no nodes/graph_topics → cheap no-op, no raise


def test_migration_53_fail_loud_on_lock(tmp_path):
    """2026-06-29 P8 M2: M53 must FAIL-LOUD on write-lock contention, never
    silently skip — the same-release topic predicates hard-depend on
    kind='topic'; a swallowed skip leaves topics invisible to the topic engine."""
    dbf = str(tmp_path / "m53.db")
    setup = sqlite3.connect(dbf)
    setup.execute("CREATE TABLE graph_topics (id TEXT, is_mirror INTEGER DEFAULT 0)")
    setup.execute("CREATE TABLE nodes (id TEXT, kind TEXT, parent_id TEXT, state TEXT)")
    setup.execute("INSERT INTO graph_topics VALUES ('top1',0)")
    setup.execute("INSERT INTO nodes VALUES ('top1','task',NULL,'open')")
    setup.commit()
    holder = sqlite3.connect(dbf, timeout=0)
    holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")  # hold the write lock
    victim = sqlite3.connect(dbf, timeout=0)
    try:
        with pytest.raises(sqlite3.OperationalError):
            migrate_53_kind_topic(victim)  # must RAISE, not swallow
    finally:
        holder.execute("ROLLBACK")
    # the still-task row proves the failed migration did NOT partially commit:
    assert setup.execute(
        "SELECT kind FROM nodes WHERE id='top1'"
    ).fetchone()[0] == "task"
