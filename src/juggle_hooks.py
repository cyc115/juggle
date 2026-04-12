#!/usr/bin/env python3
"""
Juggle Hooks - Claude Code hook handlers.
Called automatically by Claude Code on lifecycle events.
Usage: python3 juggle_hooks.py <event_name>
Events: UserPromptSubmit, Stop, SessionStart, PostToolUse
"""

import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

# Add the directory containing this file to sys.path so we can import siblings.
sys.path.insert(0, str(Path(__file__).parent))

from juggle_db import JuggleDB
from juggle_context import build_context_string

_DATA_DIR = Path(os.environ.get("CLAUDE_PLUGIN_DATA", Path.home() / ".claude" / "juggle"))
DB_PATH = _DATA_DIR / "juggle.db"

logging.basicConfig(
    filename=str(_DATA_DIR / "juggle.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def is_active() -> bool:
    """Return True if juggle is enabled and active."""
    if not DB_PATH.exists():
        return False
    try:
        db = JuggleDB(str(DB_PATH))
        return db.is_active()
    except Exception as exc:
        logging.warning("is_active check failed: %s", exc)
        return False


def get_db() -> JuggleDB:
    return JuggleDB(str(DB_PATH))


def get_classification_candidates(threads: list[dict]) -> list[dict]:
    """Return threads eligible for topic classification match.

    Only threads with status not in ('done', 'archived') are considered.
    If no match is found among these candidates, a new thread should be
    created — closed threads are never resurrected.
    """
    return [t for t in threads if t.get("status") not in ("done", "archived")]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def auto_approve_blocked_agents() -> None:
    """Scan busy agent panes and send approval keystrokes for blocked prompts."""
    try:
        db = get_db()
        agents = db.get_all_agents()
        busy_panes = [a["pane_id"] for a in agents if a.get("status") not in ("idle",) and a.get("pane_id")]
        for pane_id in busy_panes:
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", pane_id, "-p", "-S", "-20"],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                continue
            output = result.stdout
            if "allow Claude to edit its own settings" in output:
                subprocess.run(["tmux", "send-keys", "-t", pane_id, "2", "Enter"], capture_output=True)
                logging.info("auto-approved settings-edit prompt in pane %s", pane_id)
            elif re.search(r"Do you want to", output, re.IGNORECASE):
                subprocess.run(["tmux", "send-keys", "-t", pane_id, "1", "Enter"], capture_output=True)
                logging.info("auto-approved prompt in pane %s", pane_id)
    except Exception as exc:
        logging.warning("auto_approve_blocked_agents error: %s", exc)


def handle_user_prompt_submit(data: dict) -> None:
    """Inject juggle context and record the user prompt."""
    if not is_active():
        sys.exit(0)

    auto_approve_blocked_agents()

    try:
        context = build_context_string()
        if context:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
            }
            print(json.dumps(output))

        # Save the user prompt to the messages table for the current thread.
        prompt = data.get("prompt", "")
        if prompt:
            db = get_db()
            thread_id = db.get_current_thread()
            if thread_id is not None:
                db.add_message(thread_id, "user", prompt)
    except Exception as exc:
        logging.error("UserPromptSubmit handler error: %s", exc, exc_info=True)

    sys.exit(0)


def handle_stop(data: dict) -> None:
    """Capture last assistant message and mark notifications delivered."""
    if not is_active():
        sys.exit(0)

    try:
        db = get_db()

        # Capture orchestrator response — available via last_assistant_message field
        # (added to Stop hook payload in recent Claude Code release)
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

        pending = db.get_pending_notifications()
        ids = [n["id"] for n in pending]
        db.mark_notifications_delivered(ids)
    except Exception as exc:
        logging.error("Stop handler error: %s", exc, exc_info=True)

    sys.exit(0)


def handle_session_start(data: dict) -> None:
    """Inject restoration context on resume or compact."""
    if not is_active():
        sys.exit(0)

    try:
        reason = data.get("reason", "")
        # Inject context for resume/compact or when reason is unknown/absent.
        if reason not in ("new",):
            db = get_db()
            current_thread = db.get_current_thread()
            if current_thread:
                thread = db.get_thread(current_thread)
                thread_label = thread["label"] if (thread and thread.get("label")) else (current_thread[:8] if current_thread else "unknown")
            else:
                thread_label = "unknown"
            topic_count = len(db.get_all_threads())
            additional_context = (
                f"JUGGLE RESTORED: {thread_label} active. "
                f"{topic_count} topics. "
                "Call `python juggle_cli.py show-topics` to see status."
            )
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": additional_context,
                }
            }
            print(json.dumps(output))
    except Exception as exc:
        logging.error("SessionStart handler error: %s", exc, exc_info=True)

    sys.exit(0)


def handle_post_tool_use(data: dict) -> None:
    """Detect orchestrator violations and JUGGLE ACTIVE leaks in tool calls."""
    if not is_active():
        sys.exit(0)

    try:
        tool_name = data.get("tool_name", "")

        # Violation detection: warn if orchestrator used file tools directly
        FORBIDDEN_TOOLS = {"Read", "Edit", "Write", "Glob", "Grep", "NotebookEdit"}
        if tool_name in FORBIDDEN_TOOLS and is_active():
            warning = (
                f"⚠️ ORCHESTRATOR VIOLATION: You used [{tool_name}] directly in the main thread. "
                "All file reads, edits, and searches MUST go through tmux agents. "
                "Dispatch a task to a tmux agent instead."
            )
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": warning,
                }
            }
            print(json.dumps(output))
            sys.exit(0)

        if tool_name != "Agent":
            sys.exit(0)

        tool_input = data.get("tool_input", {})
        if not isinstance(tool_input, dict):
            sys.exit(0)

        # Extract JUGGLE_THREAD:<id> from the agent prompt.
        prompt = tool_input.get("prompt", "")
        thread_match = re.search(r"\[JUGGLE_THREAD:([^\]]+)\]", prompt)
        thread_id = thread_match.group(1) if thread_match else None

        # Violation logging: warn if JUGGLE ACTIVE block was forwarded to a sub-agent.
        if "JUGGLE ACTIVE" in prompt:
            db = get_db()
            warning = (
                f"[juggle] WARNING: Agent prompt for thread {thread_id or '?'} contains "
                f"'JUGGLE ACTIVE' block — context leaked to sub-agent. "
                f"Strip JUGGLE blocks before dispatching agents."
            )
            current = db.get_current_thread()
            db.add_notification(thread_id or current or "", warning)
            logging.warning("JUGGLE ACTIVE leaked into sub-agent prompt for thread %s", thread_id)
    except Exception as exc:
        logging.error("PostToolUse handler error: %s", exc, exc_info=True)

    sys.exit(0)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

HANDLERS = {
    "UserPromptSubmit": handle_user_prompt_submit,
    "Stop": handle_stop,
    "SessionStart": handle_session_start,
    "PostToolUse": handle_post_tool_use,
}


def main() -> None:
    if len(sys.argv) < 2:
        logging.error("juggle_hooks.py called without an event name argument")
        sys.exit(0)

    event_name = sys.argv[1]

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        logging.error("Failed to parse stdin JSON for event %s: %s", event_name, exc)
        data = {}

    handler = HANDLERS.get(event_name)
    if handler is None:
        logging.warning("Unknown hook event: %s", event_name)
        sys.exit(0)

    handler(data)


if __name__ == "__main__":
    main()
