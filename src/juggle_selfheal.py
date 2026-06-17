"""Juggle Self-Heal — captures Juggle-caused errors for gated diagnosis."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sysconfig
import traceback as _tb
from datetime import datetime, timedelta, timezone
from pathlib import Path

from juggle_settings import get_settings  # noqa: E402 — after stdlib imports

_log = logging.getLogger(__name__)
_SELFHEAL_ENV = "JUGGLE_SELFHEAL_OP"

_ALLOWLISTED_TYPES = (SystemExit, KeyboardInterrupt)


def _is_allowlisted(exc: BaseException) -> bool:
    if isinstance(exc, _ALLOWLISTED_TYPES):
        return True
    import sqlite3
    if isinstance(exc, sqlite3.OperationalError):
        if "database is locked" in str(exc).lower():
            return True
    return False


def _is_stdlib(filename: str) -> bool:
    stdlib_paths = [sysconfig.get_path("stdlib"), sysconfig.get_path("platstdlib")]
    return any(p and filename.startswith(p) for p in stdlib_paths)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _get_db():
    from juggle_db import JuggleDB, DB_PATH
    db = JuggleDB(str(DB_PATH))
    db.init_db()
    return db


def _compute_class_a_signature(exc: BaseException, entrypoint: str) -> str:
    exc_type = type(exc).__name__
    frames = _tb.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    normalized = []
    for frame in frames:
        if _is_stdlib(frame.filename) or "site-packages" in frame.filename:
            continue
        fname = Path(frame.filename).name
        # Only Juggle source files — avoids test-path line-number pollution
        if not fname.startswith("juggle_"):
            continue
        normalized.append(f"{fname}:{frame.lineno}:{frame.name}")
    normalized = normalized[-5:]
    frames_str = "|".join(normalized) or entrypoint
    sig_input = f"class_A:{exc_type}:{frames_str}"
    return hashlib.sha256(sig_input.encode()).hexdigest()[:16]


def _compute_class_b_signature(tool: str, error_text: str, juggle_ref: str) -> str:
    normalized_err = re.sub(r"\d+", "", error_text[:120].lower())
    normalized_err = re.sub(r"\s+", " ", normalized_err).strip()
    ref_basename = Path(juggle_ref).name if "/" in juggle_ref else juggle_ref.split(":")[0]
    sig_input = f"class_B:{tool}:{normalized_err}:{ref_basename}"
    return hashlib.sha256(sig_input.encode()).hexdigest()[:16]


def record_error(exc: BaseException, entrypoint: str, context: dict | None = None) -> None:
    """Capture a Class A exception. Never re-raises. Self-protecting."""
    if os.environ.get(_SELFHEAL_ENV):
        return
    try:
        if _is_allowlisted(exc):
            return
        sig = _compute_class_a_signature(exc, entrypoint)
        full_tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
        db = _get_db()
        os.environ[_SELFHEAL_ENV] = "1"
        try:
            db.dedup_or_insert_error(
                signature_hash=sig,
                error_class="A",
                exc_type=type(exc).__name__,
                traceback=full_tb,
                entrypoint=entrypoint,
                command_args=json.dumps(context or {}),
            )
        finally:
            os.environ.pop(_SELFHEAL_ENV, None)
    except Exception as inner:
        _log.error("selfheal.record_error itself failed: %s", inner)


def record_orchestration_error(
    tool: str,
    tool_input: dict,
    error_text: str,
    juggle_ref: str,
) -> None:
    """Capture a Class B tool error. Never re-raises. Self-protecting."""
    if os.environ.get(_SELFHEAL_ENV):
        return
    try:
        sig = _compute_class_b_signature(tool, error_text, juggle_ref)
        db = _get_db()
        os.environ[_SELFHEAL_ENV] = "1"
        try:
            db.dedup_or_insert_error(
                signature_hash=sig,
                error_class="B",
                exc_type=None,
                traceback=error_text,
                entrypoint=tool,
                command_args=json.dumps(tool_input),
                surface=juggle_ref,
                juggle_ref=juggle_ref,
            )
        finally:
            os.environ.pop(_SELFHEAL_ENV, None)
    except Exception as inner:
        _log.error("selfheal.record_orchestration_error itself failed: %s", inner)


def _try_claim_diagnosis_slot(db, error_event_id: int) -> bool:
    """Atomically claim the diagnosis slot. Returns True if claimed."""
    with db._connect() as conn:
        in_flight = conn.execute(
            "SELECT COUNT(*) FROM error_events WHERE status = 'diagnosing'"
        ).fetchone()[0]
        if in_flight > 0:
            return False
        cur = conn.execute(
            "UPDATE error_events SET status = 'diagnosing' WHERE id = ? AND status = 'open'",
            (error_event_id,),
        )
        conn.commit()
        return cur.rowcount == 1


def _get_pending_selfheal_count(db) -> int:
    """Return count of non-resolved error_events. Safe to call even if table absent."""
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM error_events WHERE status != 'resolved'"
            ).fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Auto-diagnosis loop — Tasks 2-8
# ---------------------------------------------------------------------------

def get_diagnosis_candidates(db, min_count: int = 3) -> list[dict]:
    """Return open class-A error rows with count >= min_count, ordered count DESC."""
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM error_events "
            "WHERE status='open' AND error_class='A' AND count >= ? "
            "ORDER BY count DESC",
            (min_count,),
        ).fetchall()
    return [dict(r) for r in rows]


def select_diagnosis_candidate(
    rows: list[dict],
    *,
    in_flight_exists: bool,
    enabled: bool,
) -> dict | None:
    """Pure gate: return the top candidate row or None.

    Returns None when disabled, in-flight diagnosis exists, or no rows.
    """
    if not enabled or in_flight_exists or not rows:
        return None
    return rows[0]


def reset_stale_diagnosing_rows(db, now: datetime, staleness_secs: int = 270) -> int:
    """Reset rows stuck in 'diagnosing' beyond staleness_secs back to 'open'.

    Returns count of rows reset. Deterministic with injected now.
    """
    cutoff = (now - timedelta(seconds=staleness_secs)).strftime("%Y-%m-%d %H:%M")
    with db._connect() as conn:
        cur = conn.execute(
            "UPDATE error_events SET status='open' "
            "WHERE status='diagnosing' AND last_seen < ?",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount


def purge_expired_selfheal(db, now: datetime, retention_days: int = 14) -> int:
    """Delete error_events rows whose last_seen is older than retention_days.

    Returns count of deleted rows. Deterministic with injected now.
    """
    cutoff = (now - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M")
    with db._connect() as conn:
        # Only purge resolved/open rows — never purge diagnosing/awaiting_approval
        # rows that may still be actively in-flight.
        cur = conn.execute(
            "DELETE FROM error_events WHERE last_seen < ? AND status IN ('open', 'resolved')",
            (cutoff,),
        )
        conn.commit()
        return cur.rowcount


def build_diagnosis_prompt(row: dict) -> str:
    """Pure prompt builder for a diagnosis coder agent.

    Carries all fields the agent needs for RCA + fix; instructs operator-gated
    workflow (request-action HIGH). Never raises.
    """
    sig = row.get("signature_hash") or ""
    exc = row.get("exc_type") or "unknown"
    ep = row.get("entrypoint") or "unknown"
    tb = (row.get("traceback") or "").strip()
    args = row.get("command_args") or "{}"
    count = row.get("count") or 0
    first = row.get("first_seen") or ""
    last = row.get("last_seen") or ""
    surface = row.get("surface") or ""

    return (
        f"## Juggle Self-Heal Diagnosis Task\n\n"
        f"**Signature**: `{sig}`\n"
        f"**Exception type**: `{exc}`\n"
        f"**Entrypoint**: `{ep}`\n"
        f"**Surface**: `{surface}`\n"
        f"**Occurrence count**: {count} (first: {first}, last: {last})\n"
        f"**Command args**: `{args}`\n\n"
        f"### Traceback\n```\n{tb}\n```\n\n"
        f"### Your task\n"
        f"1. RCA: identify the root cause of this recurring exception.\n"
        f"2. Implement a minimal fix on a new branch (TDD: RED→GREEN). "
        f"Do NOT auto-merge the fix — this is operator-gated.\n"
        f"3. When done, FINISH WITH: `request-action HIGH` summarizing "
        f"the RCA and your branch name so the operator can review and merge.\n\n"
        f"**IMPORTANT**: Do not self-merge. Operator approval is required before "
        f"any fix lands in main. Never auto-merge self-heal fixes.\n"
    )


def _in_flight_exists(db) -> bool:
    with db._connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM error_events WHERE status='diagnosing'"
        ).fetchone()[0]
    return n > 0


def maybe_dispatch_selfheal_diagnosis(db, dispatch_fn=None) -> bool:
    """Claim a diagnosis slot and dispatch a coder agent if conditions are met.

    Returns True if a dispatch was attempted.
    Sets JUGGLE_SELFHEAL_OP during dispatch to prevent recursive capture.
    dispatch_fn(db, thread_id, prompt) — injectable for testing.
    """
    cfg = get_settings().get("selfheal", {})
    enabled = bool(cfg.get("enabled", False))
    min_count = int(cfg.get("min_count", 3))

    # Reset stale diagnosing rows BEFORE checking in-flight — otherwise a crash
    # mid-dispatch leaves the row stuck forever, permanently blocking new dispatch.
    reset_stale_diagnosing_rows(db, datetime.now(timezone.utc))

    candidates = get_diagnosis_candidates(db, min_count=min_count)
    in_flight = _in_flight_exists(db)
    row = select_diagnosis_candidate(candidates, in_flight_exists=in_flight, enabled=enabled)
    if row is None:
        return False

    event_id = row["id"]
    if not _try_claim_diagnosis_slot(db, event_id):
        return False

    prompt = build_diagnosis_prompt(row)

    if dispatch_fn is None:
        dispatch_fn = _real_dispatch

    os.environ[_SELFHEAL_ENV] = "1"
    try:
        try:
            with db._connect() as _conn:
                session_id = db._get_session_key(_conn, "session_id") or ""
        except Exception:
            session_id = ""
        thread_id = db.create_thread(
            f"[selfheal] diagnose {row.get('exc_type','?')} sig={row.get('signature_hash','')[:8]}",
            session_id=session_id,
        )
        dispatch_fn(db, thread_id, prompt)
        db.set_error_event_status(event_id, "awaiting_approval")
    except Exception as e:
        _log.error("selfheal dispatch failed: %s", e)
        db.set_error_event_status(event_id, "open")
        return False
    finally:
        os.environ.pop(_SELFHEAL_ENV, None)

    return True


def _real_dispatch(db, thread_id: str, prompt: str) -> None:
    """Dispatch via the same pool path as graph_tick."""
    import tempfile
    from juggle_graph_dispatch import _dispatch_via_pool

    task = {"id": f"selfheal-{thread_id[:8]}"}
    _dispatch_via_pool(db, thread_id, prompt, task)
