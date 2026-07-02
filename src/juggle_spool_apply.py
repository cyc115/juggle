"""juggle_spool_apply — watchdog-side spool drain (single-writer broker, T-spool).

Owns: apply_event (one event → the EXISTING cmd_*/db.* write function, with
crash-safe journal-first idempotency and BaseException-safe replay), drain_spool
(the watchdog tick's entry point: read_pending → apply each → journal/dead-letter).
Must not own: spool file format (dbops.spool), the write bodies themselves (reused
verbatim from juggle_cmd_agents*/juggle_cmd_graph — no reimplementation).
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone

from dbops.spool import SpoolEvent, read_pending, move_to_dead
from juggle_spool_paths import spool_dir

_log = logging.getLogger(__name__)


def _journal_state(db, uuid: str) -> str | None:
    with db._connect() as conn:
        row = conn.execute(
            "SELECT outcome FROM spool_journal WHERE uuid = ?", (uuid,)
        ).fetchone()
    return row["outcome"] if row else None


def _journal_insert_applying(db, uuid: str, event_type: str) -> None:
    with db._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO spool_journal(uuid, event_type, applied_at, outcome) "
            "VALUES (?, ?, ?, 'applying')",
            (uuid, event_type, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()


def _journal_set_outcome(db, uuid: str, outcome: str) -> None:
    with db._connect() as conn:
        conn.execute(
            "UPDATE spool_journal SET outcome = ?, applied_at = ? WHERE uuid = ?",
            (outcome, datetime.now(timezone.utc).isoformat(), uuid),
        )
        conn.commit()


_NS_DEFAULTS = dict(
    retain_text=None, open_questions=None, handoff=None, role=None,
    failure_type=None, max_retries=0, recovery_dispatched=False,
    type="manual_step", priority="normal", fail=False, db_path=None,
)


def _ns(event: SpoolEvent) -> argparse.Namespace:
    """Reconstruct the SAME argparse.Namespace the original CLI parser would
    have built: defaults, then the event's top-level thread_id (the CLI's
    positional — some writers keep it there, not in args, e.g. action_notify/
    action_create), then the event args (an args-supplied thread_id, e.g. the
    resolved one on agent_complete, wins over the top-level)."""
    return argparse.Namespace(**{**_NS_DEFAULTS, "thread_id": event.thread_id, **event.args})


def _dispatch(event: SpoolEvent) -> None:
    """Call the ONE matching cmd_* write function for this event type.
    Raises on any failure — apply_event is the sole exception boundary.
    Event-type strings and arg shapes match the committed spool writers
    (juggle_cmd_agents*/juggle_cmd_graph), NOT a parallel reimplementation."""
    a = event.args
    if event.type == "agent_complete":
        from juggle_cmd_agents_complete import cmd_complete_agent
        cmd_complete_agent(_ns(event))
    elif event.type == "agent_fail":
        from juggle_cmd_agents_complete import cmd_fail_agent
        cmd_fail_agent(_ns(event))
    elif event.type == "action_create":
        from juggle_cmd_agents import cmd_request_action
        cmd_request_action(_ns(event))
    elif event.type == "action_ack":
        from juggle_cmd_agents import cmd_ack_action
        cmd_ack_action(_ns(event))
    elif event.type == "action_notify":
        if not a.get("message"):
            raise ValueError("action_notify event missing required 'message'")
        from juggle_cmd_agents import cmd_notify
        cmd_notify(_ns(event))
    elif event.type == "graph_mark_task":
        from juggle_cmd_graph import cmd_graph_mark_task
        cmd_graph_mark_task(_ns(event))
    else:
        raise ValueError(f"unknown spool event type {event.type!r}")


def apply_event(db, event: SpoolEvent) -> tuple[bool, str]:
    """Apply one spool event via the SAME cmd_* handler a direct CLI call would
    use. Returns (ok, message); NEVER lets a BaseException (including the
    replayed CLI handlers' own sys.exit(1) validation branches) escape — see
    DA Resolution #1. Journal-first ('applying' before the handler runs, not
    after) so a real process crash mid-apply is detectable and refused on
    retry rather than silently re-run — see DA Resolution #2."""
    state = _journal_state(db, event.uuid)
    if state == "applied":
        return True, f"{event.uuid} already applied — skipped"
    if state == "applying":
        return False, (
            f"{event.uuid} ({event.type}) found in 'applying' state — a prior apply "
            "attempt was interrupted mid-flight (process crash). Refusing to blind-retry: "
            "some handlers have non-transactional side effects (e.g. git integrate/push) "
            "that may already be partially applied. Dead-lettered for manual triage."
        )

    _journal_insert_applying(db, event.uuid, event.type)
    try:
        a = event.args
        if event.type == "record_error":
            # Arg keys match _spool_error_event (juggle_selfheal.py): the
            # captured context rides under 'command_args' (a JSON string), NOT
            # 'context' — reading the wrong key would silently drop it.
            db.dedup_or_insert_error(
                signature_hash=a["signature_hash"],
                error_class=a.get("error_class", "A"),
                exc_type=a.get("exc_type"), traceback=a.get("traceback"),
                entrypoint=a.get("entrypoint"),
                command_args=a.get("command_args", "{}"),
            )
        else:
            _dispatch(event)
        _journal_set_outcome(db, event.uuid, "applied")
        return True, f"applied {event.type} {event.uuid}"
    except BaseException as exc:  # BaseException: replayed cmd_* handlers use
        # sys.exit(1) for validation (SystemExit is NOT an Exception subclass) —
        # DA Resolution #1. Caught here, at the single replay boundary, and
        # NEVER re-raised: the watchdog tick must survive a poison event.
        _journal_set_outcome(db, event.uuid, "failed")
        kind = type(exc).__name__
        detail = f"code={exc.code}" if isinstance(exc, SystemExit) else str(exc)
        _log.exception("spool apply failed for %s (%s): %s", event.uuid, event.type, kind)
        return False, f"{kind} during replay of {event.type}: {detail}"


def drain_spool(db) -> dict:
    """Watchdog tick entry point: apply every pending spool event in order.

    Applied files are removed by unlink (the file itself is the at-least-once
    signal; spool_journal is the idempotency backstop for the rare case a file
    survives a successful apply, e.g. a crash between apply and unlink)."""
    stats = {"applied": 0, "skipped_dup": 0, "dead": 0}
    for event in read_pending(spool_dir()):
        ok, msg = apply_event(db, event)
        if ok:
            if "already applied" in msg:
                stats["skipped_dup"] += 1
            else:
                stats["applied"] += 1
            if event.path is not None:
                event.path.unlink(missing_ok=True)
        else:
            stats["dead"] += 1
            if event.path is not None:
                move_to_dead(spool_dir(), event.path, msg)
            db.add_action_item(
                thread_id=event.thread_id or None,
                message=f"⚠️ Spool event {event.uuid} ({event.type}) dead-lettered: {msg}",
                type_="failure",
                priority="high",
            )
    return stats
