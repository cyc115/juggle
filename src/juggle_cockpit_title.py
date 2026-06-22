"""Cockpit title-bar version (single source of truth: .claude-plugin/plugin.json).

Extracted from juggle_cockpit.py to keep that module within its LOC budget.
The Textual Header reads App.title / App.sub_title; on_mount sets sub_title from
``_cockpit_subtitle(_get_version(), width=...)``.
"""
from __future__ import annotations

import json
from pathlib import Path


def _get_version() -> str:
    """Read the juggle version from plugin.json. Returns '?' on failure."""
    plugin_json = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    try:
        return json.loads(plugin_json.read_text())["version"]
    except Exception:
        return "?"


def _drift_banner(boot: str, current: str) -> str | None:
    """Return a restart-to-load banner when the on-disk version has moved past
    the version captured at cockpit boot; None when they match.

    ``boot``    — version read from plugin.json at cockpit launch (the running code).
    ``current`` — version re-read from plugin.json on a refresh tick (on disk).

    Returns None when the versions match OR either is unknown ("?") so a failed
    read never raises a false "restart" banner. Incident 2026-06-21: a stale
    cockpit ran ~4.5h against newer code and MASKED a fix until manual restart.
    """
    if not boot or not current or "?" in (boot, current) or boot == current:
        return None
    return f"Cockpit running v{boot} (v{current} available) — restart to load"


def _cockpit_subtitle(version: str, width: int | None = None) -> str:
    """Build the Header sub_title with the version appended.

    Wide: "Cockpit v2 · v<version>". Narrow (width < 100): drop the prefix
    and show just "v<version>" so the title never overflows the 80-col profile.
    """
    ver = f"v{version}"
    if width is not None and width < 100:
        return ver
    return f"Cockpit v2 · {ver}"
