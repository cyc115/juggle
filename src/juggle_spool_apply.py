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


# Every attribute any replayed cmd_* handler reads MUST be seeded here (or via
# the top-level thread_id) so a partial/empty-args event degrades to the
# handler's own validation (SystemExit → clean dead-letter) instead of an
# AttributeError before it (2026-07-02 incident: agent_complete replay raised
# "'Namespace' object has no attribute 'result_summary'" for empty-args junk
# events). Keys mirror the writer shapes in juggle_cmd_agents*/juggle_cmd_graph;
# test_spool_apply_event_shape pins writer-args ⊆ these defaults ∪ {thread_id}.
_NS_DEFAULTS = dict(
    result_summary=None, error=None, message=None,
    action_id=None, task_id=None, node_id=None, text=None,
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
    elif event.type in ("graph_mark_task", "graph_learn"):
        import juggle_cmd_graph as _g
        {"graph_mark_task": _g.cmd_graph_mark_task,
         "graph_learn": _g.cmd_graph_learn}[event.type](_ns(event))
    else:
        raise ValueError(f"unknown spool event type {event.type!r}")


# A mark/complete-kind replay whose target task/topic has ALREADY reached the
# SAME-sign terminal state the event intends is a SUPERSEDED no-op, NOT a
# dead-letter (2026-07-03 spool 0ecaef08 + 8960b2cc, action items 5226/5227;
# same class as 5210/5218 for agent_complete): the watchdog integrated +
# reconciled the topic to 'verified' before draining the still-pending event,
# so state is already correct and the handoff already recorded. A CROSS-sign
# terminal state (event says --fail but task 'verified', or a success event
# against a failed-* task) CONTRADICTS the event — NOT superseded — and is left
# to the handler, whose terminal-state guard raises → loud dead-letter.
_SUCCESS_TERMINAL = frozenset({"verified", "integrated-unlanded"})
_FAILURE_TERMINAL = frozenset(
    {"failed-exec", "failed-integration", "failed-verify", "blocked-failed"}
)


def _superseded_replay(db, event: SpoolEvent) -> str | None:
    """For a mark/complete-kind event, return a short reason string iff its
    target task/topic already sits in the terminal state the event intended
    (→ journal 'superseded', no dead-letter). Return None for every other case:
    a non-terminal target (handler advances it normally), a cross-sign
    CONTRADICTION (handler raises → loud dead-letter), a missing/unknown target
    (handler's own validation dead-letters), or a non-mark/complete event type.
    NEVER raises — any lookup error yields None so the normal replay path runs
    and a genuine failure still surfaces."""
    try:
        if event.type == "graph_mark_task":
            from dbops import db_graph

            task_id = event.args.get("task_id")
            if not task_id:
                return None
            target = db_graph.get_task(db, task_id)
            if target is None:
                return None
            intends_fail = bool(event.args.get("fail"))
            kind, ident = "task", task_id
        elif event.type in ("agent_complete", "agent_fail"):
            from dbops.db_topics import get_topic_by_thread

            thread_id = event.args.get("thread_id") or event.thread_id
            if not thread_id:
                return None
            target = get_topic_by_thread(db, thread_id)
            if target is None:
                return None
            intends_fail = event.type == "agent_fail"
            kind, ident = "topic", target["id"]
        else:
            return None
    except BaseException:  # a supersede lookup must never mask a real failure
        return None
    state = target["state"]
    want = _FAILURE_TERMINAL if intends_fail else _SUCCESS_TERMINAL
    if state in want:
        return f"{kind} {ident!r} already in terminal state {state!r}; replay superseded"
    return None


def _emit_superseded_notice(db, event: SpoolEvent, reason: str) -> None:
    """Record a watchdog-only DB row for a superseded replay (never pushed).
    Best-effort: a notification failure must not turn a no-op into a dead-letter."""
    try:
        from dbops import event_kinds as _ek
        db.emit_event(
            thread_id=event.thread_id or None,
            message=f"↻ Spool {event.type} {event.uuid[:8]} superseded — {reason}",
            session_id="",
            kind=_ek.WATCHDOG_RECOVERY,
            handled_by="watchdog",
        )
    except BaseException:
        _log.debug("superseded notice emit failed for %s", event.uuid, exc_info=True)


def apply_event(db, event: SpoolEvent) -> tuple[bool, str]:
    """Apply one spool event via the SAME cmd_* handler a direct CLI call would
    use. Returns (ok, message); NEVER lets a BaseException (including the
    replayed CLI handlers' own sys.exit(1) validation branches) escape — see
    DA Resolution #1. Journal-first ('applying' before the handler runs, not
    after) so a real process crash mid-apply is detectable and refused on
    retry rather than silently re-run — see DA Resolution #2."""
    state = _journal_state(db, event.uuid)
    if state in ("applied", "superseded"):
        return True, f"{event.uuid} already applied — skipped"
    if state == "applying":
        return False, (
            f"{event.uuid} ({event.type}) found in 'applying' state — a prior apply "
            "attempt was interrupted mid-flight (process crash). Refusing to blind-retry: "
            "some handlers have non-transactional side effects (e.g. git integrate/push) "
            "that may already be partially applied. Dead-lettered for manual triage."
        )

    superseded = _superseded_replay(db, event)
    if superseded is not None:
        _journal_insert_applying(db, event.uuid, event.type)
        _journal_set_outcome(db, event.uuid, "superseded")
        _emit_superseded_notice(db, event, superseded)
        return True, f"superseded {event.type} {event.uuid}: {superseded}"

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
        elif event.type == "record_orchestration_error":
            # Class B mirror of record_error (T-spool-06b): the captured tool
            # input rides under 'command_args' (a JSON string); surface/juggle_ref
            # carry the offending tool reference for triage.
            db.dedup_or_insert_error(
                signature_hash=a["signature_hash"],
                error_class=a.get("error_class", "B"),
                exc_type=a.get("exc_type"), traceback=a.get("traceback"),
                entrypoint=a.get("entrypoint"),
                command_args=a.get("command_args", "{}"),
                surface=a.get("surface"), juggle_ref=a.get("juggle_ref"),
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
    stats = {"applied": 0, "skipped_dup": 0, "superseded": 0, "dead": 0}
    dead: list[tuple[str, str, str, str]] = []  # (thread_id, uuid, type, msg)
    for event in read_pending(spool_dir()):
        ok, msg = apply_event(db, event)
        if ok:
            if "already applied" in msg:
                stats["skipped_dup"] += 1
            elif msg.startswith("superseded "):
                stats["superseded"] += 1
            else:
                stats["applied"] += 1
            if event.path is not None:
                event.path.unlink(missing_ok=True)
        else:
            stats["dead"] += 1
            if event.path is not None:
                move_to_dead(spool_dir(), event.path, msg)
            dead.append((event.thread_id or "", event.uuid, event.type, msg))
    _file_dead_letter_action_items(db, dead)
    return stats


# A burst of empty/malformed junk events (e.g. the 2026-07-02 empty-args
# agent_complete fixtures) must NOT flood the cockpit with one HIGH action item
# each — cap per-event items and collapse the overflow into a single grouped
# alert so the signal survives without the noise.
_DEAD_ACTION_ITEM_CAP = 3


def _file_dead_letter_action_items(db, dead: list[tuple[str, str, str, str]]) -> None:
    if not dead:
        return
    if len(dead) <= _DEAD_ACTION_ITEM_CAP:
        for thread_id, uuid, etype, msg in dead:
            db.add_action_item(
                thread_id=thread_id or None,
                message=f"⚠️ Spool event {uuid} ({etype}) dead-lettered: {msg}",
                type_="failure",
                priority="high",
            )
        return
    sample = ", ".join(f"{etype}:{uuid[:8]}" for _, uuid, etype, _ in dead[:_DEAD_ACTION_ITEM_CAP])
    db.add_action_item(
        thread_id=None,
        message=(
            f"⚠️ {len(dead)} spool events dead-lettered this drain "
            f"(first {_DEAD_ACTION_ITEM_CAP}: {sample}). See spool/dead/ for full reasons."
        ),
        type_="failure",
        priority="high",
    )
