"""Regression pins: every JuggleDB connection must be corruption-hardened.

Incident (2026-06-10): the shared juggle SQLite DB was vulnerable to corruption
under multi-agent fan-in because the connection factory (JuggleDB._connect) set
no journal/sync pragmas — WAL only stuck if init_db happened to run first, and
synchronous=FULL (a per-connection pragma) was never asserted at all. The
factory now sets WAL + synchronous=FULL + busy_timeout on every connect,
independent of init_db.
"""

import sqlite3
import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def _raw_db(tmp_path):
    """A JuggleDB pointed at a freshly created delete-journal DB file.

    Crucially this does NOT call init_db(), so WAL is only present if the
    factory (_connect) asserts it — pinning the factory, not init_db.
    """
    from juggle_db import JuggleDB

    path = tmp_path / "raw.db"
    seed = sqlite3.connect(str(path))
    seed.execute("PRAGMA journal_mode=DELETE")  # force non-WAL header
    seed.execute("CREATE TABLE t (x)")
    seed.commit()
    seed.close()
    return JuggleDB(db_path=path)


def test_factory_connection_uses_wal_journal_mode(tmp_path):
    """_connect must assert WAL even on a DB whose header is non-WAL."""
    db = _raw_db(tmp_path)
    with db._connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal", f"expected wal, got {mode!r}"


def test_factory_connection_uses_synchronous_full(tmp_path):
    """synchronous=FULL (2) — per-connection, must be set on every connect."""
    db = _raw_db(tmp_path)
    with db._connect() as conn:
        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
    assert sync == 2, f"expected synchronous=FULL (2), got {sync!r}"


def test_factory_connection_sets_busy_timeout(tmp_path):
    """busy_timeout prevents spurious 'database is locked' under fan-in."""
    db = _raw_db(tmp_path)
    with db._connect() as conn:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout >= 5000, f"expected busy_timeout >= 5000ms, got {timeout!r}"
