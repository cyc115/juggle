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

# When "1", ``ensure_watchdog`` never launches a REAL detached daemon (the
# ``spawn is None`` path). The test harness sets it so cockpit on_mount /
# ``juggle start`` — including in a `uv run` subprocess child, which inherits the
# env — cannot leak a background daemon onto a pytest tmp DB (2026-06-21
# daemon-teardown leak). An injected ``spawn=`` (unit tests) is always honored.
DISABLE_SPAWN_ENV = "JUGGLE_WATCHDOG_DISABLE_SPAWN"

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


def read_lock_pid(db_path) -> int | None:
    """PID recorded in this DB's watchdog lock file, or None."""
    try:
        txt = lock_path_for(db_path).read_text().strip()
        return int(txt) if txt else None
    except (FileNotFoundError, ValueError):
        return None


def is_watchdog_alive(db_path) -> bool:
    """True iff a LIVE watchdog currently holds this DB's singleton lock.

    Probe by trying to take the lock non-blocking: success ⇒ nobody holds it
    (release immediately, return False); contention ⇒ a live watchdog owns it.
    This is the canonical liveness check — the lock IS the singleton truth.
    """
    lock = lock_path_for(db_path)
    if not lock.exists():
        return False
    try:
        fd = os.open(str(lock), os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Lock-gated ensure-exists / lifecycle (cockpit + start + autopilot share these)
# ---------------------------------------------------------------------------


def canonical_repo_path(start: str | None = None) -> str:
    """The real main repo work-tree — never a worktree copy.

    A cockpit launched from a ``cyc_*`` worktree must still start the watchdog
    from the canonical main checkout (latest merged code), so resolve the
    primary work-tree via ``git worktree list`` (its first entry).
    """
    base = start or str(Path(__file__).resolve().parent.parent)
    try:
        res = subprocess.run(
            ["git", "-C", base, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                if line.startswith("worktree "):
                    return line[len("worktree "):].strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return base


def start_watchdog_detached(db_path, *, repo_path: str | None = None) -> int:
    """Spawn a DETACHED, sanctioned watchdog daemon from the canonical main repo.

    start_new_session detaches it from the launcher's session/process group so
    closing the cockpit (a different session) never takes it down. The daemon
    itself acquires the singleton lock; lock-gate via ``ensure_watchdog`` so two
    racing callers don't both spawn. Returns the launched PID.
    """
    repo = repo_path or canonical_repo_path()
    env = os.environ.copy()
    env["JUGGLE_DB_PATH"] = str(db_path)
    env["JUGGLE_ORCHESTRATOR"] = "1"
    env[SANCTION_ENV] = "1"
    env.pop("JUGGLE_WATCHDOG_SUPERVISED", None)
    log_dir = Path(db_path).parent
    log_dir.mkdir(parents=True, exist_ok=True)
    spawn_log = open(log_dir / "watchdog-spawn.log", "ab")
    try:
        proc = subprocess.Popen(
            ["uv", "run", "python", "src/juggle_watchdog_daemon.py"],
            cwd=repo,
            env=env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=spawn_log,
            stderr=spawn_log,
        )
    finally:
        # Child has dup'd the fd; close ours to avoid a leak.
        spawn_log.close()
    return proc.pid


# Spawn-lifecycle gates (respawn debounce + freeze sentinel) live in
# juggle_watchdog_lifecycle (extracted 2026-06-20 to keep this module under its
# LOC budget). Re-exported here so the historical call/import surface is intact.
from juggle_watchdog_lifecycle import (  # noqa: E402,F401
    default_min_respawn_interval,
    freeze_sentinel_path,
    freeze_watchdog,
    is_watchdog_frozen,
    read_last_spawn,
    record_spawn,
    should_suppress_spawn,
    spawn_stamp_path,
    unfreeze_watchdog,
)


def ensure_watchdog(
    db_path,
    *,
    repo_path: str | None = None,
    spawn=None,
    survive_timeout: float = 2.0,
    min_respawn_interval: float | None = None,
    force: bool = False,
) -> bool:
    """Lock-gated ensure-exists: start a detached watchdog only if none is live.

    Returns True only if a NEW watchdog was launched AND came up alive (acquired
    the lock) within ``survive_timeout``. Returns False if a live watchdog holds
    the lock, if the freeze sentinel or respawn debounce suppresses the spawn, or
    if the spawned daemon never survived. The lock is the authoritative singleton
    — a racing second launch is harmless (the loser fails to acquire and exits).

    Spawn gates (2026-06-20 leak): ``should_suppress_spawn`` folds (a) the freeze
    sentinel — a hard no-op even under ``force`` so ``stop-watchdog --freeze``
    holds against the 15s cockpit ensure — and (b) the respawn debounce — within
    ``min_respawn_interval`` (default ``watchdog.min_respawn_interval_secs``) of
    the last spawn, suppress a respawn even when the lock isn't held yet, so a
    slow cold-start isn't re-spawned 15s after 15s. ``force`` (W/R hotkey)
    bypasses the debounce but never the freeze.
    """
    if is_watchdog_alive(db_path):
        return False
    if min_respawn_interval is None:
        min_respawn_interval = default_min_respawn_interval()
    now = time.monotonic()
    if should_suppress_spawn(
        db_path, now=now, min_respawn_interval=min_respawn_interval, force=force
    ):
        return False
    # Test/CI backstop: never launch a REAL detached daemon when disabled. Only
    # the real-spawn path (spawn is None) is gated — an injected ``spawn`` (unit
    # tests) is always honored. Propagates to `uv run` subprocess children, so a
    # cockpit/CLI launched by a test cannot leak a daemon onto a tmp DB.
    if spawn is None and os.environ.get(DISABLE_SPAWN_ENV) == "1":
        return False

    record_spawn(db_path, now)
    (spawn or start_watchdog_detached)(db_path, repo_path=repo_path)
    deadline = time.monotonic() + survive_timeout
    while time.monotonic() < deadline:
        if is_watchdog_alive(db_path):
            return True
        time.sleep(0.05)
    return is_watchdog_alive(db_path)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_watchdog(db_path, *, timeout: float = 3.0) -> bool:
    """Gracefully stop the watchdog holding this DB's lock (SIGTERM→SIGKILL).

    Killing the holder releases the flock. Targets the lock-file PID (DB-scoped)
    — never a broad pattern kill that could take down another DB's watchdog.
    Returns True if a live process was signalled.
    """
    pid = read_lock_pid(db_path)
    if pid is None or not _pid_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return True


def toggle_watchdog(db_path, *, repo_path: str | None = None, spawn=None) -> str:
    """W hotkey: stop a live watchdog, or start one if none. Returns the action.

    ``force=True`` on the start path: an explicit user action must never be
    swallowed by the respawn debounce (which only throttles the automatic 15s
    cockpit ensure)."""
    if is_watchdog_alive(db_path):
        stop_watchdog(db_path)
        return "stopped"
    # Explicit start is an explicit unfreeze — lift any freeze before spawning.
    unfreeze_watchdog(db_path)
    ensure_watchdog(db_path, repo_path=repo_path, spawn=spawn, force=True)
    return "started"


def restart_watchdog(
    db_path,
    *,
    repo_path: str | None = None,
    spawn=None,
    survive_timeout: float = 2.0,
) -> bool:
    """R hotkey: kill the existing watchdog and relaunch from the canonical main
    path (the 'always run latest code' path). Returns True only if the relaunched
    daemon survived to acquire the singleton lock within ``survive_timeout``."""
    stop_watchdog(db_path)
    deadline = time.monotonic() + 3.0
    while is_watchdog_alive(db_path) and time.monotonic() < deadline:
        time.sleep(0.05)
    # Explicit restart is an explicit unfreeze; force=True also bypasses the
    # respawn debounce.
    unfreeze_watchdog(db_path)
    return ensure_watchdog(
        db_path, repo_path=repo_path, spawn=spawn,
        survive_timeout=survive_timeout, force=True,
    )


def release_singleton_lock(fd) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


# Watchdog process enumeration + kill-ALL moved to juggle_reaper (process killing
# is the reaper's domain). Re-exported so `from juggle_watchdog_singleton import
# find_watchdog_pids / terminate_all_watchdogs` keeps working (stop-watchdog,
# tests). WATCHDOG_PROC_PATTERNS stays here as the canonical 'what is a watchdog'
# definition; the reaper imports it lazily to avoid an import cycle.
from juggle_reaper import (  # noqa: E402,F401
    find_watchdog_pids,
    terminate_all_watchdogs,
)
