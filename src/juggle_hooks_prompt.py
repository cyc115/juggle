"""
juggle_hooks_prompt — UserPromptSubmit and Stop hook handlers.

Owns: handle_user_prompt_submit, handle_stop, auto_approve_blocked_agents,
      Hindsight retention helpers.
Must not own: checkpoint logic, tool-use blocking, DB path constants, or the
autopilot directive (juggle_hooks_autopilot).
"""

import json
import logging
import os
import re
import subprocess
import sys
from juggle_hooks_config import (
    _record_error_safe,
    _get_session_id,
    is_active,
    get_db,
)
from juggle_context import build_context_string
from juggle_hooks_autopilot import (  # noqa: F401 — re-exported for juggle_hooks
    _AUTOPILOT_DIRECTIVE,
    autopilot_context as _autopilot_context,
)
from juggle_hooks_prose import clear_prose_decision, record_prose_decision


# Patterns that are always safe to approve (send "1" = proceed).
_SAFE_APPROVE_PATTERNS = [
    r"Do you want to proceed with", r"Do you want to overwrite",
    r"Do you want to create", r"Would you like to",
]

# Keywords that indicate a destructive action — never auto-approve if present.
_DESTRUCTIVE_KEYWORDS = [
    "delete", "force", "reset", "remove",
    "drop", "destroy", "push to main", "push to master",
]


# Patterns indicating the orchestrator asked for permission instead of acting.
_PERMISSION_ASKING_PATTERNS = [
    r"should i (proceed|dispatch|implement|go ahead|make|fix|run)",
    r"want me to (implement|dispatch|fix|proceed|run|make)",
    r"shall i (implement|dispatch|proceed|fix)",
    r"dispatch the coder\??",
    r"ready to implement\??",
    r"do you want me to",
]



def get_classification_candidates(threads: list[dict]) -> list[dict]:
    """Return threads eligible for topic classification match.

    Only threads with status not in ('done', 'archived') are considered.
    If no match is found among these candidates, a new thread should be
    created — closed threads are never resurrected.
    """
    return [t for t in threads if t.get("status") not in ("done", "archived")]


def auto_approve_blocked_agents() -> None:
    """Scan busy agent panes and send approval keystrokes for blocked prompts."""
    try:
        db = get_db()
        agents = db.get_all_agents()
        busy_panes = [
            a["pane_id"]
            for a in agents
            if a.get("status") not in ("idle",) and a.get("pane_id")
        ]
        for pane_id in busy_panes:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-20"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                continue
            output = result.stdout

            if "allow Claude to edit its own settings" in output:
                subprocess.run(
                    ["tmux", "send-keys", "-t", pane_id, "2", "Enter"],
                    capture_output=True,
                )
                logging.info("auto-approved settings-edit prompt in pane %s", pane_id)
                continue

            output_lower = output.lower()
            if any(kw in output_lower for kw in _DESTRUCTIVE_KEYWORDS):
                logging.info(
                    "skipped auto-approve (destructive keyword) in pane %s", pane_id
                )
                continue

            if any(re.search(p, output, re.IGNORECASE) for p in _SAFE_APPROVE_PATTERNS):
                subprocess.run(
                    ["tmux", "send-keys", "-t", pane_id, "1", "Enter"],
                    capture_output=True,
                )
                logging.info("auto-approved prompt in pane %s", pane_id)
    except Exception as exc:
        logging.warning("auto_approve_blocked_agents error: %s", exc)


def handle_user_prompt_submit(data: dict) -> None:
    """Inject juggle context (plus autopilot directive) and record the user prompt."""
    # Agent sessions: inject ONLY the role anchor (build_context_string returns
    # anchor-only when JUGGLE_IS_AGENT=1). Skip the orchestrator dashboard, the
    # autopilot directive, the agent-pane auto-approve sweep, the thread message
    # write, and Hindsight retention — an agent's prompt is a task, not a user
    # turn, so writing it into orchestrator history (and re-injecting ~2000 tokens
    # of dashboard every turn) is pure waste.
    if os.environ.get("JUGGLE_IS_AGENT") == "1":
        try:
            anchor = build_context_string()
            if anchor:
                print(
                    json.dumps(
                        {
                            "hookSpecificOutput": {
                                "hookEventName": "UserPromptSubmit",
                                "additionalContext": anchor,
                            }
                        }
                    )
                )
        except Exception as exc:
            print(f"[juggle] WARNING: UserPromptSubmit agent error (fail-open): {exc}", file=sys.stderr)
            _record_error_safe(exc, "juggle_hooks.UserPromptSubmit")
            logging.error("UserPromptSubmit agent-anchor error: %s", exc, exc_info=True)
        sys.exit(0)

    autopilot = _autopilot_context()

    # Heartbeat: refresh orchestrator session TTL so a long-running session
    # does not expire mid-work (TTL resets on every prompt from the orchestrator).
    try:
        curr_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
        if curr_sid and is_active():
            db = get_db()
            if db.get_orchestrator_session_id() == curr_sid:
                db.touch_orchestrator_session_ts()
    except Exception:
        pass

    # Autopilot is independent of juggle mode — re-assert it even when inactive.
    if not is_active():
        if autopilot:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": autopilot,
                        }
                    }
                )
            )
        sys.exit(0)

    auto_approve_blocked_agents()

    try:
        context = build_context_string()
        combined = "\n\n".join(part for part in (autopilot, context) if part)
        if combined:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": combined,
                }
            }
            print(json.dumps(output))

        db = get_db()
        # Auto-ack: the user's reply answers the most recent prose decision
        # surfaced on the prior Stop (mirrors clear_askuser_decision).
        clear_prose_decision(db)

        # Save the user prompt to the messages table for the current thread.
        prompt = data.get("prompt", "")
        if prompt:
            thread_id = db.get_current_thread()
            if thread_id is not None:
                db.add_message(thread_id, "user", prompt)
    except Exception as exc:
        print(f"[juggle] WARNING: UserPromptSubmit error (fail-open): {exc}", file=sys.stderr)
        _record_error_safe(exc, "juggle_hooks.UserPromptSubmit")
        logging.error("UserPromptSubmit handler error: %s", exc, exc_info=True)

    sys.exit(0)


def handle_stop(data: dict, scan_class_b_fn) -> None:
    """Capture last assistant message and mark notifications delivered."""
    if not is_active():
        sys.exit(0)

    try:
        db = get_db()

        # Capture orchestrator response — available via last_assistant_message field
        last_msg = data.get("last_assistant_message", "").strip()
        if last_msg and len(last_msg) > 10:
            thread_id = db.get_current_thread()
            if thread_id is not None:
                db.add_message(thread_id, "assistant", last_msg)
                logging.info(
                    "Stop: captured assistant message for thread %s (%d chars)",
                    thread_id,
                    len(last_msg),
                )

                # Violation: orchestrator asked for permission instead of acting
                if any(
                    re.search(p, last_msg, re.IGNORECASE)
                    for p in _PERMISSION_ASKING_PATTERNS
                ):
                    db.add_notification_v2(
                        thread_id,
                        "⚠️ ORCHESTRATOR: You asked for permission instead of acting. "
                        "Clear fixes → dispatch immediately. Only gate on genuine design "
                        "decisions via AskUserQuestion.",
                        session_id=_get_session_id(db),
                    )
                    logging.warning(
                        "Stop: permission-asking detected in thread %s", thread_id
                    )

                # Prose decision/advisory ("your call", "say X to proceed", …):
                # no tool call fired, so mirror the AskUserQuestion bridge and
                # auto-file a [auto-decision] action item (deduped).
                record_prose_decision(db, last_msg)
        # Class B: scan transcript for Juggle-caused tool errors
        scan_class_b_fn(data)
        # Clean up pre-compaction checkpoint on normal session end.
        from juggle_hooks_config import _CHECKPOINT_PATH as _CP
        try:
            _CP.unlink(missing_ok=True)
        except Exception:
            pass
    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.Stop")
        logging.error("Stop handler error: %s", exc, exc_info=True)

    sys.exit(0)
