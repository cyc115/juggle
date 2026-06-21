"""Daemon-survivor guard — fail-loud detection of leaked watchdog daemons.

2026-06-21 daemon-teardown leak (8 full-suite ERRORS): a test that spawns a real
``uv run python src/juggle_watchdog_daemon.py`` (the cockpit on_mount self-heal
path, or the daemon-entrypoint pin) can leave the DETACHED python daemon CHILD
alive when teardown reaps only the ``uv run`` parent. The orphan keeps ticking
against the test's tmp DB and contaminates the rest of the suite.

``scoped_daemon_survivors`` finds such a survivor SCOPED to a single test's
tmp_path: it reads each per-DB singleton-lock sidecar ``.<db>.watchdog.lock``
written under tmp_path (a live daemon records its own PID there) and returns the
PIDs that are (a) still alive and (b) a juggle_watchdog_daemon.py process.
Scoping by the tmp_path lock files means a concurrent xdist worker's daemon or
the live PROD watchdog is NEVER mis-attributed to this test. The scan is
non-recursive — the daemon-spawning tests all place their DB at tmp_path top
level — so for the 99% of tests with no lock sidecar it is a single ``scandir``
with no process introspection.
"""
from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

DAEMON_MARKER = "juggle_watchdog_daemon.py"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _is_daemon_proc(pid: int) -> bool:
    from daemon_pidfile import is_process

    return is_process(pid, DAEMON_MARKER)


def scoped_daemon_survivors(
    tmp_root,
    *,
    is_alive: Callable[[int], bool] = _pid_alive,
    is_daemon: Callable[[int], bool] = _is_daemon_proc,
) -> list[int]:
    """PIDs of live watchdog DAEMONS holding a singleton lock under ``tmp_root``.

    Reads every ``.*.watchdog.lock`` directly under ``tmp_root`` and returns the
    lock-recorded PIDs that are both alive and a daemon process. ``is_alive`` /
    ``is_daemon`` are injectable so the detection logic is unit-pinnable without
    a real ``ps``.
    """
    root = Path(tmp_root)
    survivors: list[int] = []
    try:
        locks = sorted(root.glob(".*.watchdog.lock"))
    except OSError:
        return survivors
    for lock in locks:
        try:
            pid = int(lock.read_text().strip())
        except (OSError, ValueError):
            continue
        if is_alive(pid) and is_daemon(pid):
            survivors.append(pid)
    return survivors


def reap_survivors(
    pids, *, killer: Callable[[int], None] | None = None
) -> None:
    """SIGTERM→SIGKILL each survivor so a leak never contaminates the rest of
    the suite. Best-effort; never raises. ``killer`` is injectable for tests."""
    for pid in pids:
        if killer is not None:
            killer(pid)
            continue
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except OSError:
                break
            time.sleep(0.05)
            if not _pid_alive(pid):
                break
