"""Claude Code folder-trust pre-registration.

Claude Code gates a fresh directory behind a "Do you trust the files in this
folder?" prompt that is NOT bypassed by ``--permission-mode bypassPermissions``
/ ``--dangerously-skip-permissions`` (those skip *tool* permissions, not the
*workspace-trust* gate). An agent spawned into a dir Claude has never seen hangs
at that prompt forever — the 2026-06-20 agent-pane leak.

Claude Code records folder trust in ``~/.claude.json`` under
``projects[<abs_dir>].hasTrustDialogAccepted``. The historical helper here wrote
only ``{"allowedTools": []}`` and never set that flag, so the dialog still fired
(every juggle-created worktree entry had ``hasTrustDialogAccepted`` absent).
``ensure_dir_trusted`` writes the flag Claude actually reads.

Pure/IO-thin: a single best-effort JSON merge. A failure here must never block
agent dispatch — the caller still pays the (recoverable) trust-prompt cost.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# Env override for the config path (tests point this at a tmp file so a real
# ~/.claude.json is never touched).
CLAUDE_JSON_ENV = "JUGGLE_CLAUDE_JSON_PATH"


def _default_claude_json_path() -> Path:
    return Path(os.environ.get(CLAUDE_JSON_ENV, Path.home() / ".claude.json"))


def ensure_dir_trusted(dir_path: str, claude_json_path: Path | str | None = None) -> bool:
    """Mark ``dir_path`` as a trusted Claude Code project (idempotent).

    Sets ``projects[dir_path].hasTrustDialogAccepted = True`` — the field Claude
    Code checks before showing the trust gate — preserving every other project
    entry and top-level key. Upgrades an entry that already exists but lacks the
    flag (the leaked-worktree case). Returns True iff the file was written.

    Best-effort: any error is swallowed and returns False — pre-trust failure
    must never crash a spawn.
    """
    cfg = Path(claude_json_path) if claude_json_path is not None else _default_claude_json_path()
    try:
        if cfg.exists():
            data = json.loads(cfg.read_text())
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}
        projects = data.setdefault("projects", {})
        entry = projects.get(dir_path)
        if not isinstance(entry, dict):
            entry = {"allowedTools": []}
            projects[dir_path] = entry
        if entry.get("hasTrustDialogAccepted") is True:
            return False  # already trusted — no rewrite
        entry["hasTrustDialogAccepted"] = True
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(data, indent=2))
        return True
    except Exception:
        return False  # best-effort; never block dispatch
