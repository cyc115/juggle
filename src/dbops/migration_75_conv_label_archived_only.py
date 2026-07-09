"""Migration 75 (2026-07-09 incident D1, follow-up to 5e0c8f8/Migration 54) —
widen ``idx_nodes_live_label`` to match the 2026-07-08 Python-level held-slug
invariant (``dbops.slug_alloc._held_labels``: any NON-ARCHIVED conversation
holds its label — only archiving frees it).

WHY: 5e0c8f8 widened the allocator's Python-level held-set scan past
LIVE_NODE_STATES ('open','running','background') to "any state != 'archived'",
but the DB-level uniqueness backstop (idx_nodes_live_label, Migration 54) was
never widened to match — it still only enforces uniqueness among
('open','running','background'). A 'done'/'failed-exec' conversation is NOT
protected by any schema constraint, so a bug or write path that bypasses (or
has a latent gap in) the Python-level check has ZERO backstop — exactly the
2026-07-09 incident shape (two [AA] rows, one 'done' one 'background', with
no IntegrityError ever raised). Widening the index promotes the invariant
from "Python convention" to "structurally enforced" (fail-loud, not fail-soft
— see the repo's Integration pipeline design principle).

Apply via `juggle doctor` (behind assert_migration_allowed); never run
directly against the shared prod DB. Idempotent and presence-guarded.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)

_LIVE_LABEL_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_live_label "
    "ON nodes(user_label) WHERE user_label IS NOT NULL "
    "AND kind='conversation' AND state != 'archived'"
)


def migrate_75_conv_label_archived_only(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "nodes" not in tables:
        return  # pre-Migration-44 DB; nothing to widen

    ncols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    if "user_label" not in ncols:
        return  # pre-parity nodes schema

    # Repair any pre-existing duplicate NON-ARCHIVED labels before tightening
    # the index — a UNIQUE index over duplicate rows raises on creation (same
    # defense Migration 54 applies for the live-only subset).
    try:
        from dbops.repair_dup_labels import repair_duplicate_held_labels
        repair_duplicate_held_labels(conn)
    except sqlite3.OperationalError as e:
        _log.warning("Migration 75: pre-widen dup repair skipped: %s", e)

    try:
        conn.execute("DROP INDEX IF EXISTS idx_nodes_live_label")
        conn.execute(_LIVE_LABEL_INDEX)
        conn.commit()
        _log.info(
            "Migration 75: idx_nodes_live_label widened to state != 'archived'"
        )
    except sqlite3.OperationalError as e:
        _log.warning("Migration 75: idx_nodes_live_label not widened: %s", e)
