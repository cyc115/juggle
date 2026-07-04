"""
juggle_hooks_finalize — AgentStop hook: code-enforce agent finalization.

Owns: handle_agent_stop and its helpers.
Must not own: orchestrator Stop capture (juggle_hooks_prompt), tool-use blocking,
DB path constants.

The finalization rule ("run `agent complete`/`agent fail` before ending your
turn") previously lived ONLY in agent prompts — a prompt-only contract that
coder agents violated (observed: LV, MR) by printing a recap and idling at the
prompt with their thread wedged in-flight. This Stop hook is the code backstop.

It is installed ONLY in the per-role agent settings overlay (`--settings` at
spawn — see juggle_settings.settings_overlay_base), so it fires in agent panes
and NEVER in the orchestrator/main session. On Stop it checks whether this
agent's thread is still in-flight and unfinalized; if so it BLOCKS the turn-end
and tells the agent to finalize.

Constraints (task T-enforce-finalize-stop-hook):
  * agent panes only — also gated on JUGGLE_IS_AGENT=1 (defence in depth).
  * fast — one filesystem scan of the spool + one agents/threads read.
  * fail-open — ANY error (DB locked/missing/malformed) lets the stop through;
    a hook exception must never wedge an agent.
  * loop-safe — honors ``stop_hook_active`` so an unfinalized turn is nudged
    once, never trapped in an infinite block loop.
"""
from __future__ import annotations

import json
import os
import sys

_FINALIZE_MSG = (
    "🚫 You have NOT finalized this task — your Juggle thread is still in-flight. "
    'Run `agent complete <label> "<summary>" --retain "<findings>"` '
    '(or `agent fail <label> "<error>"` if blocked) BEFORE ending your turn. '
    "Do not stop at the prompt without finalizing."
)


def _agent_thread(db, cwd: str):
    """Return the in-flight thread UUID for the busy agent whose worktree == cwd.

    Mirrors juggle_hooks_prompt._stamp_agent_session's cwd→worktree match: the
    Stop payload's cwd is the agent's worktree, and exactly one busy agent owns
    that worktree. Returns None when no busy agent maps to cwd — i.e. nothing is
    in-flight here to finalize (agent already reaped, or this isn't a worktree).
    """
    if not cwd:
        return None
    for a in db.get_all_agents():
        if a.get("status") != "busy" or not a.get("assigned_thread"):
            continue
        thread = db.get_thread(a["assigned_thread"]) or {}
        if (thread.get("worktree_path") or "") == cwd:
            return a["assigned_thread"]
    return None


def _is_finalized(thread_uuid: str) -> bool:
    """True when an agent_complete/agent_fail for this thread is already recorded.

    In agent context `agent complete`/`agent fail` SPOOL their event (they never
    open the shared DB read-write — see spool_event_if_agent), so the
    authoritative "did finalize" signal is a pending spool event whose
    args.thread_id (resolved to the full UUID at write time) matches. Pure
    filesystem read — no DB connection.
    """
    from dbops.spool import read_pending
    from juggle_spool_paths import spool_dir

    for ev in read_pending(spool_dir()):
        if ev.type in ("agent_complete", "agent_fail"):
            tid = str((ev.args or {}).get("thread_id") or ev.thread_id or "")
            if tid == thread_uuid:
                return True
    return False


def _block_reason(data: dict) -> str | None:
    """Return the finalization nudge if this agent's thread is unfinalized, else None."""
    from juggle_hooks_config import get_db

    db = get_db()
    cwd = (data.get("cwd") or "").strip()
    thread_uuid = _agent_thread(db, cwd)
    if not thread_uuid:
        return None  # no in-flight agent thread here — nothing to enforce
    if _is_finalized(thread_uuid):
        return None  # already ran agent complete/fail (spooled)
    return _FINALIZE_MSG


def handle_agent_stop(data: dict) -> None:
    """Block an agent's turn-end until it has run `agent complete`/`agent fail`.

    Fail-open on EVERYTHING: not-an-agent, no in-flight thread, already
    finalized, loop guard, or any exception → exit 0 (allow the stop). Only a
    genuine unfinalized in-flight thread emits the block decision.
    """
    # Agent panes only. The overlay that installs this hook is agent-scoped, but
    # gate here too so a stray invocation in the orchestrator never blocks.
    if os.environ.get("JUGGLE_IS_AGENT") != "1":
        sys.exit(0)
    # Loop guard: once we've blocked and the agent is continuing FROM this hook,
    # never block again — one firm nudge, never an infinite stop loop.
    if data.get("stop_hook_active"):
        sys.exit(0)
    try:
        reason = _block_reason(data)
    except Exception:
        sys.exit(0)  # fail-open — a hook error must never wedge an agent
    if reason:
        print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)
