"""Juggle Self-Heal — tick-driven auto-diagnosis loop.

Extracted from juggle_selfheal.py (selfheal-triage-v2 P1) so the capture module
stays under the ≤300-line architecture gate and the v2 triage wiring has room
to grow. Owns: diagnosis-slot claiming, candidate selection/ordering, stale-row
reset, retention purge, prompt building, and the dispatch tick.
Must not own: error capture (juggle_selfheal.py) or pure triage logic
(selfheal_triage.py).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from juggle_settings import get_settings  # noqa: E402 — after stdlib imports

_log = logging.getLogger(__name__)
# Same sentinel as juggle_selfheal._SELFHEAL_ENV — guards against recursive
# capture while the diagnoser itself runs. Literal duplicated (not imported)
# to keep this module's dependency on juggle_selfheal one-directional.
_SELFHEAL_ENV = "JUGGLE_SELFHEAL_OP"


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


def get_diagnosis_candidates(db, min_count: int = 3) -> list[dict]:
    """Return open class-A AND class-B error rows with count >= min_count,
    ordered by real-bug signal strength (anti-starvation), not raw count.

    selfheal-triage-v2 P1 (spec §4.3): the diagnoser now eats B-class too;
    a strong-signal low-count bug must not starve behind a high-count noise group.
    """
    from selfheal_triage import order_candidates
    with db._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM error_events "
            "WHERE status='open' AND count >= ? ",
            (min_count,),
        ).fetchall()
    return order_candidates([dict(r) for r in rows])


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
        f"### If this is actually BENIGN (transient/non-bug)\n"
        f"Do NOT silently hide it. Instead finish with: "
        f"`juggle selfheal-propose-nonissue {row.get('id','?')}` — this sets the "
        f"row to `non_issue_proposed` (VISIBLE, greyed) for one-click operator "
        f"confirmation to `non_issue`. Only the operator turns a proposal into a "
        f"sticky non_issue. (selfheal-triage-v2 P1 — no silent auto-hide.)\n\n"
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

    # Deterministic allowlist sweep BEFORE candidate selection (spec §4.3 tier 1).
    # The only silent-hide path in P1; every suppression is audit-logged.
    if cfg.get("allowlist_sweep_enabled", True):
        from selfheal_triage import classify_allowlist, ALLOWLIST_VERSION
        for s in db.sweep_allowlist_to_nonissue(classify_allowlist, ALLOWLIST_VERSION):
            _log.info(
                "selfheal.allowlist sweep id=%s rule=%s v%s sig=%s",
                s["id"], s["rule_id"], ALLOWLIST_VERSION, (s["signature_hash"] or "")[:8],
            )
        # Re-surface valve (spec §4.4): a mis-classified non_issue re-alerts.
        rs = db.resurface_nonissue_rows(
            datetime.now(timezone.utc),
            surge_count=int(cfg.get("resurface_surge_count", 20)),
            absolute_count=int(cfg.get("resurface_absolute_count", 100)),
            lease_days=int(cfg.get("resurface_lease_days", 30)),
        )
        for r in rs:
            _log.info("selfheal.resurface id=%s reason=%s sig=%s",
                      r["id"], r["reason"], (r["signature_hash"] or "")[:8])

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
    from juggle_graph_dispatch import _dispatch_via_pool

    task = {"id": f"selfheal-{thread_id[:8]}"}
    _dispatch_via_pool(db, thread_id, prompt, task)
