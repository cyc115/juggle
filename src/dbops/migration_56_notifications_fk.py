"""Migration 56 (P8 tail) — repoint the last stray thread FK.

After the P8 terminal drop (Migration 55, v1.86.0) every sibling table
(messages/notifications_v2/action_items/agent_runs) was rebuilt to
``REFERENCES nodes(id)``. ONE table was missed: ``notifications``. Its FK still
reads ``thread_id ... REFERENCES "threads_old"(thread_id)`` — a dangling
reference to a table that never existed in the unified store (``threads_old``
was a transient rename artifact from Migration 1:
``ALTER TABLE threads RENAME TO threads_old`` … ``DROP TABLE threads_old``).
Migration 55's repoint matched only FKs whose target was literally ``threads``,
so the quoted ``threads_old`` reference slipped through.

It is currently INERT only because the app runs with PRAGMA foreign_keys OFF;
with FK ON, ``INSERT INTO notifications`` fails ("no such table:
main.threads_old"). This migration rebuilds ``notifications`` via the SAME
table-rebuild helper Migration 55 used (``_rebuild_fk_to_nodes``) so
``thread_id`` REFERENCES nodes(id) like every sibling — data-preserving (all
rows + the AUTOINCREMENT high-water mark), idempotent, presence-guarded, and
fail-loud on a locked DB (``BEGIN IMMEDIATE``, cf. Migration 51/53/55).

Out of scope: pre-existing orphan rows (a notifications row whose thread_id
points at an already-deleted node) are LEFT untouched — this fixes the SCHEMA
FK only, never deletes or NULLs data.
"""
from __future__ import annotations

import logging
import sqlite3

from dbops.migration_55_drop_legacy import _rebuild_fk_to_nodes

_log = logging.getLogger(__name__)


def migrate_56_notifications_fk(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    # No-op BEFORE any lock when the table is absent or the unified store was
    # never built (pre-Migration-44 DB) — nothing to repoint.
    if "notifications" not in tables or "nodes" not in tables:
        return
    fks = conn.execute('PRAGMA foreign_key_list("notifications")').fetchall()
    # Idempotent / presence-guarded: only act when some FK still targets a table
    # other than nodes (the stray threads_old reference). Already-repointed (every
    # FK → nodes) or FK-less tables fall through to a no-op.
    if not any(fk[2] != "nodes" for fk in fks):
        return

    # Flush any pending implicit transaction so the PRAGMA + BEGIN IMMEDIATE below
    # run in clean autocommit (PRAGMA foreign_keys is a silent no-op inside a txn).
    conn.commit()
    prev_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")  # table-rebuild idiom (cf. Migration 41/55)

    prev_iso = conn.isolation_level
    conn.isolation_level = None               # explicit transaction control
    conn.execute("BEGIN IMMEDIATE")           # write lock up front; raises on contention (fail-LOUD)
    try:
        # Snapshot the AUTOINCREMENT high-water mark BEFORE the rebuild. The
        # rebuild's INSERT..SELECT bumps the new table's sequence to max(id), which
        # would REGRESS it if higher ids were since deleted — so restore it after.
        seq_row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='notifications'").fetchone()

        _rebuild_fk_to_nodes(conn, "notifications")  # FK "threads_old" -> nodes(id)

        if seq_row is not None:
            old_seq = seq_row[0]
            cur = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='notifications'").fetchone()
            if cur is None:
                conn.execute(
                    "INSERT INTO sqlite_sequence(name, seq) VALUES('notifications', ?)",
                    (old_seq,))
            elif old_seq > cur[0]:
                conn.execute(
                    "UPDATE sqlite_sequence SET seq=? WHERE name='notifications'",
                    (old_seq,))
        conn.execute("COMMIT")
        _log.info("Migration 56: notifications.thread_id FK repointed threads_old -> nodes")
    except Exception:
        conn.execute("ROLLBACK")              # fail-LOUD: nothing changed, abort upgrade
        raise
    finally:
        conn.isolation_level = prev_iso
        conn.execute(f"PRAGMA foreign_keys = {'ON' if prev_fk else 'OFF'}")
