#!/usr/bin/env python3
"""Juggle vault paths — the ONE canonical vault / loop-output root resolver.

Single source of truth for resolving the Obsidian vault root and the loop-output
root from settings. Extracts the duplicated inline home-relative normalization
that lived at `juggle_cli.py:53-57` and `juggle_cmd_research.py:52-62` (spec
§1.2, 2026-07-04) so every command — and the loop cross-topic handoff path (spec
§1.4 `loop_run_dir`) — reads ONE resolver, never a hardcoded vault literal
(§1.6).

`LOOP_OUTPUT_DIR_DEFAULT` lives HERE (a module constant), not in
`juggle_settings.py`, which is at its 460-line LOC budget (plan P0a). A config
default is legitimate config data; §1.6 forbids hardcoded vault literals in
loop/DISPATCH code, not the resolver's own schema default.
"""

from pathlib import Path

from juggle_settings import get_settings

# Fallback for `paths.loop_output_dir` when the key is absent from settings.
# Home-relative style, mirroring the `vault` default. Loop outputs live INSIDE
# the vault (spec §1.3, user 2026-07-04) so they are searchable in Obsidian.
LOOP_OUTPUT_DIR_DEFAULT = "/Documents/personal/projects/juggle/loops"


def _resolve_home_relative(value: str) -> Path:
    """Resolve a settings path the way the vault has ALWAYS resolved (verbatim
    from the former inline logic): a leading '~' is expanduser'd; anything else
    is treated as home-relative. The settings loader does NOT auto-expand
    `vault`/`loop_output_dir` (juggle_settings.py:434-436 expands only
    data_dir/config_dir/digest_log_dir), so this resolver owns the expansion.
    """
    if value.startswith("~"):
        return Path(value).expanduser()
    return Path.home() / value.lstrip("/")


def get_vault_root() -> Path:
    """Absolute Obsidian vault root — the single source of truth (spec §1.2).

    Byte-identical to the former `juggle_cli._get_vault_root` /
    `juggle_cmd_research._get_vault_info` inline resolution.
    """
    vault_val = get_settings()["paths"].get("vault", "/Documents/personal")
    return _resolve_home_relative(vault_val)


def get_loop_output_root() -> Path:
    """Absolute loop-output root — base for `loop_run_dir` (spec §1.3/§1.4).

    Sourced from `paths.loop_output_dir` with a module-constant fallback so
    `juggle_settings.py` (at its LOC budget) stays untouched (plan P0a).
    """
    val = get_settings()["paths"].get("loop_output_dir", LOOP_OUTPUT_DIR_DEFAULT)
    return _resolve_home_relative(val)
