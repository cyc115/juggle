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
    SRC_DIR,
    _get_hindsight_client,  # noqa: F401 — re-exported for sub-module use
    _last_sentences,        # noqa: F401
    _resolve_thread,
    get_db,
)
import juggle_cmd_integrate  # noqa: F401 — imported here so sub-modules can access via _com
from juggle_cmd_agents_worktree import (  # noqa: F401 — re-exported patch surface
    _create_worktree,
    _finalize_worktree,
)
from juggle_harness import get_adapter
from juggle_settings import get_settings as _get_settings
from juggle_tmux import JuggleTmuxManager

_AGENT_TTL_SECS: int = _get_settings()["agent_idle_ttl_secs"]

UNIVERSAL_PREAMBLE = """\
## Universal rules (enforced for every agent)

1. Your task ENDS with a `complete-agent` or `fail-agent` Bash call. NEVER stop at the input prompt and wait for guidance — either complete with results, complete with BLOCKER, or complete with --open-questions JSON.
2. Pre-existing test failures (failures present on the base commit) are NOT your concern — document in --retain and proceed.

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
