"""Harness backstop against the agent verify zombie-loop (TODO L13).

bc514f3 funnels coder agents to a single FOREGROUND `juggle verify`; agents that
ignore the template still loop — spawn the full suite as a BACKGROUND job, poll
it, "come to rest", repeat — burning 100k–330k tokens each. The prompt fix
cannot stop a template-ignoring agent, so this is the CODE cap (deferred as
a48433ac DA M1/I2): count BACKGROUND full-suite/verify spawns per agent session
and hard-DENY beyond the cap, from the PreToolUse hook.

Only BACKGROUND spawns are counted — a single FOREGROUND `juggle verify` (the
sanctioned path) is never flagged.
"""
from __future__ import annotations

import json
import logging
import re
import sys

from dbops.verify_spawns import bump_verify_spawn

# Allow this many BACKGROUND suite spawns per agent session; DENY the next one.
# The sanctioned flow is ONE foreground `juggle verify`, so a small cap still
# leaves room for the rare legitimate background test run while killing the loop.
MAX_BG_VERIFY_SPAWNS = 2

# Full-suite / verify invocation forms. Matches `juggle verify`,
# `... juggle_cli.py verify`, any `pytest` run (uv run pytest / python -m pytest),
# and `make test` / `make test-fast`.
_BG_SUITE_RE = re.compile(
    r"\bjuggle\s+verify\b"
    r"|juggle_cli\.py\s+verify\b"
    r"|\bpytest\b"
    r"|\bmake\s+test\b"
)


def is_bg_suite_spawn(tool_name: str, tool_input) -> bool:
    """True iff this tool call is a BACKGROUND spawn of the full suite / verify.

    The zombie-loop signal: a Bash command run with ``run_in_background`` that
    invokes the suite. A FOREGROUND run (no ``run_in_background``) is the
    sanctioned single verify and returns False.
    """
    if tool_name != "Bash" or not isinstance(tool_input, dict):
        return False
    if not tool_input.get("run_in_background"):
        return False
    command = tool_input.get("command", "")
    return bool(command) and bool(_BG_SUITE_RE.search(command))


def enforce_verify_spawn_cap(data: dict, db_path) -> None:
    """Count a background suite spawn; hard-DENY (exit 2) beyond the cap.

    Returns normally (caller proceeds) when the call is not a background suite
    spawn or is still within the cap. Fails OPEN on any storage error — never
    block an agent because of a telemetry/DB hiccup.
    """
    try:
        if not is_bg_suite_spawn(data.get("tool_name", ""), data.get("tool_input", {})):
            return
        session_id = data.get("session_id", "") or "unknown"
        count = bump_verify_spawn(db_path, session_id)
    except SystemExit:
        raise
    except Exception as exc:  # fail OPEN — responsiveness over a perfect count
        logging.warning("verify-spawn cap check failed: %s", exc)
        return

    if count > MAX_BG_VERIFY_SPAWNS:
        msg = (
            f"🚫 Background full-suite/verify spawn BLOCKED (#{count} this agent). "
            "Do NOT background-and-poll the suite — that zombie-loops and burns the "
            "token budget (TODO L13). Run the suite ONCE in the FOREGROUND: "
            "`juggle verify`."
        )
        output = {
            "hookSpecificOutput": {"permissionDecision": "deny"},
            "systemMessage": msg,
        }
        logging.info("PreToolUse: capped background verify spawn #%d", count)
        print(json.dumps(output), file=sys.stderr)
        sys.exit(2)
