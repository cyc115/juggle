"""juggle_integrate_lock — per-repo merge-queue file lock for integrate.

Owns: lock path resolution, PID-liveness checks, acquire/release of the
per-repo lockfile used to serialize `_run_integrate` runs (the merge queue).
Must not own: the integration pipeline itself (juggle_cmd_integrate) or any
graph-task semantics.

Extracted verbatim from juggle_cmd_integrate (2026-06-10, autopilot Phase 3
mechanical split — the file was at its LOC-gate budget).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

_log = logging.getLogger(__name__)

# Holder refreshes the lockfile timestamp at this cadence while it works
# (DA M2: long test_cmd runs must look live to waiters/operators).
HEARTBEAT_INTERVAL_SECS = 30.0

# A live-pid lock whose timestamp is stale beyond this multiple of the
# heartbeat interval is a PHANTOM: the recorded holder never started
# heartbeating (2026-07-04 phantom-holder outage) and is by construction NOT
# mid-gate, so a waiter may steal it. DA M2's "never steal a live pid" assumed
# the holder heartbeats; a live pid with a dead heartbeat is not a real holder.
PHANTOM_HEARTBEAT_MULTIPLE = 10.0

# Global serialized integrate lock (#5038, 2026-07-01 integrate storm): EVERY
# integrate — autopilot or not — BLOCKS on the per-repo merge queue with this
# generous safety valve. It is NOT a normal-path deadline: waiters queue behind
# the holder (which is running the full suite inside the lock) and win as soon
# as it releases. 1800s only trips on a genuine hang → fail LOUD, no partial
# merge. Replaces the old 300s-timeout-and-FAIL that let N concurrent suites
# thrash CPU and trip waiters' short deadline (tasks left verified-but-unmerged).
INTEGRATE_LOCK_TIMEOUT_SECS = 1800.0

# Back-compat alias: autopilot fan-in completions already used this generous
# wait; it now applies uniformly to all integrates.
AUTOPILOT_LOCK_TIMEOUT_SECS = INTEGRATE_LOCK_TIMEOUT_SECS

# Live heartbeat threads for locks held by THIS process, keyed by lock path.
_heartbeats: dict[str, tuple[threading.Event, threading.Thread]] = {}
_heartbeats_mutex = threading.Lock()


def _start_heartbeat(lock_path: Path, interval: float) -> None:
    stop = threading.Event()

    def _beat() -> None:
        payload_pid = os.getpid()
        while not stop.wait(interval):
            try:
                lock_path.write_text(f"{payload_pid}\n{time.time()}\n")
            except OSError:
                return

    t = threading.Thread(
        target=_beat, daemon=True, name=f"repo-lock-heartbeat-{lock_path.name}"
    )
    t.start()
    with _heartbeats_mutex:
        _heartbeats[str(lock_path)] = (stop, t)


def _stop_heartbeat(lock_path: Path) -> None:
    with _heartbeats_mutex:
        entry = _heartbeats.pop(str(lock_path), None)
    if entry:
        stop, t = entry
        stop.set()
        t.join(timeout=2.0)


def _holds_own_heartbeat(lock_path: Path) -> bool:
    """True if THIS process has a live heartbeat for lock_path (i.e. it has
    verified ownership). Used to distinguish a genuinely-held lock from a
    lockfile that merely happens to record our pid (a poisoned self-hold)."""
    with _heartbeats_mutex:
        return str(lock_path) in _heartbeats


def _get_lock_path(repo_path: str) -> Path:
    from juggle_settings import get_settings
    config_dir = Path(get_settings()["paths"]["config_dir"]).expanduser()
    locks_dir = config_dir / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(repo_path).name.replace(" ", "_")
    return locks_dir / f"{safe_name}.lock"


def _read_lock(lock_path: Path) -> tuple[int, float]:
    """Return (pid, timestamp) from lock file; (0, 0.0) on any parse error."""
    try:
        parts = lock_path.read_text().strip().splitlines()
        return int(parts[0]), float(parts[1])
    except (OSError, ValueError, IndexError):
        return 0, 0.0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # EPERM: process exists, we just can't signal it


def acquire_repo_lock(
    repo_path: str,
    timeout_secs: float = 300.0,
    heartbeat_interval: float = HEARTBEAT_INTERVAL_SECS,
) -> Path:
    """Acquire a per-repo file lock. Returns the lock path.

    Acquisition is an ATOMIC exclusive create (``O_CREAT|O_EXCL``): exactly one
    of N racing acquirers can create the lockfile, so the winner's own pid is
    always what the file records — no phantom holder (2026-07-04 phantom-holder
    outage RC5, which the old shared-tmp + rename-replace scheme caused: POSIX
    rename silently replaces, so two racers could leave one process's pid in a
    lock another believed it won).

    Steal rules for an existing lock:
    - dead holder pid → steal;
    - a lockfile recording THIS process's own pid that we never verified
      ownership of (no live heartbeat) → poisoned self-hold; remove and retry
      (belt-and-braces — unreachable given atomic create; logged loudly);
    - a LIVE holder whose heartbeat is stale beyond
      ``PHANTOM_HEARTBEAT_MULTIPLE`` × ``heartbeat_interval`` → phantom holder
      that never started heartbeating; steal (logged loudly);
    - a LIVE holder with a fresh heartbeat → waited on until ``timeout_secs``,
      then RuntimeError (DA M2: never steal a live, heartbeating holder).

    While held, a daemon heartbeat thread refreshes the lockfile timestamp
    every ``heartbeat_interval`` seconds; ``release_repo_lock`` stops it.
    """
    lock_path = _get_lock_path(repo_path)
    deadline = time.monotonic() + timeout_secs
    my_pid = os.getpid()
    phantom_threshold = heartbeat_interval * PHANTOM_HEARTBEAT_MULTIPLE

    while True:
        # (a) Atomic exclusive create — the ONLY way to become the holder.
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            fd = None
        if fd is not None:
            try:
                os.write(fd, f"{my_pid}\n{time.time()}\n".encode())
            finally:
                os.close(fd)
            _start_heartbeat(lock_path, heartbeat_interval)
            return lock_path

        # Lock exists — decide whether to steal, wait, or fail.
        existing_pid, lock_ts = _read_lock(lock_path)
        lock_age = time.time() - lock_ts

        if not _pid_alive(existing_pid):
            lock_path.unlink(missing_ok=True)  # dead PID — steal
            continue

        # (b) Self-hold detection: the lockfile records our own live pid but we
        # never verified ownership of it (no heartbeat). Poisoned — recover.
        if existing_pid == my_pid and not _holds_own_heartbeat(lock_path):
            _log.error(
                "acquire_repo_lock: poisoned self-hold on %s (records our own "
                "pid %d, no verified heartbeat) — removing and retrying "
                "[2026-07-04 phantom-holder]", lock_path, my_pid,
            )
            lock_path.unlink(missing_ok=True)
            continue

        # (c) Phantom detection: a live pid whose heartbeat is stale beyond the
        # threshold never started heartbeating → not a real holder; steal.
        if lock_age > phantom_threshold:
            _log.error(
                "acquire_repo_lock: stealing phantom lock on %s — live PID %d "
                "but heartbeat stale %.0fs (> %.0fs threshold); recorded holder "
                "never heartbeat [2026-07-04 phantom-holder]",
                lock_path, existing_pid, lock_age, phantom_threshold,
            )
            lock_path.unlink(missing_ok=True)
            continue

        # Live holder, fresh heartbeat — wait (never steal).
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Cannot acquire lock for {repo_path}: "
                f"held by live PID {existing_pid} for {lock_age:.0f}s "
                f"(waited {timeout_secs:.0f}s; live locks are never stolen)"
            )
        time.sleep(0.05)


def release_repo_lock(lock_path: Path) -> None:
    """Remove the lock only if owned by the current process."""
    if not lock_path:
        return
    _stop_heartbeat(lock_path)
    if not lock_path.exists():
        return
    pid, _ = _read_lock(lock_path)
    if pid == os.getpid():
        lock_path.unlink(missing_ok=True)
