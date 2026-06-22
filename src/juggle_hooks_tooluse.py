"""
juggle_hooks_tooluse — PreToolUse and PostToolUse hook handlers.

Owns: handle_pre_tool_use, handle_post_tool_use, _is_orchestrator_session,
      _bash_write_pattern, _tool_input_sample, _log_agent_tool_use.
Must not own: checkpoint logic, Hindsight retention, DB path constants.
"""

import json
import logging
import os
import re
import sys

import juggle_hooks_config as _cfg
from juggle_db import JuggleDB
from juggle_hooks_askuser import clear_askuser_decision, record_askuser_decision
from juggle_verify_cap import enforce_verify_spawn_cap

# Use _cfg.<name>() everywhere — do NOT bind these as local names, so that
# tests can monkeypatch juggle_hooks_config.<name> and have the patches take
# effect inside these handlers.
_record_error_safe = lambda *a, **k: _cfg._record_error_safe(*a, **k)
_get_session_id = lambda *a, **k: _cfg._get_session_id(*a, **k)


def is_active() -> bool:  # thin delegate — reads _cfg at call time
    return _cfg.is_active()


def get_db():  # thin delegate — reads _cfg at call time
    return _cfg.get_db()


# ---------------------------------------------------------------------------
# Bash-write detection
# ---------------------------------------------------------------------------

_BASH_WRITE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bsed\s+(-[^\s]*i[^\s]*\s|--in-place)"), "sed -i (in-place edit)"),
    (
        re.compile(r"\bgit\s+(add|commit|push|reset|rebase|merge|rm|mv)\b"),
        "git write operation",
    ),
    (re.compile(r"\brm\s+"), "rm (file deletion)"),
    (re.compile(r"\bpatch\b"), "patch"),
    (re.compile(r"\btee\s+\S"), "tee (file write)"),
    (re.compile(r"\bmv\s+\S+\s+\S"), "mv (file move/overwrite)"),
    (
        re.compile(r"(?<![0-9&|])\s*>{1,2}(?!\s*(?:/dev/|/tmp/|&|\d))"),
        "output redirect (> or >>)",
    ),
]

_BASH_WRITE_ALLOW_PATTERNS: list[re.Pattern] = [
    re.compile(r"/tmp/juggle_"),  # juggle orchestrator task-file writes
]


def _bash_write_pattern(command: str) -> str | None:
    """Return the label of the first file-write pattern found, or None."""
    for allow_pat in _BASH_WRITE_ALLOW_PATTERNS:
        if allow_pat.search(command):
            return None
    for pattern, label in _BASH_WRITE_PATTERNS:
        if pattern.search(command):
            return label
    return None


def _tool_input_sample(tool_input) -> str | None:
    """Return a short (<=120 char) representative sample of a tool's input."""
    if not isinstance(tool_input, dict) or not tool_input:
        return None
    for key in ("command", "file_path", "path", "pattern", "query", "url", "prompt"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            sample = val if len(val) <= 120 else val[:117] + "..."
            return f"{key}={sample}"
    return next(iter(tool_input), None)


def _log_agent_tool_use(data: dict) -> None:
    """Best-effort: record this agent's tool call for usage analytics."""
    try:
        tool_name = data.get("tool_name", "")
        if not tool_name:
            return
        role = os.environ.get("JUGGLE_AGENT_ROLE") or "unknown"
        mode = "audit" if os.environ.get("JUGGLE_AGENT_AUDIT") else "normal"
        sample = _tool_input_sample(data.get("tool_input"))
        JuggleDB(str(_cfg.DB_PATH)).record_agent_tool_use(  # _cfg.DB_PATH read at call time
            role, tool_name, mode, sample
        )
    except Exception as exc:
        logging.warning("agent tool-use logging failed: %s", exc)


# ---------------------------------------------------------------------------
# Orchestrator session guard
# ---------------------------------------------------------------------------

_ORCHESTRATOR_SESSION_TTL_SECS = 86400  # 24 hours


def _is_orchestrator_session(data: dict) -> bool:
    """Return True iff the current session is the registered orchestrator and not stale."""
    import time

    try:
        db = get_db()
        orch_sid = db.get_orchestrator_session_id()
        if not orch_sid:
            return False

        curr_sid = data.get("session_id", "")
        if curr_sid != orch_sid:
            return False

        ts = db.get_orchestrator_session_ts()
        if ts and (time.time() - ts) > _ORCHESTRATOR_SESSION_TTL_SECS:
            db.set_orchestrator_session_id("")
            logging.info("PreToolUse: orchestrator session %s expired (>24h)", orch_sid[:8])
            return False

        return True
    except Exception as exc:
        logging.warning("_is_orchestrator_session check failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_pre_tool_use(data: dict) -> None:
    """Hard-block Edit/Write/NotebookEdit/Bash-writes in the orchestrator main thread."""
    if os.environ.get("JUGGLE_IS_AGENT"):
        _log_agent_tool_use(data)
        enforce_verify_spawn_cap(data, _cfg.DB_PATH)  # may DENY + sys.exit(2)
        sys.exit(0)

    if not is_active():
        sys.exit(0)

    try:
        tool_name = data.get("tool_name", "")
        BLOCKED_TOOLS = {"Edit", "Write", "NotebookEdit"}
        _TMP_PREFIXES = ("/tmp/", "/private/tmp/")
        if tool_name in BLOCKED_TOOLS and _is_orchestrator_session(data):
            file_path = data.get("tool_input", {}).get("file_path", "")
            if tool_name in ("Write", "Edit") and any(
                file_path.startswith(p) for p in _TMP_PREFIXES
            ):
                sys.exit(0)
            session_id = data.get("session_id", "")[:8]
            msg = (
                f"🚫 {tool_name} blocked in juggle orchestrator session [{session_id}]. "
                "File edits must go through an agent. Use get-agent + send-task to dispatch."
            )
            logging.info(
                "PreToolUse: blocked %s in orchestrator session %s",
                tool_name,
                session_id,
            )
            output = {
                "hookSpecificOutput": {"permissionDecision": "deny"},
                "systemMessage": msg,
            }
            print(json.dumps(output), file=sys.stderr)
            sys.exit(2)
        if tool_name == "Bash" and _is_orchestrator_session(data):
            command = data.get("tool_input", {}).get("command", "")
            matched = _bash_write_pattern(command)
            if matched:
                session_id = data.get("session_id", "")[:8]
                msg = (
                    f"🚫 Bash blocked in juggle orchestrator session [{session_id}]: "
                    f"detected file-write pattern '{matched}'. "
                    "File modifications must go through an agent. Use get-agent + send-task to dispatch."
                )
                logging.info(
                    "PreToolUse: blocked Bash(%s) in orchestrator session %s",
                    matched,
                    session_id,
                )
                output = {
                    "hookSpecificOutput": {"permissionDecision": "deny"},
                    "systemMessage": msg,
                }
                print(json.dumps(output), file=sys.stderr)
                sys.exit(2)
        # Track pending AskUserQuestion decisions
        if tool_name == "AskUserQuestion":
            record_askuser_decision(get_db(), data)

    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.PreToolUse")
        logging.error("PreToolUse handler error: %s", exc, exc_info=True)

    sys.exit(0)


def handle_post_tool_use(data: dict) -> None:
    """Detect orchestrator violations and JUGGLE ACTIVE leaks in tool calls."""
    if os.environ.get("JUGGLE_IS_AGENT") == "1":
        sys.exit(0)

    if not is_active():
        sys.exit(0)

    try:
        tool_name = data.get("tool_name", "")

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

        # Clear pending decisions after AskUserQuestion completes
        if tool_name == "AskUserQuestion":
            clear_askuser_decision(get_db(), data)
            sys.exit(0)

        if tool_name != "Agent":
            sys.exit(0)

        tool_input = data.get("tool_input", {})
        if not isinstance(tool_input, dict):
            sys.exit(0)

        # Violation: foreground Agent call
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

        prompt = tool_input.get("prompt", "")
        thread_match = re.search(r"\[JUGGLE_THREAD:([^\]]+)\]", prompt)
        thread_id = thread_match.group(1) if thread_match else None

        if "JUGGLE ACTIVE" in prompt:
            db = get_db()
            warning = (
                f"[juggle] WARNING: Agent prompt for thread {thread_id or '?'} contains "
                f"'JUGGLE ACTIVE' block — context leaked to sub-agent. "
                f"Strip JUGGLE blocks before dispatching agents."
            )
            current = db.get_current_thread()
            db.add_notification_v2(
                thread_id or current or "", warning, session_id=_get_session_id(db)
            )
            logging.warning(
                "JUGGLE ACTIVE leaked into sub-agent prompt for thread %s", thread_id
            )

    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.PostToolUse")
        logging.error("PostToolUse handler error: %s", exc, exc_info=True)

    sys.exit(0)
