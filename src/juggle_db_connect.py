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


def open_connection(path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection with standard juggle pragmas.

    Applies:
      WAL journal mode, synchronous=NORMAL, busy_timeout=5000
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn
