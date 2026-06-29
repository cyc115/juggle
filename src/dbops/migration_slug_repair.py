"""dbops.migration_slug_repair — pre-index repair of duplicate live labels on the
legacy ``threads`` table (the slug-wheel migration's helper).

Extracted from dbops.slug_alloc (P8 c4-write-cut): this is a MIGRATION-only
operation on the now-frozen ``threads`` table — ``run_migration_slug_wheel`` calls
it before (re)creating the partial unique index ``idx_threads_live_label`` so a
pre-existing duplicate live label can't abort ``CREATE UNIQUE INDEX``. It is NOT
part of slug_alloc's live-allocation path (which now scans ``nodes``), so it lives
with the migration code instead of polluting the steady-state allocator. The
node-store equivalent repair lives in dbops.migration_54_conv_state_parity.
"""
from __future__ import annotations

from dbops.slug_alloc import LIVE_SLUG_STATES, _first_free_slug


def repair_duplicate_live_labels(conn) -> int:
    """Reassign fresh slugs to live threads that share a label. Returns count.

    Before the widened unique index can be (re)created, any pre-existing
    duplicate live labels must be broken or ``CREATE UNIQUE INDEX`` fails.
    Pre-2026-06-21 the narrow index/skip-live omitted 'background', so a live
    background agent could share a slug with a new active thread. Keeps the
    oldest holder of each slug and gives each newer duplicate a free slug.
    """
    ph = ",".join("?" * len(LIVE_SLUG_STATES))
    rows = conn.execute(
        f"SELECT id, user_label FROM threads "
        f"WHERE user_label IS NOT NULL AND status IN ({ph}) "
        f"ORDER BY user_label, created_at, id",
        LIVE_SLUG_STATES,
    ).fetchall()
    held = {r["user_label"] for r in rows}
    seen: set[str] = set()
    reassigned = 0
    for r in rows:
        lbl = r["user_label"]
        if lbl not in seen:
            seen.add(lbl)
            continue
        new = _first_free_slug(held)
        held.add(new)
        conn.execute(
            "UPDATE threads SET user_label = ? WHERE id = ?", (new, r["id"])
        )
        reassigned += 1
    return reassigned
