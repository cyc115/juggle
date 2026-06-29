"""Migration 55 (P8 TERMINAL drop) — retire the legacy tables. IRREVERSIBLE.

This is the last P8 collapse step. By now every live reader/writer resolves from
``nodes``/``node_edges``; the legacy ``threads``/``graph_topics``/``graph_tasks``/
``graph_edges`` tables have ZERO live consumers. This migration drops them, but
ONLY after a fail-loud pre-drop reconcile that rescues conversation state still
living in ``threads.status`` but not yet propagated to the conversation node:

  (a) PRE-DROP STATE RECONCILE — set ``nodes.state`` from the authoritative
      ``threads.status`` for the terminal/explicit statuses the node store must
      not lose: archived→archived, closed→done, background→background. An
      'active' thread is LEFT AS-IS — the conversation node's live state
      (open/running/background) is the richer truth and must not be clobbered to
      'open'. Then ASSERT (fail-loud) that 0 conversation rows remain divergent;
      a non-zero count ROLLS BACK and aborts the upgrade so no archived state is
      lost. (Incident: ~45 archived-but-open conversations existed after a prior
      reconcile was retired in a merge conflict.)
  (b) FK REPOINT — rebuild any table whose schema still carries a FOREIGN KEY at
      ``threads(id)`` (messages/notifications/notifications_v2/action_items/
      agent_runs) to reference ``nodes(id)`` instead, via the SQLite table-rebuild
      idiom (cf. Migration 41), so no surviving table dangles a FK at the dropped
      table. Detected dynamically — only rebuilt if such a FK actually exists.
  (c) DROP — graph_edges, graph_tasks, graph_topics, threads in FK-safe order,
      presence-guarded (skip if already absent → idempotent re-run).

FAIL-LOUD: ``BEGIN IMMEDIATE`` takes the write lock up front (cf. Migration 51/53);
lock contention PROPAGATES and the init_db caller aborts. The whole drop is one
transaction — a failing assert rolls everything back. Apply via ``juggle doctor``
(behind ``assert_migration_allowed``); never run directly against the shared prod
DB. Idempotent (re-run sees the tables already gone and no-ops before the lock).
"""
from __future__ import annotations

import logging
import re
import sqlite3

_log = logging.getLogger(__name__)

_LEGACY_DROP_ORDER = ("graph_edges", "graph_tasks", "graph_topics", "threads")

# threads.status -> node state for the statuses the node store is AUTHORITATIVE-
# from. 'active' is intentionally absent: a live conversation node keeps its own
# state (open/running/background); active never forces a downgrade to 'open'.
_RECONCILE = (("archived", "archived"), ("closed", "done"), ("background", "background"))


def migrate_55_drop_legacy(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    # No-op BEFORE any lock when the legacy tables are already gone (idempotent
    # re-run) or the unified store was never built (pre-Migration-44 DB).
    if not (set(_LEGACY_DROP_ORDER) & tables) or "nodes" not in tables:
        return

    # Flush any pending implicit transaction (e.g. Migration 54's uncommitted
    # UPDATEs) so the PRAGMA + BEGIN IMMEDIATE below run in a clean autocommit
    # state — PRAGMA foreign_keys is a silent no-op inside an open transaction.
    conn.commit()
    prev_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")  # table-rebuild idiom (cf. Migration 41)

    prev_iso = conn.isolation_level
    conn.isolation_level = None               # explicit transaction control
    conn.execute("BEGIN IMMEDIATE")           # write lock up front; raises on contention (fail-LOUD)
    try:
        if "threads" in tables:
            _reconcile_conv_state(conn)       # (a) — fail-loud assert inside
        _repoint_thread_fks(conn)             # (b)
        for t in _LEGACY_DROP_ORDER:          # (c) FK-safe order, presence-guarded
            if t in tables:
                conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute("COMMIT")
        _log.info("Migration 55: legacy tables dropped after pre-drop state reconcile")
    except Exception:
        conn.execute("ROLLBACK")              # fail-LOUD: nothing dropped, abort upgrade
        raise
    finally:
        conn.isolation_level = prev_iso
        conn.execute(f"PRAGMA foreign_keys = {'ON' if prev_fk else 'OFF'}")


def _reconcile_conv_state(conn: sqlite3.Connection) -> None:
    """Set nodes.state from the authoritative threads.status for the terminal
    statuses, then fail-loud if any conversation row stays divergent."""
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
    if "status" not in tcols:
        return  # very old pre-status threads schema — nothing authoritative to read

    for status, state in _RECONCILE:
        conn.execute(
            "UPDATE nodes SET state=? "
            "WHERE kind='conversation' AND state!=? "
            "AND id IN (SELECT id FROM threads WHERE status=?)",
            (state, state, status),
        )

    divergent = conn.execute(
        "SELECT COUNT(*) FROM nodes n JOIN threads t ON t.id = n.id "
        "WHERE n.kind='conversation' AND ("
        "  (t.status='archived'   AND n.state!='archived') OR "
        "  (t.status='closed'     AND n.state!='done') OR "
        "  (t.status='background' AND n.state!='background'))"
    ).fetchone()[0]
    if divergent:
        raise RuntimeError(
            f"Migration 55: {divergent} conversation node(s) still divergent from "
            "threads.status after reconcile — aborting the legacy-table drop to "
            "preserve their state"
        )


def _repoint_thread_fks(conn: sqlite3.Connection) -> None:
    """Rebuild every user table whose schema FOREIGN-KEYs threads(id) to instead
    reference nodes(id), so no table dangles a FK at the about-to-be-dropped table.
    Only tables with such a FK are touched (detected via foreign_key_list)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
    ).fetchall()
    for (name,) in rows:
        if name in _LEGACY_DROP_ORDER:
            continue  # being dropped anyway
        fks = conn.execute(f'PRAGMA foreign_key_list("{name}")').fetchall()
        if any(fk[2] == "threads" for fk in fks):
            _rebuild_fk_to_nodes(conn, name)


def _rebuild_fk_to_nodes(conn: sqlite3.Connection, name: str) -> None:
    """SQLite table-rebuild: recreate ``name`` with its FK repointed threads→nodes,
    preserving rows, indexes and triggers. Runs with foreign_keys OFF (set by the
    caller) so copying rows never re-validates the FK. Reused verbatim by Migration
    56 to repoint the one table (``notifications``) whose FK targeted the transient
    ``threads_old`` rename artifact rather than ``threads``."""
    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()[0]
    # Snapshot explicit (sql IS NOT NULL → not auto-) indexes + triggers BEFORE the
    # rename, while their definitions still say "... ON <name>".
    aux = [
        r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name=? "
            "AND type IN ('index','trigger') AND sql IS NOT NULL",
            (name,),
        ).fetchall()
    ]
    # Rewrite the FK clause to ``REFERENCES nodes(id)``. Matches both the live form
    # ``REFERENCES threads(id)`` (messages/notifications_v2/action_items/agent_runs)
    # and the quoted dangling artifact ``REFERENCES "threads_old"(thread_id)``
    # (notifications, Migration 56); both target nodes' ``id`` primary key.
    new_sql = re.sub(
        r'REFERENCES\s+("threads_old"|threads)\s*\(\s*\w+\s*\)',
        "REFERENCES nodes(id)",
        create_sql,
        flags=re.IGNORECASE,
    )

    old = f"{name}__p8old"
    conn.execute(f'ALTER TABLE "{name}" RENAME TO "{old}"')
    conn.execute(new_sql)                                  # recreates `name`, FK -> nodes
    conn.execute(f'INSERT INTO "{name}" SELECT * FROM "{old}"')
    conn.execute(f'DROP TABLE "{old}"')                    # frees the old index/trigger names
    for sql in aux:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass
