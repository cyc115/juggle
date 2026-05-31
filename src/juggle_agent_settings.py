#!/usr/bin/env python3
"""Generate per-role Claude Code settings overlays for juggle agents.

Background agents launch as full ``claude`` processes in tmux panes. Previously
their per-role denied-tool list was passed as a long
``--disallowedTools a,b,c,...`` flag that had to be *pasted* into the pane —
unreliable, because tmux collapses large pastes (see ``send_task`` in
``juggle_tmux.py`` for the retry machinery that exists to fight this). Instead
we write a small settings JSON file per role and launch with
``--settings <path>`` — one short, fixed token regardless of how long the deny
list is.

Correctness property (verified against the Claude Code docs):

  ``--settings`` LAYERS over the host settings hierarchy — *"Values you set here
  override the same keys in your settings.json files for this session. Keys you
  omit keep their file-based values."* And permission ``allow``/``deny``/``ask``
  arrays UNION across every source (managed, user, project, local, CLI).

So this overlay is purely ADDITIVE: the host environment's own user / project /
managed settings stay fully in effect, and we only add role rules on top. That
is what makes it portable across arbitrary dev environments — we never read or
replace the host's settings. The overlay must therefore contain ONLY additive
surfaces (``permissions.*`` arrays, ``env``) plus any scalar a role
*deliberately* wants to override; omitted keys are left to the host.
"""

import json
import uuid
from pathlib import Path

from juggle_settings import get_settings


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into ``base``; return a new dict."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _union(*lists) -> list:
    """Concatenate lists preserving first-seen order, dropping duplicates."""
    seen: set = set()
    out: list = []
    for lst in lists:
        for item in lst or []:
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def build_agent_overlay(role: str | None, overrides: dict | None = None) -> dict:
    """Return the additive settings overlay dict for ``role``.

    Composition (lowest → highest precedence):
      ``agent.settings_overlay_base``
        → ``agent.settings_overlay_by_role[role]``
        → ``permissions.deny`` derived from ``disallowed_tools_*`` (unioned with
          any deny already present in the templates)
        → per-dispatch ``overrides`` (deep-merged last).

    Empty by default beyond ``permissions.deny`` — identical across roles today,
    but ``settings_overlay_by_role`` lets any role diverge later (its own
    ``model``, ``env``, ``hooks``, ``sandbox``, etc.) with no code change.
    """
    agent = get_settings().get("agent", {})

    overlay = _deep_merge(
        agent.get("settings_overlay_base") or {},
        (agent.get("settings_overlay_by_role") or {}).get(role) or {},
    )

    denied = _union(
        agent.get("disallowed_tools_universal") or [],
        (agent.get("disallowed_tools_by_role") or {}).get(role) or [] if role else [],
    )
    if denied:
        perms = dict(overlay.get("permissions") or {})
        perms["deny"] = _union(perms.get("deny") or [], denied)
        overlay["permissions"] = perms

    if overrides:
        overlay = _deep_merge(overlay, overrides)

    return overlay


def _overlay_dir() -> Path:
    cfg_dir = Path(get_settings()["paths"]["config_dir"]).expanduser()
    out = cfg_dir / "agent-settings"
    out.mkdir(parents=True, exist_ok=True)
    return out


def write_agent_overlay(role: str | None, overrides: dict | None = None) -> Path:
    """Write ``build_agent_overlay(role, overrides)`` to JSON; return its path.

    Per-role file (``<role>.json``), regenerated on every spawn so config edits
    take effect. When per-dispatch ``overrides`` are supplied the file is
    per-agent (unique name) so concurrent agents of the same role can't clobber
    each other's overlay.
    """
    overlay = build_agent_overlay(role, overrides)
    role_name = role or "default"
    fname = (
        f"{role_name}-{uuid.uuid4().hex[:8]}.json" if overrides else f"{role_name}.json"
    )
    path = _overlay_dir() / fname
    path.write_text(json.dumps(overlay, indent=2))
    return path
