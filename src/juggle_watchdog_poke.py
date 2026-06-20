"""juggle_watchdog_poke — SIGUSR1 tick-on-demand helper (P4).

Owns: poke_watchdog() — signal the live watchdog to tick immediately.
Callers: ready-writers (recompute_ready, recompute_topic_ready) after
         promoting nodes to 'ready'. No-op when no live watchdog.

Design: pure function, no DB open, reads PID from singleton lock file.
The 30s periodic backstop in the daemon covers any dropped/missed signal.
"""
from __future__ import annotations

import logging
import os
import signal

_log = logging.getLogger(__name__)


def read_lock_pid(db_path) -> int | None:
    """PID recorded in this DB's watchdog lock file, or None."""
    from juggle_watchdog_singleton import read_lock_pid as _read
    return _read(db_path)


def poke_watchdog(db_path) -> None:
    """Send SIGUSR1 to the live watchdog so it ticks immediately.

    No-op (no exception) when:
    - No PID file / PID is None (watchdog not running)
    - Process at that PID is dead (ProcessLookupError)
    - We lack permission (PermissionError — shouldn't happen for same-user process)

    The 30s periodic backstop ensures the tick still runs even if the signal
    is missed or the watchdog is not running.
    """
    pid = read_lock_pid(db_path)
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGUSR1)
    except (ProcessLookupError, PermissionError):
        pass  # watchdog not running; 30s backstop will handle it
    except OSError:
        pass
