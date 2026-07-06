"""
juggle_cmd_agents_common — Shared symbols and pure classifiers for agent command modules.

Owns: re-exports of get_db, _resolve_thread, JuggleTmuxManager, get_adapter,
      _get_settings, UNIVERSAL_PREAMBLE, _AGENT_TTL_SECS, SRC_DIR,
      _create_worktree/_finalize_worktree, and the pure summary/failure
      classifiers (_looks_complete, _matches_draft, _matches_plan,
      _classify_failure).
Must not own: command handler logic.

All agent sub-modules (pool, lifecycle, complete, tasks) import via:
    import juggle_cmd_agents_common as _com
and call _com.get_db(), _com.JuggleTmuxManager(), etc. at call time so that
test monkeypatches on this module's attributes take effect — this is the
single patch surface for the whole juggle_cmd_agents_* family.
"""

import re

from juggle_cli_common import (
    SRC_DIR,                # noqa: F401 — re-exported for sub-module use
    _get_hindsight_client,  # noqa: F401 — re-exported for sub-module use
    _last_sentences,        # noqa: F401 — re-exported for sub-module use
    _resolve_thread,        # noqa: F401 — re-exported for sub-module use
    get_db,                 # noqa: F401 — re-exported for sub-module use
)
import juggle_cmd_integrate  # noqa: F401 — imported here so sub-modules can access via _com
from juggle_cmd_agents_worktree import (  # noqa: F401 — re-exported patch surface
    _create_worktree,
    _finalize_worktree,
)
from juggle_harness import get_adapter  # noqa: F401 — re-exported for sub-module use
from juggle_settings import get_settings as _get_settings
from juggle_tmux import JuggleTmuxManager  # noqa: F401 — re-exported for sub-module use

_AGENT_TTL_SECS: int = _get_settings()["agent_idle_ttl_secs"]

# PC2/PC3 cutover (2026-07-03, pc2-coder-cutover + pc3-emitters-sweep): no
# longer prepended for coder/planner/researcher — juggle_dispatch_core.
# send_task_to_agent renders all three via render_agent_prompt
# (juggle_prompt_context), whose header INVARIANT line and Guardrails section
# carry rules 1/2 respectively. juggle_metrics still imports this constant to
# measure boilerplate bytes in historical (already-dispatched) ledger
# prompts, and juggle_dispatch_core's fallback path still prepends it for any
# future role not yet swept onto render_agent_prompt.
UNIVERSAL_PREAMBLE = """\
## Universal rules (enforced for every agent)

1. Your task ENDS with an `agent complete` or `agent fail` Bash call. NEVER stop at the input prompt and wait for guidance — either complete with results, complete with BLOCKER, or complete with --open-questions JSON.
2. Pre-existing test failures (failures present on the base commit) ARE your concern — proactively FIX them during the run unless the task says otherwise, and note them in --retain.

---

"""

_DRAFT_PATTERNS = [
    re.compile(r"\bdraft (v\d+|version|complete|written)\b", re.I),
    re.compile(r"\bfirst pass\b", re.I),
    re.compile(r"\bwip\b", re.I),
    re.compile(r"\bplaceholders? (remain|left|unresolved)\b", re.I),
    re.compile(r"\btodos? (remain|left|added)\b", re.I),
    re.compile(r"\bpartial (result|implementation|fix|completion|work)\b", re.I),
    re.compile(r"\bin progress\b", re.I),
    re.compile(r"\binitial (draft|version|cut|pass)\b", re.I),
    re.compile(
        r"\bpending (?:review|input|decision) (?:from|by|on|before|required|needed)\b",
        re.I,
    ),
    re.compile(r"\bpending user\b", re.I),
    re.compile(r"\bv1 (draft|prototype|sketch)\b", re.I),
]

_PLAN_PATTERNS = [
    re.compile(r"\bplan written\b", re.I),
    re.compile(r"\bspec written\b", re.I),
    re.compile(r"\bdesign doc(?:ument)? (?:written|drafted)\b", re.I),
    re.compile(r"\bplan at \S+", re.I),
    re.compile(r"\bspec at \S+", re.I),
]

_COMPLETE_PATTERNS = [
    re.compile(r"\ball (\d+ )?tests pass(?:ing|ed)?\b", re.I),
    re.compile(r"\b(?:committed|merged|pushed) to (?:main|master)\b", re.I),
    re.compile(r"\bSHA: ?[a-f0-9]{7,}\b", re.I),
    re.compile(r"\bPR (?:opened|merged|created): ?#?\d+", re.I),
    re.compile(r"\bshipped (?:v\d|to (?:prod|production|main))\b", re.I),
    re.compile(r"\bv\d+\.\d+\.\d+\b.*\b(?:released|shipped|complete)\b", re.I),
]


def _looks_complete(summary: str) -> bool:
    return any(p.search(summary) for p in _COMPLETE_PATTERNS)


def _matches_draft(summary: str) -> bool:
    return any(p.search(summary) for p in _DRAFT_PATTERNS)


def _matches_plan(summary: str) -> bool:
    return any(p.search(summary) for p in _PLAN_PATTERNS)


_TRANSIENT_PATTERNS = (
    "etimedout",
    "econnrefused",
    "econnreset",
    "timeout",
    "timed out",
    "rate limit",
    "429",
    "502",
    "503",
    "504",
    "network unreachable",
    "temporarily unavailable",
    "audio device",
    "audio busy",
)

_PERSISTENT_HINTS = (
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "filenotfounderror",
    "no such file",
    "permissionerror",
    "syntaxerror",
    "typeerror",
    "valueerror",
    "assertionerror",
    "keyerror",
    "attributeerror",
)


def _classify_failure(error: str) -> str:
    """Return 'transient' or 'persistent'. Case-insensitive substring match."""
    if not error:
        return "persistent"
    low = error.lower()
    # Persistent hints take precedence when ambiguous
    for h in _PERSISTENT_HINTS:
        if h in low:
            return "persistent"
    for t in _TRANSIENT_PATTERNS:
        if t in low:
            return "transient"
    return "persistent"


def file_role_action_items(db, thread_uuid, agent, args, open_questions) -> None:
    """File the role-based post-completion review action item (if any).

    Extracted verbatim from cmd_complete_agent (2026-07-05 LOC-gate): a
    researcher with open questions / a planner / a plan- or draft-shaped coder
    summary gets a "review before proceeding" item; a clean coder run gets none.
    """
    role = (agent.get("role") if agent else None) or getattr(args, "role", None)
    if role == "researcher" and open_questions:
        db.add_action_item(
            thread_id=thread_uuid,
            message=f"Review: {args.result_summary}",
            type_="review",
            priority="normal",
        )
    elif role == "planner":
        db.add_action_item(
            thread_id=thread_uuid,
            message=f"Review plan before dispatching coder: {args.result_summary}",
            type_="decision",
            priority="normal",
        )
    elif role not in ("researcher", "planner"):
        summary = args.result_summary or ""
        if _matches_plan(summary):
            db.add_action_item(
                thread_id=thread_uuid,
                message=f"Review before dispatching coder: {args.result_summary}",
                type_="decision",
                priority="normal",
            )
        elif _matches_draft(summary) and not _looks_complete(summary):
            db.add_action_item(
                thread_id=thread_uuid,
                message=f"Review/iterate: {args.result_summary}",
                type_="manual_step",
                priority="normal",
            )
