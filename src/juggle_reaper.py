"""Watchdog-daemon reaper + process enumeration / kill-ALL.

RCA 2026-06-20 §6: NOTHING reaped a watchdog daemon whose worktree/DB had
vanished (it kept ticking forever), and the per-DB flock is not a global cap, so
N tmp DBs ran N daemons → ~109 detached daemons over ~8h. Two backstops, wired
into the watchdog tick: ``reap_orphan_watchdog_daemons`` (SIGTERM any daemon
whose JUGGLE_DB_PATH file or cwd is gone) and ``enforce_daemon_cap`` (global cap;
kills the oldest over the cap and ALWAYS logs — no silent cap). Also the
``find_watchdog_pids`` / ``terminate_all_watchdogs`` kill-ALL helpers (process
killing is the reaper's domain). Pure seams: process-introspection / kill / log
are injected so policy is unit-testable; default readers shell out to
``ps``/``lsof`` (macOS) or ``/proc`` (Linux).
"""
from __future__ import annotations

import logging
import os
import signal
import subprocess
from collections.abc import Callable
from pathlib import Path

_log = logging.getLogger("juggle-watchdog")


# ---------------------------------------------------------------------------
# Default process-introspection readers (injected in tests)
# ---------------------------------------------------------------------------


def read_proc_db_path(pid: int) -> str | None:
    """Return the ``JUGGLE_DB_PATH`` env value of process ``pid``, or None.

    Parsed from ``ps eww -p <pid>`` (same technique juggle already uses to read
    a pane's JUGGLE_IS_AGENT env). None on any failure / if the var is absent.
    """
    try:
        out = subprocess.run(
            ["ps", "eww", "-p", str(pid)],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None
    for tok in out.split():
        if tok.startswith("JUGGLE_DB_PATH="):
            val = tok[len("JUGGLE_DB_PATH="):]
            return val or None
    return None


def read_proc_cwd(pid: int) -> str | None:
    """Return the working directory of process ``pid``, or None.

    Linux: readlink /proc/<pid>/cwd. macOS/BSD: ``lsof -a -p <pid> -d cwd``.
    None on any failure (conservative — caller treats unknown as 'not orphan').
    """
    proc_cwd = Path(f"/proc/{pid}/cwd")
    if proc_cwd.exists():
        try:
            return os.readlink(proc_cwd)
        except OSError:
            return None
    try:
        out = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None
    # lsof -Fn emits a line "n<path>" for the cwd fd.
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def read_proc_start_time(pid: int) -> float | None:
    """Return an orderable process-start key (lower = older), or None.

    Uses ``ps -o lstart=`` parsed via the elapsed-seconds column ``etimes`` when
    available; falls back to the raw etime. We only need a relative ordering, so
    ``etimes`` (seconds since start, larger = older) is negated to make lower =
    older for a stable "kill the oldest" sort.
    """
    try:
        out = subprocess.run(
            ["ps", "-o", "etimes=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None
    try:
        etimes = int(out)
    except (TypeError, ValueError):
        return None
    # Larger etimes = older process. We want lower = older for the cap sort, so
    # negate: an older daemon gets a smaller key and is killed first.
    return float(-etimes)


def _default_killer(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


# --- Watchdog process enumeration + kill-ALL (re-exported from singleton) -----


def find_watchdog_pids(pattern: str | None = None) -> list[int]:
    """Return PIDs of every running watchdog process (excluding ourselves).

    ``pattern`` overrides the default production patterns (used by tests to
    target a unique marker without touching the real watchdog).
    """
    from juggle_watchdog_singleton import WATCHDOG_PROC_PATTERNS

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


def _any_alive(pids: list[int]) -> bool:
    for pid in pids:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            continue
    return False


def terminate_all_watchdogs(
    pattern: str | None = None, *, timeout: float = 3.0
) -> list[int]:
    """SIGTERM every watchdog process, escalating to SIGKILL after ``timeout``.

    Returns the list of PIDs that were signalled. A freeze must actually freeze
    everything, so this targets ALL matching processes — not just a recorded
    pidfile entry.
    """
    import time as _time

    pids = find_watchdog_pids(pattern)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if not _any_alive(pids):
            break
        _time.sleep(0.1)
    for pid in pids:
        try:
            os.kill(pid, 0)
            os.kill(pid, signal.SIGKILL)  # still alive — escalate
        except (ProcessLookupError, PermissionError):
            pass
    return pids


# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


def daemon_is_orphan(db_path: str | None, cwd: str | None) -> bool:
    """True iff this daemon's DB file OR working dir has vanished.

    Conservative: an unknown (None) DB path returns False — we never reap a
    daemon we can't positively identify as orphaned. An unknown cwd alone does
    NOT mark orphan (cwd is best-effort); only a *known-missing* cwd does.
    """
    if not db_path:
        return False
    if not Path(db_path).exists():
        return True
    if cwd is not None and not Path(cwd).exists():
        return True
    return False


# ---------------------------------------------------------------------------
# Reapers (injected dependencies default to the real readers above)
# ---------------------------------------------------------------------------


def reap_orphan_watchdog_daemons(
    *,
    pids: list[int] | None = None,
    db_path_reader: Callable[[int], str | None] = read_proc_db_path,
    cwd_reader: Callable[[int], str | None] = read_proc_cwd,
    killer: Callable[[int], None] = _default_killer,
    log: Callable[[str], None] | None = None,
) -> list[int]:
    """SIGTERM every watchdog daemon whose DB file or worktree no longer exists.

    Returns the PIDs reaped. Each reap is logged (RCA: no silent reaps). ``pids``
    defaults to the live watchdog processes (``find_watchdog_pids``, which
    already excludes the current process).
    """
    emit = log or _log.warning
    if pids is None:
        pids = find_watchdog_pids()

    reaped: list[int] = []
    for pid in pids:
        db_path = db_path_reader(pid)
        cwd = cwd_reader(pid)
        if daemon_is_orphan(db_path, cwd):
            killer(pid)
            reaped.append(pid)
            emit(
                f"[reaper] killed orphan watchdog daemon pid={pid} "
                f"(db_path={db_path!r} cwd={cwd!r} no longer exists)"
            )
    return reaped


def enforce_daemon_cap(
    max_daemons: int,
    *,
    pids: list[int] | None = None,
    start_time_reader: Callable[[int], float | None] = read_proc_start_time,
    killer: Callable[[int], None] = _default_killer,
    log: Callable[[str], None] | None = None,
) -> list[int]:
    """Global backstop: if live daemons exceed ``max_daemons``, kill the oldest
    over the cap and LOG it (RCA P1 — the per-DB flock is not a global cap).

    Lowest ``start_time_reader`` key = oldest = killed first. A cap <= 0 disables
    the backstop. Daemons with an unreadable start key sort last (kept). Returns
    the PIDs reaped.
    """
    emit = log or _log.warning
    if max_daemons <= 0:
        return []
    if pids is None:
        pids = find_watchdog_pids()

    if len(pids) <= max_daemons:
        return []

    # Sort oldest-first; unknown start keys (None) sort last so we never kill a
    # daemon we can't age. A large sentinel keeps None at the tail.
    def _key(pid: int) -> float:
        st = start_time_reader(pid)
        return st if st is not None else float("inf")

    ordered = sorted(pids, key=_key)
    over = len(pids) - max_daemons
    victims = ordered[:over]

    reaped: list[int] = []
    for pid in victims:
        killer(pid)
        reaped.append(pid)
    if reaped:
        emit(
            f"[reaper] daemon cap ({max_daemons}) exceeded: {len(pids)} live "
            f"watchdog daemons — killed oldest {len(reaped)}: {reaped}"
        )
    return reaped


def reap_watchdog_daemons_tick(max_daemons: int) -> None:
    """One tick of both daemon backstops — fail-safe (never raises).

    Called from the watchdog poll loop. Orphan reap first (frees obvious
    leaks), then the global cap as the catch-all.
    """
    try:
        reap_orphan_watchdog_daemons()
    except Exception:
        _log.exception("[reaper] orphan-daemon reap failed — continuing")
    try:
        enforce_daemon_cap(max_daemons)
    except Exception:
        _log.exception("[reaper] daemon-cap enforce failed — continuing")
