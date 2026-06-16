"""Watchdog singleton + prod-launch guards (2026-06-16 incident fix).

Prevents the prod-DB pollution cascade: a worktree/test-launched watchdog daemon
must (a) REFUSE to run against the production DB unless launched through the
sanctioned orchestrator entrypoint, and (b) never run as a second concurrent
instance — an exclusive flock guarantees a single live daemon per DB. It also
provides the kill-ALL helper that `stop-watchdog` uses so a freeze actually
freezes every watchdog process, not just the one recorded in the pidfile.

Pure/IO-thin helpers only — no daemon loop or policy lives here.
"""
from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import time
from pathlib import Path

# Set ONLY by sanctioned launchers (`juggle start` / cockpit child spawn), never
# by the daemon itself and never by test runs. Its absence ⇒ not orchestrator-
# launched, so a prod-targeted daemon aborts immediately.
SANCTION_ENV = "JUGGLE_WATCHDOG_SANCTIONED"

# The one production DB that must never be touched by a worktree/test daemon.
PROD_DB_PATH = (Path.home() / ".claude" / "juggle" / "juggle.db").resolve()

# Cmdline substrings that identify a running watchdog process (both the thin
# script wrapper and the direct-module invocation used by the cockpit child).
WATCHDOG_PROC_PATTERNS = ("juggle-agent-watchdog", "juggle_watchdog_daemon.py")


class WatchdogLaunchRefused(RuntimeError):
    """A watchdog tried to run against prod without the orchestrator sanction."""


class WatchdogAlreadyRunning(RuntimeError):
    """A second watchdog tried to start while another holds the singleton lock."""


# ---------------------------------------------------------------------------
# Prod-launch sanction guard
# ---------------------------------------------------------------------------


def is_prod_db(db_path) -> bool:
    try:
        return Path(db_path).resolve() == PROD_DB_PATH
    except OSError:
        return False


def is_sanctioned() -> bool:
    return os.environ.get(SANCTION_ENV) == "1"


def assert_launch_allowed(db_path) -> None:
    """Refuse to start a watchdog against the prod DB unless sanctioned.

    A worktree- or test-launched daemon never sets ``SANCTION_ENV``, so this
    aborts it before it can tick against production. Non-prod (temp) DBs are
    always allowed.
    """
    if is_prod_db(db_path) and not is_sanctioned():
        raise WatchdogLaunchRefused(
            f"refusing to start watchdog against production DB {db_path} "
            f"without {SANCTION_ENV}=1 — only the orchestrator entrypoint may "
            f"start the prod watchdog (worktree/test launch blocked)."
        )


# ---------------------------------------------------------------------------
# Exclusive singleton flock (per DB)
# ---------------------------------------------------------------------------


def lock_path_for(db_path) -> Path:
    """Per-DB lock file so isolated test DBs get independent locks."""
    p = Path(db_path)
    return p.parent / f".{p.name}.watchdog.lock"


def acquire_singleton_lock(db_path):
    """Take an exclusive, non-blocking flock for this DB's watchdog.

    Returns the held fd — keep it open for the daemon's lifetime; the OS drops
    the lock automatically when the process dies. Raises WatchdogAlreadyRunning
    if another live watchdog already holds it.
    """
    lock = lock_path_for(db_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise WatchdogAlreadyRunning(
            f"another watchdog already holds the singleton lock {lock}"
        ) from exc
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    os.fsync(fd)
    return fd


def release_singleton_lock(fd) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Kill-ALL helper for stop-watchdog
# ---------------------------------------------------------------------------


def find_watchdog_pids(pattern: str | None = None) -> list[int]:
    """Return PIDs of every running watchdog process (excluding ourselves).

    ``pattern`` overrides the default production patterns (used by tests to
    target a unique marker without touching the real watchdog).
    """
    patterns = [pattern] if pattern else list(WATCHDOG_PROC_PATTERNS)
    pids: set[int] = set()
    me = os.getpid()
    for pat in patterns:
        try:
            res = subprocess.run(
                ["pgrep", "-f", pat], capture_output=True, text=True, timeout=5
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        for tok in res.stdout.split():
            try:
                pid = int(tok)
            except ValueError:
                continue
            if pid != me:
                pids.add(pid)
    return sorted(pids)


def terminate_all_watchdogs(
    pattern: str | None = None, *, timeout: float = 3.0
) -> list[int]:
    """SIGTERM every watchdog process, escalating to SIGKILL after ``timeout``.

    Returns the list of PIDs that were signalled. A freeze must actually freeze
    everything, so this targets ALL matching processes — not just a recorded
    pidfile entry.
    """
    pids = find_watchdog_pids(pattern)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _any_alive(pids):
            break
        time.sleep(0.1)
    for pid in pids:
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)  # still alive — escalate
        except (ProcessLookupError, PermissionError):
            pass
    return pids


def _any_alive(pids: list[int]) -> bool:
    for pid in pids:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            continue
    return False
