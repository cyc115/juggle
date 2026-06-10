"""daemon_pidfile — single source of truth for daemon pidfile/singleton logic.

Owns: cmdline-verified process probes, kill-previous-instance-from-pidfile,
atomic singleton pidfile write (optionally race-verified), and pidfile cleanup.
Must not own: any daemon loop, polling, or domain logic.

Used by scripts/juggle-agent-monitor, scripts/juggle-agent-watchdog, and
src/juggle_watchdog.py — each call site keeps its historical semantics via
parameters (see the 2026-06-10 refactor plan, Phase 1; unifying semantics is
a deferred behavior change).
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable


def is_process(pid: int, cmdline_substr: str, *, case_insensitive: bool = False) -> bool:
    """Return True if the process with given PID has cmdline_substr in its command line."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        haystack = result.stdout.lower() if case_insensitive else result.stdout
        needle = cmdline_substr.lower() if case_insensitive else cmdline_substr
        return needle in haystack
    except Exception:
        return False


def kill_existing_from_pidfile(
    pidfile_path: Path,
    is_target: Callable[[int], bool],
    *,
    log: logging.Logger | None = None,
    name: str = "daemon",
) -> None:
    """Kill the daemon recorded in pidfile_path — only if is_target(pid) confirms it.

    Verifies cmdline (via is_target) before sending any signal so stale PID
    files pointing at unrelated processes are never acted upon. Sends SIGTERM,
    waits up to 2s, then escalates to SIGKILL. Silent when log is None.
    """
    if not pidfile_path.exists():
        return
    try:
        old_pid = int(pidfile_path.read_text().strip())
    except (ValueError, OSError):
        return
    if old_pid == os.getpid():
        return
    try:
        os.kill(old_pid, 0)  # existence probe
    except (ProcessLookupError, PermissionError):
        return  # stale pidfile — process gone
    if not is_target(old_pid):
        if log is not None:
            log.warning(
                "%s: PID %d in %s is not a %s — skipping kill",
                name, old_pid, pidfile_path, name,
            )
        return
    try:
        os.kill(old_pid, signal.SIGTERM)
        for _ in range(20):
            time.sleep(0.1)
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                break
        else:
            os.kill(old_pid, signal.SIGKILL)
        if log is not None:
            log.info("%s: killed previous instance (PID %d)", name, old_pid)
    except (ProcessLookupError, PermissionError):
        pass


def write_singleton_pid(
    pidfile: Path,
    *,
    verify: bool = False,
    log: logging.Logger | None = None,
    name: str = "daemon",
) -> None:
    """Atomically write our PID to pidfile (write tmp + rename).

    With verify=True, re-read the file afterwards and exit(1) if another PID
    claimed it (guards against a race with another concurrent start).
    """
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    tmp = pidfile.with_suffix(".pid.tmp")
    tmp.write_text(str(os.getpid()))
    tmp.replace(pidfile)
    if verify:
        try:
            claimed = int(pidfile.read_text().strip())
            if claimed != os.getpid():
                (log or logging).warning(
                    "%s: pidfile claimed by PID %d after write — exiting",
                    name, claimed,
                )
                sys.exit(1)
        except (ValueError, OSError):
            pass


def cleanup_singleton_pid(pidfile: Path) -> None:
    """Remove pidfile if (and only if) it still records our own PID."""
    try:
        if pidfile.exists():
            if int(pidfile.read_text().strip()) == os.getpid():
                pidfile.unlink()
    except Exception:
        pass
