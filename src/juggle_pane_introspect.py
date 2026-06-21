"""Pane → agent-process introspection (shared `ps eww` / `pgrep` helpers).

Extracted from juggle_tmux (2026-06-20) to de-duplicate the identical
"pane pid → pgrep children → scan JUGGLE_IS_AGENT=1 in `ps eww` env" routine
that backed both ``_pane_has_juggle_agent_env`` and ``_get_oneshot_child_pid``,
and to keep juggle_tmux under its LOC budget. Re-imported into juggle_tmux so
the historical ``juggle_tmux._pane_has_juggle_agent_env`` patch surface (used by
many tests) is unchanged.
"""
from __future__ import annotations

import subprocess


def _agent_child_pids(pane_id: str) -> list[str]:
    """Child PIDs (as strings) of ``pane_id`` whose env has JUGGLE_IS_AGENT=1.

    Best-effort: any tmux/pgrep/ps failure yields an empty list.
    """
    try:
        pane_pid = subprocess.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_pid}"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        if not pane_pid:
            return []
        children = (
            subprocess.run(
                ["pgrep", "-P", pane_pid],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip().splitlines()
        )
        matches = []
        for child in children:
            env_out = subprocess.run(
                ["ps", "eww", "-p", child],
                capture_output=True, text=True, timeout=3,
            ).stdout
            if "JUGGLE_IS_AGENT=1" in env_out:
                matches.append(child)
        return matches
    except Exception:
        return []


def pane_has_juggle_agent_env(pane_id: str) -> bool:
    """True if any child process of the pane has JUGGLE_IS_AGENT=1."""
    return bool(_agent_child_pids(pane_id))


def get_oneshot_child_pid(pane_id: str) -> int | None:
    """PID of a one-shot JUGGLE_IS_AGENT=1 child in ``pane_id``, or None."""
    pids = _agent_child_pids(pane_id)
    if not pids:
        return None
    try:
        return int(pids[0])
    except (TypeError, ValueError):
        return None
