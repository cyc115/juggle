"""dbops.migration_seq — DB-reserved migration numbers (P2, 2026-07-03).

Kills the duplicate-column failure class (2026-07-02 cluster): two coders
picking the same "next" migration number by eyeballing ``dbops/migration_*.py``
concurrently, then both landing ``ALTER TABLE ADD COLUMN`` for the same number.
``reserve_next`` hands out a strictly-increasing integer atomically, so two
concurrent callers against the same DB file never collide — SQLite's own file
locking (busy_timeout, set on every connection by ``juggle_db_connect``) plus a
single atomic ``UPDATE ... RETURNING`` serializes reservations.

BOOTSTRAP is one past the highest migration file that exists at the time this
module is authored (64) — a plain hand-picked number, same as every migration
before the allocator existed (chicken-egg, ack'd in the plan's DA log).
"""

from __future__ import annotations

import sqlite3

BOOTSTRAP_NEXT = 65

CREATE_MIGRATION_SEQ = """
CREATE TABLE IF NOT EXISTS migration_seq (
  id            INTEGER PRIMARY KEY CHECK (id = 1),
  next_number   INTEGER NOT NULL
);
"""


def ensure_migration_seq(conn: sqlite3.Connection) -> None:
    """Create migration_seq and seed it at BOOTSTRAP_NEXT — idempotent, never
    resets an already-advanced counter."""
    conn.execute(CREATE_MIGRATION_SEQ)
    conn.execute(
        "INSERT OR IGNORE INTO migration_seq (id, next_number) VALUES (1, ?)",
        (BOOTSTRAP_NEXT,),
    )
    conn.commit()


def reserve_next(conn: sqlite3.Connection) -> int:
    """Atomically reserve and return the next migration number.

    Single-statement UPDATE...RETURNING under SQLite's own file lock — safe
    across concurrent connections/processes against the same DB file.
    """
    ensure_migration_seq(conn)
    row = conn.execute(
        "UPDATE migration_seq SET next_number = next_number + 1 "
        "WHERE id = 1 RETURNING next_number - 1"
    ).fetchone()
    conn.commit()
    return row[0]
