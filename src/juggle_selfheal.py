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


def _get_pending_selfheal_count(db) -> int:
    """Return count of actionable error_events. Safe to call even if table absent.

    Mirrors the default list view (selfheal-triage-v2 P1): excludes both
    resolved and the new sticky non_issue so the badge counts only actionable rows.
    """
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM error_events WHERE status NOT IN ('resolved','non_issue')"
            ).fetchone()
            return row[0] if row else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Auto-diagnosis loop — extracted to juggle_selfheal_diagnosis (selfheal-v2 P1).
# Re-exported here so existing `from juggle_selfheal import ...` call sites and
# tests keep working unchanged. New diagnosis-loop logic lives in that module.
# ---------------------------------------------------------------------------
from juggle_selfheal_diagnosis import (  # noqa: E402,F401
    _try_claim_diagnosis_slot,
    get_diagnosis_candidates,
    select_diagnosis_candidate,
    reset_stale_diagnosing_rows,
    purge_expired_selfheal,
    build_diagnosis_prompt,
    _in_flight_exists,
    maybe_dispatch_selfheal_diagnosis,
    _real_dispatch,
)
