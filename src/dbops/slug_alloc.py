"""dbops.slug_alloc — atomic two/three-letter slug allocation off the wheel.

Extracted from dbops.threads (2026-06-21 incident) to:
  (a) widen the LIVE-state set to include 'background' — a live background
      agent must hold a unique, addressable slug, same as 'active'/'running';
  (b) add graceful 3-char widening when the 676-slot 2-char wheel is full of
      live threads, instead of crashing the create path;
  (c) repair pre-existing duplicate live labels before the widened unique
      index is (re)created;
and to keep dbops.threads under the LOC budget.

The functions take a raw sqlite3 connection. Callers that allocate a new slug
MUST already hold the write lock (BEGIN IMMEDIATE) so the read-modify-write of
``label_seq`` and the live-set scan are serialized across processes.
"""

from __future__ import annotations

from dbops.schema import WHEEL_SIZE, _slug_from_wheel

# States whose threads are LIVE and therefore must hold a unique, addressable
# slug. MUST stay in lock-step with the partial unique index
# ``idx_threads_live_label`` and dbops.threads._OPEN_THREAD_STATES. 'background'
# was historically omitted, which let a live background agent share a slug with
# a new active thread (2026-06-21 duplicate-label incident).
LIVE_SLUG_STATES = ("active", "running", "background")

# Node-vocab equivalents of LIVE_SLUG_STATES (P8 c4-write-cut): the live-label
# scan resolves from the authoritative kind='conversation' nodes, whose `state`
# column uses node vocab ('active'≡'open' via the bijective map). MUST stay in
# lock-step with the partial unique index idx_nodes_live_label (Migration 54).
LIVE_NODE_STATES = ("open", "running", "background")

_THREE_CHAR_SPACE = 26 ** 3


def _live_labels(conn) -> set[str]:
    """Slugs currently held by a LIVE conversation node (P8 c4-write-cut: reads
    nodes, the sole conversation store, not the retired threads table)."""
    ph = ",".join("?" * len(LIVE_NODE_STATES))
    return {
        r["user_label"]
        for r in conn.execute(
            f"SELECT user_label FROM nodes WHERE kind='conversation' "
            f"AND user_label IS NOT NULL AND state IN ({ph})",
            LIVE_NODE_STATES,
        ).fetchall()
    }


def next_wheel_slug(conn) -> str:
    """Allocate the next free slug, atomically advancing ``label_seq``.

    Skips slugs held by any LIVE thread (LIVE_SLUG_STATES). On 2-char
    exhaustion (all 676 AA..ZZ held by live threads) widens to a 3-letter slug
    — graceful degradation, never crashes. The caller must hold the write lock.
    """
    row = conn.execute(
        "SELECT value FROM juggle_meta WHERE key = 'label_seq'"
    ).fetchone()
    seq = int(row["value"]) if row and row["value"] is not None else 0
    live = _live_labels(conn)
    for _ in range(WHEEL_SIZE):
        slug = _slug_from_wheel(seq)
        seq += 1
        if slug not in live:
            conn.execute(
                "INSERT INTO juggle_meta(key, value) VALUES ('label_seq', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(seq),),
            )
            return slug
    return _next_wide_slug(live)


def _wide_slug(i: int) -> str:
    """Map 0..17575 to a 3-letter slug AAA..ZZZ."""
    return (
        chr(65 + i // 676)
        + chr(65 + (i // 26) % 26)
        + chr(65 + i % 26)
    )


def _next_wide_slug(live: set[str]) -> str:
    """First 3-letter slug (AAA..ZZZ) not held by a live thread.

    Backstop for when all 676 two-letter slots are held by LIVE threads. With
    any sane MAX_THREADS the 2-char space never fills; this exists so the
    create path degrades gracefully. ``label_seq`` (the 2-char wheel pointer)
    is intentionally left untouched — normal allocation resumes once a 2-char
    slug frees up.
    """
    for i in range(_THREE_CHAR_SPACE):
        slug = _wide_slug(i)
        if slug not in live:
            return slug
    raise RuntimeError(
        "slug space exhausted: all 2- and 3-letter slugs held by live threads"
    )


def _first_free_slug(held: set[str]) -> str:
    """First slug (2-char wheel, then 3-char) not in ``held``."""
    for i in range(WHEEL_SIZE):
        slug = _slug_from_wheel(i)
        if slug not in held:
            return slug
    return _next_wide_slug(held)


# repair_duplicate_live_labels (the legacy-threads-index repair) moved to
# dbops.migration_slug_repair — it is a migration-only operation on the frozen
# `threads` table (the slug-wheel migration's pre-index repair), not part of the
# live allocation path that slug_alloc owns.
