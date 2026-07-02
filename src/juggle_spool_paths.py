"""juggle_spool_paths — resolve the spool directory (honors settings/test redirects)."""
from __future__ import annotations

import os
from pathlib import Path

from juggle_settings import get_settings


def spool_dir() -> Path:
    """Resolve the spool directory.

    ``JUGGLE_SPOOL_DIR`` (set) wins over everything — the fail-closed test
    guard (tests/conftest.py) redirects every test through this env var so a
    forgotten monkeypatch of ``spool_dir`` itself can never resolve to the
    real ``~/.juggle/spool`` (2026-07-02 incident: 79 production spool events
    were consumed/dead-lettered by test runs — see tests/test_spool_isolation.py).
    """
    override = os.environ.get("JUGGLE_SPOOL_DIR")
    if override:
        d = Path(override).expanduser()
    else:
        config_dir = Path(get_settings()["paths"]["config_dir"]).expanduser()
        d = config_dir / "spool"
    d.mkdir(parents=True, exist_ok=True)
    return d


def spool_dead_dir() -> Path:
    return spool_dir() / "dead"
