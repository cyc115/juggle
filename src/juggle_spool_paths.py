"""juggle_spool_paths — resolve the spool directory (honors settings/test redirects)."""
from __future__ import annotations

from pathlib import Path

from juggle_settings import get_settings


def spool_dir() -> Path:
    config_dir = Path(get_settings()["paths"]["config_dir"]).expanduser()
    d = config_dir / "spool"
    d.mkdir(parents=True, exist_ok=True)
    return d


def spool_dead_dir() -> Path:
    return spool_dir() / "dead"
