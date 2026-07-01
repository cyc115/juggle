"""juggle_db_connect — single seam for opening SQLite connections.

All production code that needs a raw sqlite3.Connection must go through
open_connection() so pragmas (WAL, synchronous, busy_timeout) are applied
consistently and tmpfs-path routing is isolated here.

Usage:
    from juggle_db_connect import open_connection
    conn = open_connection(db_path)

Do NOT call sqlite3.connect() directly outside of this module and
juggle_db_bootstrap / juggle_cmd_db_flush (which use the backup API
for a different purpose).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


class _ClosingConnection(sqlite3.Connection):
    """A connection that CLOSES itself when a `with` block exits.

    A stock sqlite3 connection's context manager only commits/rolls back on
    ``__exit__`` — it leaves the connection (and its OS file descriptor) open.
    Under the long-running watchdog daemon the ubiquitous
    ``with db._connect() as conn:`` pattern therefore leaked one open FD per DB
    op per tick (defect B: ~60 FDs to juggle.db observed). Closing on ``__exit__``
    releases the FD deterministically at block exit, independent of GC timing.

    Bare ``conn = db._connect()`` callers are unaffected — they don't use ``with``
    and already ``conn.close()`` in their own ``finally`` (a second close is a
    no-op).
    """

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            # Preserve stock behaviour: commit on success, rollback on error.
            super().__exit__(exc_type, exc_val, exc_tb)
        finally:
            self.close()
        return False


def open_connection(path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection with standard juggle pragmas.

    Applies:
      WAL journal mode, synchronous=NORMAL, busy_timeout=5000

    Connections close deterministically at ``with``-block exit (see
    ``_ClosingConnection``) so long-running loops don't leak file descriptors.
    """
    conn = sqlite3.connect(str(path), factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn
