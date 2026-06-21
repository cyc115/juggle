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

import json
import logging
import sqlite3
from datetime import datetime, timezone

_log = logging.getLogger("juggle.cockpit")

_SECTION_KEYS = ("context", "why", "what", "result")


# ── pure decision (no DB, no LLM) ────────────────────────────────────────────

def decide_summary_action(cached_cursor: int | None, current_cursor: int) -> str:
    """Return "EXACT" or "FULL" for the persist-only cache.

    - no cached row            → FULL
    - cursor unchanged         → EXACT (reuse cached, no LLM)
    - cursor advanced          → FULL (thread gained messages → regenerate)
    - cursor went backwards    → FULL (impossible under append-only; corruption)
    """
    if cached_cursor is None:
        return "FULL"
    if current_cursor == cached_cursor:
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
            "SELECT last_message_id, summary_json FROM topic_summary_cache "
            "WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None  # table absent (pre-migration) → miss
    if row is None:
        return None
    try:
        sections = json.loads(row["summary_json"])
    except (ValueError, TypeError):
        return None
    if not isinstance(sections, dict):
        return None
    return {"last_message_id": row["last_message_id"], "sections": sections}


def upsert_summary_cache(
    conn: sqlite3.Connection, thread_id: str, last_message_id: int, sections: dict
) -> None:
    """Idempotent UPSERT keyed by thread_id (one row per thread, last-writer-wins)."""
    conn.execute(
        "INSERT INTO topic_summary_cache "
        "(thread_id, last_message_id, summary_json, generated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(thread_id) DO UPDATE SET "
        "last_message_id=excluded.last_message_id, "
        "summary_json=excluded.summary_json, "
        "generated_at=excluded.generated_at",
        (
            thread_id,
            int(last_message_id),
            json.dumps(sections),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


# ── modal-facing wrappers (JuggleDB; never raise into the UI) ─────────────────

def load_cached_sections(
    db, thread_id: str, fallback_cursor: int, l1: dict
) -> tuple[dict | None, int]:
    """Resolve the current cursor and return (cached_sections | None, cursor).

    Checks L1 then L2 for an EXACT hit. On an L2 hit, L1 is back-filled. Any DB
    error degrades to a miss (the modal then regenerates) — never raises.
    """
    cursor = fallback_cursor
    if db is None or not thread_id:
        key = (thread_id, cursor)
        return (l1.get(key), cursor)
    try:
        with db._connect() as conn:
            conn.row_factory = sqlite3.Row
            cursor = current_cursor(conn, thread_id)
            key = (thread_id, cursor)
            if key in l1:
                return (l1[key], cursor)
            row = read_summary_cache(conn, thread_id)
        if row and decide_summary_action(row["last_message_id"], cursor) == "EXACT":
            l1[key] = row["sections"]
            return (row["sections"], cursor)
    except Exception as e:  # pragma: no cover - defensive: cache must not break UI
        _log.warning("load_cached_sections failed (%s): %s", thread_id, e)
        key = (thread_id, cursor)
        return (l1.get(key), cursor)
    return (None, cursor)


def store_summary(db, thread_id: str, cursor: int, sections: dict, l1: dict) -> None:
    """Persist a freshly generated summary to L2 + L1 — ONLY when displayable.

    R7 / B2-Q5: never write an empty/LLM-failed (no-content) summary to either
    layer, so the next view retries instead of serving garbage. A DB failure is
    swallowed (cache write must never break the modal).
    """
    if not thread_id or not has_displayable_content(sections):
        return
    l1[(thread_id, cursor)] = sections
    if db is None:
        return
    try:
        with db._connect() as conn:
            upsert_summary_cache(conn, thread_id, cursor, sections)
    except Exception as e:  # pragma: no cover - defensive
        _log.warning("store_summary failed (%s): %s", thread_id, e)
