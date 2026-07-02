"""Persist-only topic-summary cache (migration 46) — L2 DB store + pure decision.

The cockpit `i` modal keeps an in-memory L1 dict; this module adds the durable
L2 layer (`topic_summary_cache`) behind it so summaries survive a cockpit
restart and are regenerated ONLY when the thread actually changed.

Versioning cursor = ``MAX(messages.id)`` for the thread — monotonic, append-only,
never reused (DA-ratified Q2). Persist-only v1: the only actions are EXACT (cursor
unchanged → reuse cached) and FULL (no row / cursor advanced / corrupt → regen).
Incremental is deferred.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone

_log = logging.getLogger("juggle.cockpit")

_SECTION_KEYS = ("context", "why", "what", "result")


# ── pure decision (no DB, no LLM) ────────────────────────────────────────────

def child_node_signature(child_nodes: list[dict] | None) -> str:
    """Stable, order-independent hash of the topic's child task-nodes.

    Fingerprints sorted (id, state, updated_at) triples so ANY node development
    — a state transition, an updated_at touch, or an add/remove — yields a
    different digest and invalidates the cached summary. No children → "" (the
    node dimension contributes nothing to the fingerprint). Pure/deterministic.
    """
    if not child_nodes:
        return ""
    triples = sorted(
        (
            str(n.get("id") or ""),
            str(n.get("state") or ""),
            str(n.get("updated_at") or ""),
        )
        for n in child_nodes
    )
    h = hashlib.sha256()
    for t in triples:
        h.update(("\x1f".join(t) + "\x1e").encode("utf-8"))
    return h.hexdigest()[:16]


def decide_summary_action(
    cached_cursor: int | None,
    current_cursor: int,
    cached_signature: str | None = None,
    current_signature: str = "",
) -> str:
    """Return "EXACT" or "FULL" for the persist-only cache.

    Staleness fingerprint = f(message cursor, child_node_signature):
    - no cached row                         → FULL
    - cursor AND node-signature unchanged   → EXACT (reuse cached, no LLM)
    - cursor advanced (new messages)        → FULL (regenerate)
    - node-signature changed (node dev)     → FULL (regenerate)
    - cursor went backwards                 → FULL (append-only violation; corrupt)
    """
    if cached_cursor is None:
        return "FULL"
    if current_cursor == cached_cursor and (cached_signature or "") == (current_signature or ""):
        return "EXACT"
    return "FULL"


def has_displayable_content(sections: dict | None) -> bool:
    """True iff at least one of the 4 sections has content.

    Mirrors the modal's real `_apply_summary` display gate (any_content) — the
    write-gate must match it so a partial-but-usable summary that WOULD display
    is also cached, while an empty/LLM-failed one is never persisted (R7/B2-Q5).
    """
    if not sections:
        return False
    return any((sections.get(k) or "").strip() for k in _SECTION_KEYS)


# ── L2 DB ops (sqlite3 connection) ───────────────────────────────────────────

def current_cursor(conn: sqlite3.Connection, thread_id: str) -> int:
    """MAX(messages.id) for the thread, or 0 when the thread has no messages."""
    row = conn.execute(
        "SELECT MAX(id) AS m FROM messages WHERE thread_id = ?", (thread_id,)
    ).fetchone()
    val = row["m"] if row is not None else None
    return int(val) if val is not None else 0


def read_summary_cache(conn: sqlite3.Connection, thread_id: str) -> dict | None:
    """Return {last_message_id, sections} for the thread, or None on miss.

    A malformed/legacy `summary_json` is treated as a miss (self-healing → FULL).
    """
    try:
        row = conn.execute(
            "SELECT last_message_id, summary_json, node_signature "
            "FROM topic_summary_cache WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # table/column absent (pre-migration) → miss
    if row is None:
        return None
    try:
        sections = json.loads(row["summary_json"])
    except (ValueError, TypeError):
        return None
    if not isinstance(sections, dict):
        return None
    return {
        "last_message_id": row["last_message_id"],
        "sections": sections,
        "node_signature": row["node_signature"] or "",
    }


def upsert_summary_cache(
    conn: sqlite3.Connection,
    thread_id: str,
    last_message_id: int,
    sections: dict,
    node_signature: str = "",
) -> None:
    """Idempotent UPSERT keyed by thread_id (one row per thread, last-writer-wins)."""
    conn.execute(
        "INSERT INTO topic_summary_cache "
        "(thread_id, last_message_id, summary_json, generated_at, node_signature) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(thread_id) DO UPDATE SET "
        "last_message_id=excluded.last_message_id, "
        "summary_json=excluded.summary_json, "
        "generated_at=excluded.generated_at, "
        "node_signature=excluded.node_signature",
        (
            thread_id,
            int(last_message_id),
            json.dumps(sections),
            datetime.now(timezone.utc).isoformat(),
            node_signature or "",
        ),
    )
    conn.commit()


# ── modal-facing wrappers (JuggleDB; never raise into the UI) ─────────────────

def load_cached_sections(
    db, thread_id: str, fallback_cursor: int, l1: dict, node_signature: str = ""
) -> tuple[dict | None, int]:
    """Resolve the current cursor and return (cached_sections | None, cursor).

    Checks L1 then L2 for an EXACT hit under the node-aware fingerprint
    (cursor, node_signature). On an L2 hit, L1 is back-filled. Any DB error
    degrades to a miss (the modal then regenerates) — never raises.
    """
    cursor = fallback_cursor
    if db is None or not thread_id:
        key = (thread_id, cursor, node_signature)
        return (l1.get(key), cursor)
    try:
        with db._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = current_cursor(conn, thread_id)
            key = (thread_id, cursor, node_signature)
            if key in l1:
                return (l1[key], cursor)
            row = read_summary_cache(conn, thread_id)
        if row and decide_summary_action(
            row["last_message_id"], cursor, row.get("node_signature"), node_signature
        ) == "EXACT":
            l1[key] = row["sections"]
            return (row["sections"], cursor)
    except Exception as e:  # pragma: no cover - defensive: cache must not break UI
        _log.warning("load_cached_sections failed (%s): %s", thread_id, e)
        key = (thread_id, cursor, node_signature)
        return (l1.get(key), cursor)
    return (None, cursor)


def invalidate_summary_cache(db, thread_id: str, l1: dict) -> None:
    """Drop the L2 row and any L1 entries for ``thread_id`` — forces the next
    ``load_cached_sections`` call to MISS (FULL regen). Used by the modal's
    'r' regen key. A DB failure is swallowed (must never break the modal).
    """
    if not thread_id:
        return
    for key in [k for k in l1 if k[0] == thread_id]:
        del l1[key]
    if db is None:
        return
    try:
        with db._connect() as conn:
            conn.execute("DELETE FROM topic_summary_cache WHERE thread_id = ?", (thread_id,))
            conn.commit()
    except Exception as e:  # pragma: no cover - defensive
        _log.warning("invalidate_summary_cache failed (%s): %s", thread_id, e)


def store_summary(
    db, thread_id: str, cursor: int, sections: dict, l1: dict, node_signature: str = ""
) -> None:
    """Persist a freshly generated summary to L2 + L1 — ONLY when displayable.

    R7 / B2-Q5: never write an empty/LLM-failed (no-content) summary to either
    layer, so the next view retries instead of serving garbage. A DB failure is
    swallowed (cache write must never break the modal). Keyed by the node-aware
    fingerprint (cursor, node_signature).
    """
    if not thread_id or not has_displayable_content(sections):
        return
    l1[(thread_id, cursor, node_signature)] = sections
    if db is None:
        return
    try:
        with db._connect() as conn:
            upsert_summary_cache(conn, thread_id, cursor, sections, node_signature)
    except Exception as e:  # pragma: no cover - defensive
        _log.warning("store_summary failed (%s): %s", thread_id, e)
