"""Migration 41 (extracted from migrations_recent.py for the loc-gate budget):
drop 4 dead Hindsight columns from `threads` via the SQLite table-rebuild pattern.

DO NOT run against the shared production DB directly; apply via juggle doctor.
"""
from __future__ import annotations
import logging
import sqlite3

_log = logging.getLogger(__name__)


def run_migration_41(conn: sqlite3.Connection) -> None:
    """Drop summary, memory_context, memory_loaded, last_reflect_msg_count from threads.

    Idempotent: no-op if columns are already absent. Uses SQLite table-rebuild
    pattern (ALTER TABLE ... DROP COLUMN is unsupported before SQLite 3.35).
    DO NOT run against the shared production DB directly; apply via juggle doctor.
    """
    _DEAD = frozenset({"summary", "memory_context", "memory_loaded", "last_reflect_msg_count"})

    col_rows = conn.execute("PRAGMA table_info(threads)").fetchall()
    present = {r[1] for r in col_rows}
    if not (_DEAD & present):
        return  # already migrated

    # Snapshot index SQL before any DDL drops them
    idx_sqls = [
        r[0]
        for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='threads' AND sql IS NOT NULL"
        ).fetchall()
    ]

    # Build kept-column list: (name, type, notnull, dflt_value, pk)
    kept = [(r[1], r[2], r[3], r[4], r[5]) for r in col_rows if r[1] not in _DEAD]

    def _col_def(name, typ, notnull, dflt, pk):
        parts = [f'"{name}"']
        if typ:
            parts.append(typ)
        if pk:
            parts.append("PRIMARY KEY")
        elif notnull:
            parts.append("NOT NULL")
        if dflt is not None:
            parts.append(f"DEFAULT {dflt}")
        return " ".join(parts)

    col_defs = ", ".join(_col_def(*c) for c in kept)
    kept_list = ", ".join(f'"{c[0]}"' for c in kept)

    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(f"CREATE TABLE threads_new_mig41 ({col_defs})")
        conn.execute(f"INSERT INTO threads_new_mig41 ({kept_list}) SELECT {kept_list} FROM threads")
        conn.execute("DROP TABLE threads")
        conn.execute("ALTER TABLE threads_new_mig41 RENAME TO threads")
        for sql in idx_sqls:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        _log.info("Migration 41: dropped 4 dead Hindsight columns from threads")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 41 (drop Hindsight cols) skipped: %s", e)
        conn.execute("PRAGMA foreign_keys = ON")
