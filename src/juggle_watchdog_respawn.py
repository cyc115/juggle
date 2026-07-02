"""juggle_watchdog_respawn — idempotent startup reconciliation (2026-07-01 churn fix).

Owns the boot-HEAD sidecar and the decision to kill-and-replace the pidfile
incumbent vs defer to it. Extracted from juggle_watchdog_daemon to keep that
module under its LOC budget — no daemon loop or policy lives here.

Incident 2026-07-01: after a code-advance exit, concurrent CLI respawns each
started a daemon that unconditionally SIGTERM'd the pidfile incumbent BEFORE the
singleton flock acquire, so two SAME-code fresh daemons mutually killed each other
(4 restarts in ~64s). ``reconcile_existing_watchdog`` makes that startup step
idempotent: kill only an OLD-code or hung incumbent; defer to a fresh same-code
peer so the flock — not a mutual SIGTERM — resolves the race to one survivor.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

# Boot-HEAD sidecar: the live daemon records the git HEAD it booted on so a
# near-simultaneous respawn can tell a fresh SAME-code peer from an OLD-code one.
SINGLETON_CODEVERSION_FILE = Path.home() / ".juggle" / "watchdog.codeversion"


def read_incumbent_code_version(path: Path = SINGLETON_CODEVERSION_FILE) -> str | None:
    """Boot HEAD recorded by the daemon currently holding the singleton, or None."""
    try:
        v = path.read_text().strip()
        return v or None
    except OSError:
        return None


def record_boot_code_version(
    version: str | None, path: Path = SINGLETON_CODEVERSION_FILE
) -> None:
    """Publish our boot HEAD so a racing respawn can classify us as same/old code."""
    if version is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(version)
    except OSError:
        pass


_UNSET = object()


def _default_stale_after() -> float:
    """Hung-heartbeat threshold from ``watchdog.hung_heartbeat_secs`` (default 120)."""
    try:
        from juggle_settings import get_settings
        return float(get_settings().get("watchdog", {}).get("hung_heartbeat_secs", 120))
    except Exception:
        return 120.0


def reconcile_existing_watchdog(
    boot_code_version: str | None,
    *,
    pidfile: Path,
    kill_fn: Callable[[Path], None],
    heartbeat_age: float | None = _UNSET,  # type: ignore[assignment]
    stale_after: float | None = None,
    codeversion_path: Path = SINGLETON_CODEVERSION_FILE,
    log: logging.Logger | None = None,
) -> bool:
    """Idempotent startup reconciliation — returns True iff the incumbent was killed.

    Kills the ``pidfile`` incumbent (via ``kill_fn``) ONLY when it runs OLD code
    (recorded boot HEAD differs from ``boot_code_version``) or is HUNG
    (``heartbeat_age`` older than ``stale_after``). A fresh, same-code, live
    incumbent is left alone: the caller's next flock acquire then makes this
    redundant newcomer exit — no restart storm.

    ``heartbeat_age``/``stale_after`` default to the live heartbeat sidecar and the
    configured hung threshold; both are injectable for tests.
    """
    from juggle_watchdog_restart import should_replace_incumbent

    if heartbeat_age is _UNSET:
        from juggle_watchdog_health import read_heartbeat_age
        heartbeat_age = read_heartbeat_age()
    if stale_after is None:
        stale_after = _default_stale_after()
    incumbent_version = read_incumbent_code_version(codeversion_path)
    if boot_code_version is None or incumbent_version is None:
        same_code: bool | None = None
    else:
        same_code = incumbent_version == boot_code_version
    if should_replace_incumbent(
        same_code=same_code, heartbeat_age=heartbeat_age, stale_after=stale_after
    ):
        kill_fn(pidfile)
        return True
    if log is not None:
        log.info(
            "Watchdog: fresh same-code instance already live (incumbent HEAD=%s) "
            "— deferring to it, not respawning (idempotent)",
            incumbent_version,
        )
    return False
