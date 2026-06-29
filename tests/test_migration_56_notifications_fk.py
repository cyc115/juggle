"""Migration 56 (P8 tail) — pins for the last stray thread FK repoint.

Incident (2026-06-29): after the P8 terminal drop (Migration 55, v1.86.0) the
``notifications`` table still carried ``thread_id ... REFERENCES "threads_old"
(thread_id)`` — a dangling FK to a table that never existed in the unified store
(``threads_old`` was a transient rename artifact from Migration 1). Migration 55
repointed only FKs whose target was literally ``threads``, so this one slipped
through. Inert only because the app runs with PRAGMA foreign_keys OFF; with FK ON
an INSERT raises "no such table: main.threads_old". Migration 56 rebuilds
notifications so thread_id REFERENCES nodes(id) like every sibling table.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dbops.migration_56_notifications_fk import migrate_56_notifications_fk  # noqa: E402


def _seed_dangling_db(conn):
    """Reproduce the prod state: a ``notifications`` table whose FK dangles at the
    non-existent ``threads_old`` (built via the exact base CREATE + the ALTER-added
    columns Migrations 11/13 appended), plus a minimal ``nodes`` table."""
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT, title TEXT, "
        "state TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute("INSERT INTO nodes(id) VALUES ('c1'), ('c2')")
    conn.execute(
        'CREATE TABLE notifications (\n'
        '  id              INTEGER PRIMARY KEY AUTOINCREMENT,\n'
        '  thread_id       TEXT NOT NULL REFERENCES "threads_old"(thread_id),\n'
        '  message         TEXT NOT NULL,\n'
        '  delivered       INTEGER DEFAULT 0,\n'
        '  created_at      TEXT NOT NULL\n'
        ')'
    )
    conn.execute("ALTER TABLE notifications ADD COLUMN delivery_attempts INTEGER DEFAULT 0")
    conn.execute("ALTER TABLE notifications ADD COLUMN severity TEXT DEFAULT 'action'")
    conn.executemany(
        "INSERT INTO notifications(id, thread_id, message, delivered, created_at, "
        "delivery_attempts, severity) VALUES (?,?,?,?,?,?,?)",
        [
            (1, "c1", "first",  0, "2026-01-01", 0, "action"),
            (2, "c2", "second", 1, "2026-01-02", 3, "info"),
            (3, "c1", "third",  0, "2026-01-03", 1, "action"),
        ],
    )
    conn.commit()


def _schema(conn, table="notifications"):
    return conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()[0]


def _rows(conn):
    return conn.execute(
        "SELECT id, thread_id, message, delivered, created_at, delivery_attempts, "
        "severity FROM notifications ORDER BY id"
    ).fetchall()


def test_repoint_threads_old_to_nodes_preserves_rows(tmp_path):
    """RED-before-fix: notifications FK dangles at threads_old. After Migration 56
    it REFERENCES nodes(id), every row is preserved, and FK enforcement works."""
    conn = sqlite3.connect(str(tmp_path / "j.db"))
    _seed_dangling_db(conn)

    # BEFORE: the FK dangles at the non-existent threads_old — with FK ON any
    # INSERT fails loudly ("no such table: main.threads_old").
    assert "threads_old" in _schema(conn)
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute(
            "INSERT INTO notifications(thread_id, message, created_at) "
            "VALUES ('c1', 'boom', '2026-01-01')"
        )
    conn.rollback()
    conn.execute("PRAGMA foreign_keys = OFF")
    before = _rows(conn)

    migrate_56_notifications_fk(conn)

    # AFTER: FK repointed to nodes(id), no threads_old anywhere in the schema.
    schema = _schema(conn)
    assert "threads_old" not in schema
    assert "REFERENCES nodes(id)" in schema
    fks = conn.execute('PRAGMA foreign_key_list("notifications")').fetchall()
    assert [fk[2] for fk in fks] == ["nodes"]

    # Every row preserved verbatim (ids, messages, severity, delivery_attempts).
    assert _rows(conn) == before
    # All columns survived the rebuild.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(notifications)")}
    assert cols == {"id", "thread_id", "message", "delivered", "created_at",
                    "delivery_attempts", "severity"}

    # foreign_key_check is clean now that the parent table (nodes) exists.
    assert conn.execute("PRAGMA foreign_key_check(notifications)").fetchall() == []

    # With FK ON, an INSERT referencing a valid node SUCCEEDS...
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO notifications(thread_id, message, created_at) "
        "VALUES ('c2', 'live', '2026-01-04')"
    )
    conn.commit()
    # ...and one referencing a non-existent node is REJECTED (FK is real).
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO notifications(thread_id, message, created_at) "
            "VALUES ('ghost', 'bad', '2026-01-05')"
        )
    conn.close()


def test_sqlite_sequence_high_water_preserved(tmp_path):
    """The AUTOINCREMENT high-water mark must not regress to max(id) — ids keep
    incrementing past any since-deleted high ids."""
    conn = sqlite3.connect(str(tmp_path / "j.db"))
    _seed_dangling_db(conn)
    # Simulate rows 4..9 having existed then been deleted: seq=9 > max(id)=3.
    conn.execute("UPDATE sqlite_sequence SET seq=9 WHERE name='notifications'")
    conn.commit()

    migrate_56_notifications_fk(conn)

    seq = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='notifications'"
    ).fetchone()[0]
    assert seq == 9, "high-water mark regressed — would re-issue a used id"

    # Next AUTOINCREMENT id continues from the preserved high-water mark.
    conn.execute(
        "INSERT INTO notifications(thread_id, message, created_at) "
        "VALUES ('c1', 'next', '2026-01-06')"
    )
    new_id = conn.execute("SELECT id FROM notifications WHERE message='next'").fetchone()[0]
    assert new_id == 10
    conn.close()


def test_idempotent_second_run_is_noop(tmp_path):
    """Re-running Migration 56 after it has repointed the FK is a no-op: no error,
    schema unchanged, rows unchanged."""
    conn = sqlite3.connect(str(tmp_path / "j.db"))
    _seed_dangling_db(conn)

    migrate_56_notifications_fk(conn)
    schema_once = _schema(conn)
    rows_once = _rows(conn)

    migrate_56_notifications_fk(conn)  # second pass — must not raise
    assert _schema(conn) == schema_once
    assert _rows(conn) == rows_once
    assert "threads_old" not in _schema(conn)
    conn.close()


def test_absent_table_and_missing_nodes_are_noop(tmp_path):
    """Presence guards: no notifications table, or no nodes table, => no-op."""
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    migrate_56_notifications_fk(conn)  # nothing exists — must not raise

    # notifications present but nodes absent — still a no-op (no unified store).
    conn.execute(
        'CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, '
        'thread_id TEXT NOT NULL REFERENCES "threads_old"(thread_id), '
        'message TEXT NOT NULL, created_at TEXT NOT NULL)'
    )
    conn.commit()
    migrate_56_notifications_fk(conn)
    assert "threads_old" in _schema(conn)  # untouched
    conn.close()


def test_fresh_db_already_references_nodes_is_noop(tmp_path):
    """On a real fully-migrated DB, Migration 55 already rebuilt notifications to
    REFERENCES nodes; Migration 56 must detect that and no-op."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    with db._connect() as conn:
        fks = {fk[2] for fk in conn.execute('PRAGMA foreign_key_list("notifications")')}
        assert fks == {"nodes"}, "init_db should already point notifications at nodes"
        before = _schema(conn)
        migrate_56_notifications_fk(conn)  # no-op
        assert _schema(conn) == before
