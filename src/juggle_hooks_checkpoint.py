"""
juggle_hooks_checkpoint — PreCompact + SessionStart checkpoint handlers.

Owns: _write_checkpoint, _restore_checkpoint, handle_pre_compact,
      handle_session_start, _CHECKPOINT_PATH re-export.
Must not own: tool-use blocking, DB path constants (imported from config).
"""

import json
import logging
import os
import sys
from pathlib import Path

import juggle_hooks_config as _cfg

# Delegate through _cfg so tests can monkeypatch juggle_hooks_config.<name>
# and have the patches take effect here without reloading this module.
_record_error_safe = lambda *a, **k: _cfg._record_error_safe(*a, **k)
_get_session_id = lambda *a, **k: _cfg._get_session_id(*a, **k)


def is_active() -> bool:
    return _cfg.is_active()


def get_db():
    return _cfg.get_db()


def _write_checkpoint(db) -> None:
    """Atomically write orchestrator state snapshot to checkpoint.json."""
    import time

    checkpoint_path = _cfg._CHECKPOINT_PATH  # read at call time (testable)

    session_id = _get_session_id(db)
    thread_id = db.get_current_thread()
    thread_label = None
    if thread_id:
        thread = db.get_thread(thread_id)
        thread_label = thread.get("user_label") if thread else None

    busy = [a for a in db.get_all_agents() if a.get("status") == "busy"]
    in_flight = [
        {
            "agent_id": a["id"],
            "thread_id": a.get("assigned_thread"),
            "dispatched_at": a.get("created_at"),
        }
        for a in busy
    ]

    with db._connect() as conn:
        row = conn.execute("SELECT MAX(id) as m FROM notifications_v2").fetchone()
        cursor = row["m"] if row and row["m"] is not None else 0

    action_items = db.get_open_action_items()
    pending_head = action_items[0]["id"] if action_items else None

    payload = {
        "ts": time.time(),
        "session_id": session_id,
        "active_thread_id": thread_id,
        "active_thread_label": thread_label,
        "in_flight_dispatches": in_flight,
        "notification_cursor": cursor,
        "pending_action_item_head": pending_head,
    }
    tmp = checkpoint_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, checkpoint_path)


def _restore_checkpoint(db) -> str:
    """Return an additionalContext string from the checkpoint, or '' if absent/stale."""
    import time

    checkpoint_path = _cfg._CHECKPOINT_PATH  # read at call time (testable)
    max_age = _cfg._CHECKPOINT_MAX_AGE_SECS

    if not checkpoint_path.exists():
        return ""
    try:
        cp = json.loads(checkpoint_path.read_text())
    except Exception:
        return ""
    age = time.time() - cp.get("ts", 0)
    if age >= max_age:
        return ""
    if cp.get("session_id") != _get_session_id(db):
        return ""

    label = cp.get("active_thread_label") or "?"
    tid = (cp.get("active_thread_id") or "")[:8]
    in_flight = cp.get("in_flight_dispatches", [])
    cursor = cp.get("notification_cursor", 0)
    parts = [f"active=[{label}] {tid}"]
    if in_flight:
        parts.append(f"{len(in_flight)} agent(s) in flight")
    parts.append(f"notification cursor {cursor}")
    return f"\n\n⟳ Resuming after compaction: {', '.join(parts)}."


def handle_session_start(data: dict) -> None:
    """Inject restoration context on resume or compact."""
    # Agent sessions never need the orchestrator's startup dashboard.
    if os.environ.get("JUGGLE_IS_AGENT") == "1":
        sys.exit(0)

    if not is_active():
        db = get_db()
        if db.get_threads_by_status("open"):
            db.set_active(True)
        else:
            sys.exit(0)

    try:
        reason = data.get("reason", "")
        # Inject context for resume/compact or when reason is unknown/absent.
        if reason not in ("new",):
            db = get_db()
            from juggle_context import build_startup_output

            additional_context = build_startup_output(db)
            # Append self-heal pending count
            try:
                from juggle_selfheal import _get_pending_selfheal_count
                pending = _get_pending_selfheal_count(db)
                if pending > 0:
                    additional_context += (
                        f"\n⚠️ {pending} pending self-heal error(s) — "
                        "run `list-selfheal` to review."
                    )
            except Exception:
                pass
            # Restore pre-compaction state if a fresh checkpoint exists.
            try:
                additional_context += _restore_checkpoint(db)
            except Exception:
                pass
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": additional_context,
                }
            }
            print(json.dumps(output))
        # Reap checkpoints older than 24 h regardless of reason.
        try:
            import time as _t
            _cp = _cfg._CHECKPOINT_PATH
            if _cp.exists():
                cp = json.loads(_cp.read_text())
                if _t.time() - cp.get("ts", 0) > 86400:
                    _cp.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.SessionStart")
        logging.error("SessionStart handler error: %s", exc, exc_info=True)

    sys.exit(0)


def handle_pre_compact(data: dict) -> None:
    """Write checkpoint before compaction so SessionStart can restore state."""
    if os.environ.get("JUGGLE_IS_AGENT"):
        sys.exit(0)
    if not is_active():
        sys.exit(0)
    try:
        db = get_db()
        _write_checkpoint(db)
        logging.info("PreCompact: checkpoint written to %s", _cfg._CHECKPOINT_PATH)
        print(json.dumps({"systemMessage": "Checkpointed orchestrator state for compaction"}))
    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.PreCompact")
        logging.error("PreCompact handler error: %s", exc, exc_info=True)
    sys.exit(0)
