"""Watchdog liveness detection via heartbeat file.

The watchdog calls write_heartbeat() on every _poll_once. CLI commands call
is_watchdog_alive() to decide whether to warn about missing reaps.
"""
from __future__ import annotations

import time
from pathlib import Path

HEARTBEAT_PATH = Path.home() / ".juggle" / "watchdog_heartbeat"
_DEFAULT_STALE_SECS = 120  # 4× the default 30s poll interval


def write_heartbeat(heartbeat_path: Path = HEARTBEAT_PATH) -> None:
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_path.touch()


def is_watchdog_alive(
    heartbeat_path: Path = HEARTBEAT_PATH,
    stale_secs: int = _DEFAULT_STALE_SECS,
) -> bool:
    try:
        mtime = heartbeat_path.stat().st_mtime
        return (time.time() - mtime) < stale_secs
    except FileNotFoundError:
        return False
