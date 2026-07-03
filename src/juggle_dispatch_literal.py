"""juggle_dispatch_literal — literal-render finalize/mark-task commands.

Extracted from juggle_dispatch_core.py (2026-07-03, LOC gate —
T-fix-literal-finalize-line). Owns: the CLI-path resolver and the
generic-phrase → literal-command substitution applied to every dispatch
prompt just before it is pasted to an agent.
Must not own: worktree/pane/agent plumbing (juggle_dispatch_core).
"""

from __future__ import annotations

import os

# 'juggle <verb>' phrases every dispatch-prompt builder (task templates, graph
# hydration, topic hydration) emits generically → the literal, repo-pinned CLI
# invocation. Longer/aliased phrasings first so no partial match leaves a
# dangling 'juggle' word.
_FINALIZE_CMD_PHRASES = (
    "juggle complete-agent",
    "juggle fail-agent",
    "juggle agent complete",
    "juggle agent fail",
    "juggle graph mark-task",
)
_FINALIZE_CMD_VERBS = {
    "juggle complete-agent": "agent complete",
    "juggle fail-agent": "agent fail",
    "juggle agent complete": "agent complete",
    "juggle agent fail": "agent fail",
    "juggle graph mark-task": "graph mark-task",
}
_FINALIZE_PLACEHOLDERS = ("<thread>", "<THREAD>", "$THREAD")


def _cli_invocation_prefix() -> str:
    """Absolute, repo-pinned ``python3 .../juggle_cli.py`` prefix.

    Mirrors juggle_context.render_agent_role_anchor_for's resolver (2026-07-03
    cli-skew fix, commit 40b2c87): prefers JUGGLE_REPO_ROOT (the dispatching
    orchestrator's own repo, injected by juggle_harness) over a bare 'juggle'
    alias or a stale installed-plugin-cache path, both of which may not exist
    in the agent's shell.
    """
    from pathlib import Path as _Path

    root = os.environ.get("JUGGLE_REPO_ROOT")
    if not root:
        try:
            root = str(_Path(__file__).resolve().parent.parent)
        except OSError:
            root = str(_Path(__file__).parent.parent)
    return f"python3 {root}/src/juggle_cli.py"


def literalize_finalize_commands(text: str, thread_label: str) -> str:
    """Render finalize/mark-task commands fully literal in a dispatch prompt.

    Regression pin — incident DG (2026-07-03): a coder agent invoked
    `juggle agent complete "$THREAD"` because no literal thread id was
    anywhere in its prompt, so THREAD expanded to empty and the CLI call
    failed with 'Error: empty thread id'. Bakes in the dispatching repo's
    CLI path for every 'juggle <verb>' phrase and replaces the generic
    '<thread>'/'$THREAD' placeholder with the actual thread label so only
    <summary>/<retain>/<handoff> are left for the agent to fill.
    """
    cli = _cli_invocation_prefix()
    out = text
    for phrase in _FINALIZE_CMD_PHRASES:
        out = out.replace(phrase, f"{cli} {_FINALIZE_CMD_VERBS[phrase]}")
    for placeholder in _FINALIZE_PLACEHOLDERS:
        out = out.replace(placeholder, thread_label)
    return out
