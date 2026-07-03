#!/usr/bin/env python3
"""juggle_context_anchor — AGENT ROLE anchor rendering for dispatched agents.

Extracted from juggle_context.py (2026-07-03, LOC gate — T-fix-literal-finalize-line).
Owns: the TMUX_PANE→DB thread-label lookup and the AGENT ROLE anchor block
(COMPLETION line), rendered with a fully literal thread id and CLI path
(2026-07-03 DG incident: a generic ``<THREAD>`` placeholder here left an
agent with no literal thread id, so its finalize call expanded to an empty
string).
Must not own: the orchestrator dashboard / tier rendering (juggle_context).
"""

from __future__ import annotations

import os

from juggle_db import JuggleDB
from juggle_settings import get_settings as _get_settings


def _agent_thread_label(db, pane_id: str) -> str:
    """Literal thread label assigned to the agent running in ``pane_id``.

    Fail-open: returns "" if the pane is unknown, unassigned, or the DB lookup
    errors — callers fall back to a visibly-unresolved marker rather than crash.
    """
    if not pane_id:
        return ""
    try:
        for a in db.get_all_agents():
            if a.get("pane_id") == pane_id and a.get("assigned_thread"):
                t = db.get_thread(a["assigned_thread"])
                if t:
                    return t.get("user_label") or t.get("label") or a["assigned_thread"][:6]
    except Exception:
        pass
    return ""


def _lookup_thread_label_for_pane(pane_id: str) -> str:
    """Standalone DB-opening variant of ``_agent_thread_label`` for callers
    (e.g. ``juggle_harness.decorate_task``) that have no open ``db`` handle."""
    if not pane_id:
        return ""
    try:
        return _agent_thread_label(JuggleDB(), pane_id)
    except Exception:
        return ""


def render_agent_role_anchor_for(role: str, thread_label: str | None = None) -> str:
    """Return the AGENT ROLE anchor block for an explicit ``role``.

    Shared by the env-driven hook path (``_render_agent_role_anchor``, used by
    Claude Code's UserPromptSubmit hook) and by harness adapters that inline the
    anchor into the task prompt for harnesses without juggle hooks
    (``juggle_harness.HarnessAdapter.decorate_task``). Returns "" for an unknown
    or unconfigured role.

    ``thread_label`` should be the actual thread this agent is dispatched to —
    when the caller doesn't have it in hand, falls back to a TMUX_PANE→DB
    lookup.
    """
    if not role:
        return ""
    role_context = _get_settings().get("agent", {}).get("role_context", {})
    identity = role_context.get(role, "")
    if not identity:
        return ""
    from pathlib import Path as _Path
    try:
        _r = _Path(__file__).resolve().parent.parent
    except OSError:
        _r = _Path(__file__).parent.parent
    # CLAUDE_PLUGIN_ROOT (plugin-cache path, can be stale) never used here — 2026-07-03 CR+CO.
    plugin_root = os.environ.get("JUGGLE_REPO_ROOT") or str(_r)
    if not thread_label:
        thread_label = _lookup_thread_label_for_pane(os.environ.get("TMUX_PANE", ""))
    thread_part = thread_label or "<thread-unresolved>"
    return (
        "--- AGENT ROLE ---\n"
        f"ROLE: {role}. {identity}\n"
        f'COMPLETION: python3 {plugin_root}/src/juggle_cli.py agent complete {thread_part} "<summary>" --retain "<key finding>"'
    )


def _render_agent_role_anchor(db=None) -> str:
    """Return the AGENT ROLE anchor block for agent panes. Empty string otherwise."""
    if os.environ.get("JUGGLE_IS_AGENT") != "1":
        return ""
    thread_label = ""
    if db is not None:
        thread_label = _agent_thread_label(db, os.environ.get("TMUX_PANE", ""))
    return render_agent_role_anchor_for(
        os.environ.get("JUGGLE_AGENT_ROLE", ""), thread_label=thread_label,
    )
