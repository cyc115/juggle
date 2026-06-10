"""juggle_integrate_lock — per-repo merge-queue file lock for integrate.

Owns: lock path resolution, PID-liveness checks, acquire/release of the
per-repo lockfile used to serialize `_run_integrate` runs (the merge queue).
Must not own: the integration pipeline itself (juggle_cmd_integrate) or any
graph-node semantics.

Extracted verbatim from juggle_cmd_integrate (2026-06-10, autopilot Phase 3
mechanical split — the file was at its LOC-gate budget).
"""

from __future__ import annotations

import os
import time
from pathlib import Path


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


def acquire_repo_lock(repo_path: str, timeout_secs: float = 300.0) -> Path:
    """Acquire a per-repo file lock. Returns the lock path.

    Steals locks with a dead PID or age > timeout_secs.
    Raises RuntimeError if a live lock cannot be acquired within timeout_secs.
    Uses atomic rename to avoid races between concurrent integrations.
    """
    lock_path = _get_lock_path(repo_path)
    deadline = time.monotonic() + timeout_secs

    while True:
        if lock_path.exists():
            existing_pid, lock_ts = _read_lock(lock_path)
            lock_age = time.time() - lock_ts
            if not _pid_alive(existing_pid):
                lock_path.unlink(missing_ok=True)  # dead PID — steal
            elif time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Cannot acquire lock for {repo_path}: "
                    f"held by PID {existing_pid} for {lock_age:.0f}s"
                )
            elif lock_age > timeout_secs:
                lock_path.unlink(missing_ok=True)  # aged-out alive lock — steal
            else:
                time.sleep(0.05)
                continue

        # Atomic write: write temp then rename
        tmp = lock_path.with_suffix(".lock.tmp")
        tmp.write_text(f"{os.getpid()}\n{time.time()}\n")
        try:
            tmp.rename(lock_path)
        except OSError:
            tmp.unlink(missing_ok=True)
            continue  # Race lost — retry

        # Verify we won (another writer could have clobbered via rename race)
        pid, _ = _read_lock(lock_path)
        if pid == os.getpid():
            return lock_path


def release_repo_lock(lock_path: Path) -> None:
    """Remove the lock only if owned by the current process."""
    if not lock_path or not lock_path.exists():
        return
    pid, _ = _read_lock(lock_path)
    if pid == os.getpid():
        lock_path.unlink(missing_ok=True)
