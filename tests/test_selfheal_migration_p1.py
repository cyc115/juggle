"""selfheal-triage-v2 P1 — Migration 45 regression pins (CHECK drop, index/row survival, fail-loud)."""
import sqlite3

import pytest

from dbops.migrations_recent import _migrate_45_drop_status_check

# DDL with the OLD status CHECK, exactly as a pre-P1 prod DB has it.
_OLD_DDL = """
CREATE TABLE error_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  signature_hash TEXT NOT NULL,
  error_class TEXT NOT NULL CHECK(error_class IN ('A','B')),
  exc_type TEXT, traceback TEXT, entrypoint TEXT, surface TEXT,
  command_args TEXT, juggle_ref TEXT,
  count INTEGER NOT NULL DEFAULT 1,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'
    CHECK(status IN ('open','diagnosing','awaiting_approval','resolved')),
  action_item_id INTEGER
);
"""


def _make_old_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute(_OLD_DDL)
    conn.execute(
        "CREATE UNIQUE INDEX idx_error_events_sig "
        "ON error_events(signature_hash) WHERE status != 'resolved'"
    )
    conn.execute("CREATE INDEX idx_error_events_status ON error_events(status)")
    for i in range(5):
        conn.execute(
            "INSERT INTO error_events(signature_hash,error_class,count,first_seen,last_seen,status)"
            " VALUES(?,?,?,?,?,?)",
            (f"sig{i}", "A", i + 1, "2026-01-01 00:00", "2026-01-01 00:00", "resolved" if i == 0 else "open"),
        )
    conn.commit()
    return conn


def test_migration_drops_status_check_and_allows_non_issue(tmp_path):
    """selfheal-v2 P1 (2026-06-21): non_issue insert blocked by old CHECK -> allowed after rebuild."""
    conn = _make_old_db(tmp_path / "t.db")
    _migrate_45_drop_status_check(conn)
    conn.execute(
        "INSERT INTO error_events(signature_hash,error_class,count,first_seen,last_seen,status)"
        " VALUES(?,?,?,?,?,?)",
        ("signew", "A", 1, "2026-01-01 00:00", "2026-01-01 00:00", "non_issue"),
    )
    conn.commit()
    sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='error_events'"
    ).fetchone()[0]
    assert "CHECK(status IN" not in sql.replace(" ", "")  # status CHECK gone
    assert "error_classIN('A','B')" in sql.replace(" ", "")  # error_class CHECK kept


def test_migration_preserves_rows_and_indices(tmp_path):
    """selfheal-v2 P1 (2026-06-21): rebuild must not lose rows or drop indices/triggers."""
    conn = _make_old_db(tmp_path / "t.db")
    before_rows = conn.execute("SELECT COUNT(*) FROM error_events").fetchone()[0]
    before_idx = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='error_events'"
            " AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    before_trg = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='error_events'"
    ).fetchone()[0]
    _migrate_45_drop_status_check(conn)
    after_rows = conn.execute("SELECT COUNT(*) FROM error_events").fetchone()[0]
    after_idx = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='error_events'"
            " AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    after_trg = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND tbl_name='error_events'"
    ).fetchone()[0]
    assert after_rows == before_rows == 5
    assert after_idx == before_idx == {"idx_error_events_sig", "idx_error_events_status"}
    assert after_trg == before_trg == 0


def test_migration_is_idempotent(tmp_path):
    """selfheal-v2 P1 (2026-06-21): running twice is a safe no-op."""
    conn = _make_old_db(tmp_path / "t.db")
    _migrate_45_drop_status_check(conn)
    _migrate_45_drop_status_check(conn)  # must not raise
    assert conn.execute("SELECT COUNT(*) FROM error_events").fetchone()[0] == 5


def test_migration_fails_loud_on_lock(tmp_path):
    """selfheal-v2 P1 (2026-06-21): a locked DB must RAISE, never silently skip."""
    conn = _make_old_db(tmp_path / "t.db")
    # Hold a competing write lock from a second connection.
    blocker = sqlite3.connect(str(tmp_path / "t.db"), timeout=0)
    blocker.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError):
        _migrate_45_drop_status_check(conn)
    blocker.rollback()
