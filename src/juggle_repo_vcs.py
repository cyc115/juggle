"""juggle_repo_vcs — per-repo VCS policy resolution (SPEC 2026-07-05 async-land).

The single reader of a repo's VCS-policy config, shared by the ancestor-gate
guards (dbops.graph_guards), the integrate publish path (juggle_integrate_submit),
and (spec #2) ``/juggle:vcs-backend-init``'s config writer — so the reader and the
writer can't drift.

Config key path (in ~/.juggle/config.json, one entry per absolute repo path;
resolved exactly like juggle_settings.get_repo_config's ``repos.<path>`` lookup):

    repos.<abs-repo-path>.vcs         # backend-name override; read by get_repo_config
    repos.<abs-repo-path>.trunk       # trunk/branch name the ancestor gate checks
                                      #   merged_sha against; default "main" (git)
    repos.<abs-repo-path>.async_land  # bool | absent -> default from the backend's
                                      #   Capabilities.async_land

Kept separate from juggle_settings (its LOC budget) — juggle_settings owns the
DEFAULTS + load order; this owns only the async-land/trunk policy resolution.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from juggle_settings import get_settings


def _repo_cfg(repo_path: str) -> dict:
    """The raw ``repos.<abs-repo-path>`` config block ({} for an unknown repo).
    Same key resolution as juggle_settings.get_repo_config."""
    return get_settings().get("repos", {}).get(str(repo_path), {})


def repo_trunk(repo_path: str) -> str:
    """Trunk/branch name the verified<=>merged ancestor gate checks against.

    Config ``repos.<repo>.trunk``; defaults to git's conventional "main". A
    non-"main" trunk (e.g. a Sapling ``remote/main``) MUST be configured here or
    the ancestor gate false-refuses every land (2026-07-05 git-ism sweep)."""
    return _repo_cfg(repo_path).get("trunk") or "main"


def repo_async_land(repo_path: str, backend) -> bool:
    """Whether publishing is submit-for-async-land (Phabricator/Gerrit/Sapling)
    vs a synchronous land.

    Config ``repos.<repo>.async_land`` when set explicitly (an operator override),
    else the backend's own ``Capabilities.async_land``. Policy branches on the
    CAPABILITY flag, NEVER the backend name (vcs_types.Capabilities contract)."""
    override = _repo_cfg(repo_path).get("async_land")
    if override is not None:
        return bool(override)
    caps = getattr(backend, "capabilities", None)
    return bool(caps is not None and caps.async_land)


def _config_path() -> Path:
    """The config.json path (``_JUGGLE_CONFIG_PATH`` override), same resolution
    as juggle_settings — so read and write hit the same file."""
    return Path(
        os.environ.get(
            "_JUGGLE_CONFIG_PATH", str(Path.home() / ".juggle" / "config.json")
        )
    )


def write_repo_vcs_config(
    repo_path: str,
    *,
    vcs: str,
    trunk: str,
    async_land: bool,
    submit_cmd: str | None = None,
    land_status_cmd: str | None = None,
) -> None:
    """Bind ``repo_path`` to a VCS backend in config.json (vcs-backend-init
    step 3). Writes the EXACT keys the readers above consume — ``vcs`` (backend
    override), ``trunk`` (ancestor-gate ref), ``async_land`` (publish policy) —
    plus optional ``submit_cmd``/``land_status_cmd`` operator hints, merged into
    ``repos.<repo_path>`` without disturbing any other config. Empty commands are
    omitted (git needs none). Fail-loud on an unwritable path (never a silent
    no-op that leaves the repo unbound)."""
    path = _config_path()
    try:
        cfg = json.loads(path.read_text())
    except (OSError, ValueError):
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}

    block: dict = {"vcs": vcs, "trunk": trunk, "async_land": bool(async_land)}
    if submit_cmd:
        block["submit_cmd"] = submit_cmd
    if land_status_cmd:
        block["land_status_cmd"] = land_status_cmd

    repos = cfg.setdefault("repos", {})
    existing = repos.get(str(repo_path), {})
    existing.update(block)
    repos[str(repo_path)] = existing

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2))
