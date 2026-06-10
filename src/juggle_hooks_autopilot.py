"""
juggle_hooks_autopilot — autopilot directive injection for UserPromptSubmit.

Owns: the autopilot directive text and the flag-gated context builder
(``autopilot_context``). The ``~/.juggle/autopilot`` flag file is an
existence-only cache for the global toggle; the armed-project authority is
the ``autopilot_armed_project`` settings key (DA M6).
Must not own: prompt-handler plumbing (juggle_hooks_prompt) or flag-file
path constants (juggle_hooks_config).
"""

from __future__ import annotations

import logging

import juggle_hooks_config as _cfg

# Concise re-assertion of the /juggle:toggle-autopilot loop.
_AUTOPILOT_DIRECTIVE = (
    "--- AUTOPILOT MODE: ON ---\n"
    "Autopilot is engaged (~/.juggle/autopilot present). Drive every requested "
    "feature to completion autonomously — do NOT pause for approval:\n"
    "1. Per feature: brainstorm/spec → devil's-advocate critique → resolve open "
    "questions yourself at staff level (decide, note why, proceed).\n"
    "2. Implement on a feature branch via dispatched agents (TDD); verify each "
    "feature with a harness before starting the next.\n"
    "3. Self-unblock: on a blocker or stalled agent, diagnose → recover → continue.\n"
    "4. Escalate ONLY for: missing credentials, an irreversible/destructive "
    "external action, or a product-direction fork with no defensible default.\n"
    "Toggle off with /juggle:toggle-autopilot. See commands/toggle-autopilot.md "
    "for the full loop."
)

# LLM directive carve-out (DA B5): re-asserted every turn while a project is
# armed, alongside the code-level send-task node guard (belt and braces).
_ARMED_CARVEOUT = (
    "ARMED PROJECT {project}: nodes of the armed project are tick-owned — "
    "NEVER dispatch them manually; report status only. The watchdog tick "
    "claims, dispatches, and completes graph nodes; manual send-task to "
    "node-bound threads is refused without --force-node."
)


def _armed_graph_context() -> str:
    """Carve-out + budgeted graph status when a project is armed, else ''.

    Authority is the ``autopilot_armed_project`` settings key (DA M6).
    Degrades to '' on any DB error — the base directive must survive.
    """
    try:
        from juggle_graph_dispatch import get_armed_project
        from juggle_graph_status import build_graph_injection

        db = _cfg.get_db()
        armed = get_armed_project(db)
        if not armed:
            return ""
        return (
            _ARMED_CARVEOUT.format(project=armed)
            + "\n"
            + build_graph_injection(db, armed)
        )
    except Exception as exc:
        logging.warning("armed-project graph context failed: %s", exc)
        return ""


def autopilot_context() -> str:
    """Return the autopilot directive (+ armed-project context) if the flag
    file is set, else ''."""
    try:
        if _cfg.AUTOPILOT_FLAG.exists():
            armed = _armed_graph_context()
            return _AUTOPILOT_DIRECTIVE + (f"\n{armed}" if armed else "")
    except Exception as exc:
        logging.warning("autopilot flag check failed: %s", exc)
    return ""
