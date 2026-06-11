#!/usr/bin/env python3
"""
Juggle Hooks - Claude Code hook handlers.
Called automatically by Claude Code on lifecycle events.
Usage: python3 juggle_hooks.py <event_name>
Events: UserPromptSubmit, Stop, SessionStart, PreCompact, PreToolUse, PostToolUse

This file is PATH-PINNED: hooks/hooks.json invokes it by path.
It is a thin dispatcher shim — behavior lives in juggle_hooks_*.py sub-modules.
"""

import json
import logging
import sys
from pathlib import Path

# Add the directory containing this file to sys.path so we can import siblings.
sys.path.insert(0, str(Path(__file__).parent))

# ---------------------------------------------------------------------------
# Re-export module-level attributes that tests patch via "juggle_hooks.<attr>"
# ---------------------------------------------------------------------------
from juggle_db import JuggleDB  # noqa: F401 — re-exported for test patching
from juggle_hooks_config import (  # noqa: F401
    DB_PATH,
    _DATA_DIR,
    _CHECKPOINT_PATH,
    _CHECKPOINT_MAX_AGE_SECS,
    AUTOPILOT_FLAG,
    _record_error_safe,
    is_active,
    get_db,
    _get_session_id,
)

# ---------------------------------------------------------------------------
# Re-export handler-level symbols used by tests / external callers
# ---------------------------------------------------------------------------
from juggle_hooks_prompt import (  # noqa: F401
    handle_user_prompt_submit,
    auto_approve_blocked_agents,
    get_classification_candidates,
    _classify_context,
    _retain_conversation_turn,
    _autopilot_context,
)
from juggle_hooks_checkpoint import (  # noqa: F401
    handle_session_start,
    handle_pre_compact,
    _write_checkpoint,
    _restore_checkpoint,
)
from juggle_hooks_tooluse import (  # noqa: F401
    handle_pre_tool_use,
    handle_post_tool_use,
    _bash_write_pattern,
    _tool_input_sample,
    _log_agent_tool_use,
    _is_orchestrator_session,
)
from juggle_hooks_classb import (  # noqa: F401
    _scan_transcript_for_class_b,
    _do_class_b_scan,
    _attribute_tool_errors,
)


# handle_stop needs to call _scan_transcript_for_class_b — wire it here so
# the sub-module doesn't need a circular back-import.
def handle_stop(data: dict) -> None:
    from juggle_hooks_prompt import handle_stop as _handle_stop
    _handle_stop(data, _scan_transcript_for_class_b)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

HANDLERS = {
    "UserPromptSubmit": handle_user_prompt_submit,
    "Stop": handle_stop,
    "SessionStart": handle_session_start,
    "PreCompact": handle_pre_compact,
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

    try:
        handler(data)
    except Exception as exc:
        # UserPromptSubmit is additive (inject context) — a filesystem or other error
        # must never block the prompt. Warn to stderr so the failure is diagnosable.
        if event_name == "UserPromptSubmit":
            print(
                f"[juggle] WARNING: UserPromptSubmit unhandled error (fail-open): {exc}",
                file=sys.stderr,
            )
            sys.exit(0)
        _record_error_safe(exc, f"juggle_hooks.{event_name}")
        logging.error("Unhandled error in hook %s: %s", event_name, exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
