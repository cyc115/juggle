"""
juggle_hooks_autopilot — autopilot directive injection for UserPromptSubmit.

Owns: the autopilot directive text and the flag-gated context builder
(``autopilot_context``). The ``~/.juggle/autopilot`` flag file is an
existence-only cache for the global toggle; the armed set is DERIVED
(default-armed): active projects minus the ``autopilot_disarmed_project``
exclusion set, via ``juggle_autopilot_state.get_armed_projects`` (2026-06-30).
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
    "5. DEFECT PROTOCOL — defects outrank features: the instant a Juggle "
    "machinery defect appears (illegal/unexpected state transition, dispatch "
    "race, topic/task/agent state divergence, shared-DB corruption, or a "
    "'verified' that isn't merged), STOP feature work, freeze (stop the "
    "watchdog), RCA → plan → fix the defect, capture it as a tracked incident, "
    "THEN resume.\n"
    "6. 'verified' ⟺ MERGED TO MAIN: mark a task/topic verified ONLY once its "
    "work is merged into main — never for unmerged WIP. Agents work in isolated "
    "worktrees and MUST NOT run migrations against the shared production DB "
    "(~/.claude/juggle/juggle.db).\n"
    "Toggle off with /juggle:toggle-autopilot. See commands/toggle-autopilot.md "
    "for the full loop."
)

# LLM directive carve-out (DA B5 / R7): re-asserted every turn while projects
# are armed, alongside the code-level send-task task guard (belt and braces).
_ARMED_CARVEOUT = (
    "ARMED PROJECTS {projects}: topics of any armed project are tick-owned — "
    "NEVER dispatch them manually; report status only. NEW work for an armed "
    "project goes in as a task: `juggle graph add-task … --topic <t>` "
    "(code-enforced — manual send-task is refused without --force-task). "
    "The watchdog tick claims, dispatches, and completes topics; integrate "
    "runs once per topic."
)


def _armed_graph_context() -> str:
    """Carve-out + budgeted topic status for EVERY armed project, else \'\'.

    The armed set is DERIVED (default-armed): active projects minus the
    disarmed exclusion set. The per-project injection budget is the 500-char
    discipline split across the set (floor 160) so total stays bounded for any
    N. Degrades to \'\' on any DB error.
    """
    try:
        from juggle_autopilot_state import get_armed_projects
        from juggle_graph_status import INJECTION_BUDGET, build_graph_injection

        db = _cfg.get_db()
        armed = get_armed_projects(db)
        if not armed:
            return ""
        per = max(160, INJECTION_BUDGET // len(armed))
        lines = [_ARMED_CARVEOUT.format(projects=", ".join(armed))]
        lines += [build_graph_injection(db, pid, budget=per) for pid in armed]
        return "\n".join(lines)
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
