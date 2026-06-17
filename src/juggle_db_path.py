"""juggle_db_path — pure DB-path resolver.

Given (mode, tmpfs_dir, durable_path, instance_id) returns a DbPaths
dataclass with live/durable/mode fields.

direct mode: live == durable == durable_path (current behaviour, zero change).
tmpfs mode on Linux: live = tmpfs_dir/juggle-<instance>.db, durable = durable_path.
tmpfs mode on macOS: falls back to direct + logs a warning (no /dev/shm).
"""

from __future__ import annotations

import logging
import platform
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)


@dataclass
class DbPaths:
    live: Path
    durable: Path
    mode: str  # "direct" or "tmpfs" (after any fallback)


def resolve_db_paths(
    mode: str,
    tmpfs_dir: str,
    durable_path: Path,
    instance_id: str,
    *,
    _platform: str | None = None,
) -> DbPaths:
    """Resolve live and durable DB paths.

    Args:
        mode: "direct" or "tmpfs"
        tmpfs_dir: directory for tmpfs live DB (e.g. /dev/shm)
        durable_path: permanent on-disk path
        instance_id: unique string included in the tmpfs filename
        _platform: override platform for testing (defaults to sys.platform)
    """
    durable_path = Path(durable_path)

    if mode not in ("direct", "tmpfs"):
        raise ValueError(f"Unknown db.mode {mode!r}; must be 'direct' or 'tmpfs'")

    if mode == "direct":
        return DbPaths(live=durable_path, durable=durable_path, mode="direct")

    # tmpfs mode
    sys_platform = _platform or platform.system().lower()
    if sys_platform in ("darwin", "windows"):
        _log.warning(
            "db.mode=tmpfs requested but %s has no /dev/shm — "
            "falling back to direct mode",
            sys_platform,
        )
        return DbPaths(live=durable_path, durable=durable_path, mode="direct")

    live_path = Path(tmpfs_dir) / f"juggle-{instance_id}.db"
    return DbPaths(live=live_path, durable=durable_path, mode="tmpfs")


def check_tmpfs_writable(tmpfs_dir: str) -> None:
    """Raise if tmpfs_dir is missing or not writable (split-brain guard).

    Call this at startup when mode=tmpfs to hard-fail rather than silently
    falling back or writing to the wrong path.

    Raises:
        FileNotFoundError: if tmpfs_dir does not exist
        PermissionError: if tmpfs_dir exists but is not writable
    """
    p = Path(tmpfs_dir)
    if not p.exists():
        raise FileNotFoundError(
            f"db.mode=tmpfs but tmpfs_dir {tmpfs_dir!r} does not exist. "
            "Create it or switch db.mode to 'direct'."
        )
    import os
    if not os.access(str(p), os.W_OK):
        raise PermissionError(
            f"db.mode=tmpfs but tmpfs_dir {tmpfs_dir!r} is not writable. "
            "Fix permissions or switch db.mode to 'direct'."
        )
