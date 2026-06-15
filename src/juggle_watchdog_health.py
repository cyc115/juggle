"""Watchdog liveness detection and child-process start helper.

The watchdog calls write_heartbeat() on every _poll_once. CLI commands call
is_watchdog_alive() to decide whether to warn about missing reaps.
The cockpit calls start_watchdog_child() to own the watchdog as a child process.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

HEARTBEAT_PATH = Path.home() / ".juggle" / "watchdog_heartbeat"
SINGLETON_PID_FILE = Path.home() / ".juggle" / "watchdog.pid"
_LOG_PATH = Path.home() / ".juggle" / "watchdog.log"
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


def read_heartbeat_age(heartbeat_path: Path = HEARTBEAT_PATH) -> float | None:
    """Return seconds since last heartbeat, or None if the file is missing."""
    try:
        return time.time() - heartbeat_path.stat().st_mtime
    except FileNotFoundError:
        return None


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def watchdog_needs_start(
    pid_alive: bool,
    heartbeat_age_s: float | None,
    threshold_s: float = 90,
) -> bool:
    """Pure decision: True if the watchdog should be (re)started."""
    if not pid_alive:
        return True
    if heartbeat_age_s is None or heartbeat_age_s > threshold_s:
        return True
    return False


def start_watchdog_child(
    pid_file: Path = SINGLETON_PID_FILE,
    heartbeat_path: Path = HEARTBEAT_PATH,
    log_path: Path = _LOG_PATH,
    repo_root: Path | None = None,
    supervisor_pid: int | None = None,
    threshold_s: float = 90,
) -> "subprocess.Popen | None":
    """Start the watchdog as a child process (same process group as caller).

    Idempotent: if a live watchdog already owns the pidfile and has a fresh
    heartbeat, return None without spawning. NOT detached — no start_new_session.
    The caller (cockpit) is responsible for terminating the child on exit.
    """
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            if _is_pid_alive(pid):
                age = read_heartbeat_age(heartbeat_path)
                if not watchdog_needs_start(True, age, threshold_s):
                    return None
        except (ValueError, OSError):
            pass

    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    log_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["JUGGLE_ORCHESTRATOR"] = "1"
    env.pop("JUGGLE_WATCHDOG_SUPERVISED", None)
    if supervisor_pid is not None:
        env["JUGGLE_SUPERVISOR_PID"] = str(supervisor_pid)

    log_fh = open(log_path, "a")  # noqa: SIM115 — kept open for subprocess lifetime
    proc = subprocess.Popen(
        ["uv", "run", "python", "src/juggle_watchdog_daemon.py"],
        stdout=log_fh,
        stderr=log_fh,
        cwd=str(repo_root),
        env=env,
    )
    log_fh.close()

    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(proc.pid))
    return proc
