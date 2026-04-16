#!/usr/bin/env python3
"""
Juggle Hooks - Claude Code hook handlers.
Called automatically by Claude Code on lifecycle events.
Usage: python3 juggle_hooks.py <event_name>
Events: UserPromptSubmit, Stop, SessionStart, PostToolUse
"""

import json
import logging
import re
import subprocess
import sys
import threading
from pathlib import Path

# Add the directory containing this file to sys.path so we can import siblings.
sys.path.insert(0, str(Path(__file__).parent))

from juggle_db import JuggleDB
from juggle_context import build_context_string
from juggle_settings import get_settings as _get_settings

_DATA_DIR = Path(_get_settings()["paths"]["data_dir"])
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


_FINANCE_KEYWORDS = re.compile(
    r"\$|dollar|account|routing|payment|tax|irs|refund|balance|transfer|invest|fund|\bira\b|hsa|401k|income|wage|salary|credit|debit",
    re.IGNORECASE,
)
_IDENTITY_KEYWORDS = re.compile(
    r"ssn|license|passport|\bdob\b|date of birth|social security|id number|\bein\b|\btin\b|driver",
    re.IGNORECASE,
)
_ACCOMPLISHMENT_KEYWORDS = re.compile(
    r"\b(filed|completed|finished|done|submitted|launched|shipped|achieved|accomplished)\b",
    re.IGNORECASE,
)
_PREFERENCE_KEYWORDS = re.compile(
    r"\b(prefer|always|never|don't|do not|remember|stop|start|like|dislike|want|need)\b",
    re.IGNORECASE,
)

_CORRECTION_PATTERNS = re.compile(
    r"\b(actually|no it\'s|no,? it\'s|wrong,? it\'s|should be|i meant|to clarify|the correct|it should|correction)\b",
    re.IGNORECASE,
)


def _classify_context(text: str) -> str:
    """Classify text into a Hindsight context tag."""
    if _ACCOMPLISHMENT_KEYWORDS.search(text):
        return "accomplishment"
    if _PREFERENCE_KEYWORDS.search(text):
        return "preference"
    if _IDENTITY_KEYWORDS.search(text):
        return "identity"
    if _FINANCE_KEYWORDS.search(text):
        return "finance"
    return "conversation"


def _retain_conversation_turn(role: str, content: str, topic: str, context_override: str | None = None) -> None:
    """Fire-and-forget: retain a conversation turn to Hindsight."""
    if len(content.strip()) < 20:
        return
    try:
        from juggle_hindsight import HindsightClient
        client = HindsightClient.from_config()
        if client is None:
            return
        text = f"[{topic}] ({role}) {content}"
        if len(text) > 10_000:
            text = text[:10_000]
        context = context_override if context_override is not None else _classify_context(content)
        client.retain(text, context=context)
    except Exception:
        pass


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

# Patterns that are always safe to approve (send "1" = proceed).
# Nothing containing destructive keywords is auto-approved.
_SAFE_APPROVE_PATTERNS = [
    r"Do you want to proceed with",
    r"Do you want to overwrite",
    r"Do you want to create",
    r"Would you like to",
]

# Keywords that indicate a destructive action — never auto-approve if present.
_DESTRUCTIVE_KEYWORDS = [
    "delete", "force", "reset", "remove", "drop", "destroy",
    "push to main", "push to master",
]


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
                continue

            output_lower = output.lower()
            if any(kw in output_lower for kw in _DESTRUCTIVE_KEYWORDS):
                logging.info("skipped auto-approve (destructive keyword) in pane %s", pane_id)
                continue

            if any(re.search(p, output, re.IGNORECASE) for p in _SAFE_APPROVE_PATTERNS):
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
        # Increment delivery attempts for pending notifications before building context
        db = get_db()
        db.increment_delivery_attempts()

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
                thread = db.get_thread(thread_id)
                topic = thread.get("topic", "") if thread else ""
                forced_ctx = "preferences" if _CORRECTION_PATTERNS.search(prompt) else None
                threading.Thread(
                    target=_retain_conversation_turn,
                    args=("user", prompt, topic, forced_ctx),
                    daemon=True,
                ).start()
    except Exception as exc:
        logging.error("UserPromptSubmit handler error: %s", exc, exc_info=True)

    sys.exit(0)


# Patterns indicating the orchestrator asked for permission instead of acting.
_PERMISSION_ASKING_PATTERNS = [
    r"should i (proceed|dispatch|implement|go ahead|make|fix|run)",
    r"want me to (implement|dispatch|fix|proceed|run|make)",
    r"shall i (implement|dispatch|proceed|fix)",
    r"dispatch the coder\??",
    r"ready to implement\??",
    r"do you want me to",
]


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

                # Auto-retain assistant response to Hindsight
                thread = db.get_thread(thread_id)
                topic = thread.get("topic", "") if thread else ""
                threading.Thread(
                    target=_retain_conversation_turn,
                    args=("assistant", last_msg, topic),
                    daemon=True,
                ).start()

                # Violation: orchestrator asked for permission instead of acting
                if any(re.search(p, last_msg, re.IGNORECASE) for p in _PERMISSION_ASKING_PATTERNS):
                    db.add_notification(
                        thread_id,
                        "⚠️ ORCHESTRATOR: You asked for permission instead of acting. "
                        "Clear fixes → dispatch immediately. Only gate on genuine design "
                        "decisions via AskUserQuestion.",
                        severity="warning",
                    )
                    logging.warning("Stop: permission-asking detected in thread %s", thread_id)

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
            from juggle_context import build_startup_output
            additional_context = build_startup_output(db)
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


def handle_pre_tool_use(data: dict) -> None:
    """Hard-block Edit/Write/NotebookEdit in the orchestrator main thread."""
    if not is_active():
        sys.exit(0)

    try:
        tool_name = data.get("tool_name", "")
        BLOCKED_TOOLS = {"Edit", "Write", "NotebookEdit"}
        if tool_name in BLOCKED_TOOLS:
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "decision": "block",
                    "reason": (
                        f"Orchestrator cannot use {tool_name} directly. "
                        "Dispatch to a tmux agent instead."
                    ),
                }
            }
            print(json.dumps(output))
    except Exception as exc:
        logging.error("PreToolUse handler error: %s", exc, exc_info=True)

    sys.exit(0)


def handle_post_tool_use(data: dict) -> None:
    """Detect orchestrator violations and JUGGLE ACTIVE leaks in tool calls."""
    if not is_active():
        sys.exit(0)

    try:
        tool_name = data.get("tool_name", "")

        # Warning-only for search/read tools (sometimes legitimate for quick lookups)
        WARNING_TOOLS = {"Read", "Glob", "Grep", "WebFetch", "WebSearch"}
        if tool_name in WARNING_TOOLS:
            warning = (
                f"⚠️ ORCHESTRATOR VIOLATION: You used [{tool_name}] directly in the main thread. "
                "All file reads and searches MUST go through tmux agents. "
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

        # Violation: foreground Agent call (run_in_background not set or false)
        if not tool_input.get("run_in_background"):
            warning = (
                "⚠️ ORCHESTRATOR VIOLATION: Foreground Agent call detected. "
                "All Agent calls MUST use run_in_background=true. The orchestrator must stay responsive."
            )
            output = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": warning,
                }
            }
            print(json.dumps(output))
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
            db.add_notification(thread_id or current or "", warning, severity="warning")
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
    "PreToolUse": handle_pre_tool_use,
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
