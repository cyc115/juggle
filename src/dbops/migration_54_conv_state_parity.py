"""Migration 54 (P8 c4-write-cut defect) — reconcile conversation nodes.state
from the legacy threads.status, then enforce the live-label uniqueness invariant
on the authoritative ``nodes`` store.

WHY (the defect): before the write-cut, archive/close edits were written to
``threads`` but the conversation node's ``state`` was not always propagated (the
dual-write mirror predates some legacy paths). Live DBs were observed with ~45
archived threads whose conversation node still read ``state='open'``. Once the
write-cut makes ``nodes`` the SOLE store and ``threads`` is dropped, that stale
``state`` becomes the permanent truth — archived/closed conversations would
resurrect as open. This one-shot, idempotent backfill rewrites every divergent
conversation node's ``state`` to ``STATUS_TO_STATE[threads.status]`` so the two
agree before the drop.

It then (a) repairs any duplicate LIVE labels the stale state had hidden and
(b) creates the partial unique index ``idx_nodes_live_label`` — the node-store
equivalent of ``idx_threads_live_label`` — so the duplicate-live-slug invariant
(2026-06-21 incident db8dfb62) keeps a DB-level guard now that the conversation
write path targets ``nodes`` only.

Apply via ``juggle doctor`` (behind ``assert_migration_allowed``); never run
directly against the shared prod DB. Idempotent and presence-guarded.
"""
from __future__ import annotations

import logging
import sqlite3

from dbops.node_translation import STATUS_TO_STATE

_log = logging.getLogger(__name__)

# Conversation states that hold a unique, addressable slug (mirrors
# slug_alloc.LIVE_SLUG_STATES mapped into node vocab + threads._LIVE_NODE_STATES).
_LIVE_NODE_STATES = ("open", "running", "background")

_LIVE_LABEL_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_live_label "
    "ON nodes(user_label) WHERE user_label IS NOT NULL "
    "AND kind='conversation' AND state IN ('open','running','background')"
)


def migrate_54_conv_state_parity(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "nodes" not in tables:
        return  # pre-Migration-44 DB; nothing to reconcile

    # (1) Reconcile nodes.state <- STATUS_TO_STATE[threads.status] for divergent
    #     conversation rows (the defect). Per-status UPDATEs keep it index-friendly
    #     and idempotent (the WHERE state!=target no-ops once converged). Guarded on
    #     the legacy `status` column — a very old pre-status threads schema has
    #     nothing to reconcile from.
    if "threads" in tables:
        tcols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
        if "status" in tcols:
            for status, state in STATUS_TO_STATE.items():
                conn.execute(
                    "UPDATE nodes SET state=? "
                    "WHERE kind='conversation' AND state!=? "
                    "AND id IN (SELECT id FROM threads WHERE status=?)",
                    (state, state, status),
                )

    ncols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "user_label" not in ncols:
        return  # pre-parity nodes schema; Migration 50 adds user_label first

    # (2) Repair any duplicate LIVE labels among conversation nodes BEFORE the
    #     unique index is created (a stale 'open' could have masked a reused slug).
    _repair_nodes_dup_live_labels(conn)

    # (3) Enforce the invariant on the node store (best-effort: a residual
    #     duplicate logs and leaves the allocator's skip-live scan as the guard,
    #     exactly like run_migration_slug_wheel's fail-soft index step).
    try:
        conn.execute(_LIVE_LABEL_INDEX)
    except sqlite3.OperationalError as e:
        _log.warning("Migration 54: idx_nodes_live_label not created: %s", e)


def _repair_nodes_dup_live_labels(conn: sqlite3.Connection) -> int:
    """Reassign fresh slugs to live conversation nodes that share a label.

    Keeps the oldest holder of each slug; gives each newer duplicate the first
    free slug off the wheel (2-char then 3-char). Returns the count reassigned.
    """
    from dbops.slug_alloc import _first_free_slug

    ph = ",".join("?" * len(_LIVE_NODE_STATES))
    rows = conn.execute(
        f"SELECT id, user_label FROM nodes WHERE kind='conversation' "
        f"AND user_label IS NOT NULL AND state IN ({ph}) "
        f"ORDER BY user_label, created_at, id",
        _LIVE_NODE_STATES,
    ).fetchall()
    held = {r[1] for r in rows}
    seen: set[str] = set()
    reassigned = 0
    for r in rows:
        lbl = r[1]
        if lbl not in seen:
            seen.add(lbl)
            continue
        new = _first_free_slug(held)
        held.add(new)
        conn.execute("UPDATE nodes SET user_label=? WHERE id=?", (new, r[0]))
        reassigned += 1
    return reassigned
