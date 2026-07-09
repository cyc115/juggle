"""dbops.repair_dup_labels — repair pre-existing duplicate NON-ARCHIVED
conversation labels (2026-07-08 incident).

Before the 2026-07-08 fix, ``next_wheel_slug`` only treated open/running/
background conversations as slug-holding, so a 'done'/'failed-exec' row that
was never archived could have its slug recycled to a brand new conversation —
leaving two non-archived rows sharing a label (prod evidence: label 'TB' held
by a 2026-07-02 'done' row and a 2026-07-08 'background' row simultaneously).
Doctor-safe (idempotent, no-op when there is nothing to repair) — the
orchestrator runs it via ``juggle doctor``, never the coder directly against
the shared DB.
"""
from __future__ import annotations

import sqlite3

from dbops.slug_alloc import _first_free_slug


def repair_duplicate_held_labels(conn: sqlite3.Connection) -> int:
    """Reassign fresh slugs to non-archived conversation nodes that share a
    label. Keeps the oldest holder of each slug; gives each newer duplicate
    the first free slug (2-char wheel, then 3-char). Returns the count
    reassigned."""
    rows = conn.execute(
        "SELECT id, user_label FROM nodes WHERE kind='conversation' "
        "AND user_label IS NOT NULL AND state != 'archived' "
        "ORDER BY user_label, created_at, id"
    ).fetchall()
    held = {r[1] for r in rows}
    seen: set[str] = set()
    reassigned = 0
    for r in rows:
        node_id, lbl = r[0], r[1]
        if lbl not in seen:
            seen.add(lbl)
            continue
        new = _first_free_slug(held)
        held.add(new)
        conn.execute("UPDATE nodes SET user_label=? WHERE id=?", (new, node_id))
        reassigned += 1
    return reassigned
