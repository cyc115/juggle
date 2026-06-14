"""juggle_watchdog_restart — Hot-restart and stale-code detection helpers.

Owns: pure decision logic for detecting source-code changes and triggering
a safe os.execv hot-restart of the watchdog daemon process.
Must not own: agent state classification, recovery/action-item logic, DB ops.

Note: juggle_watchdog.py imports and re-exports all names from this module so
that `scripts/juggle-agent-watchdog` (which imports from juggle_watchdog) and
the hot-restart mtime-watch path continue to work unchanged.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time as _time
from pathlib import Path

_log = logging.getLogger(__name__)

_HOT_RESTART_GRACE_SECS: float = 300.0  # files must be stable this long before re-exec


def _is_source_stale(recorded_mtime: float, source_path: Path) -> bool:
    """Return True if source_path has been modified since recorded_mtime."""
    try:
        return source_path.stat().st_mtime > recorded_mtime
    except OSError:
        return False


def should_exit_for_reload(stale: bool, supervised: bool) -> bool:
    """Pure decision: should the daemon sys.exit() to trigger a supervisor restart?

    Only True when source is stale AND we are running under a supervisor (e.g.
    launchd KeepAlive) that will restart us.  Without a supervisor, exiting is
    permanent — so unsupervised daemons must continue the loop instead.
    """
    return stale and supervised


def should_hot_restart(
    baseline_mtimes: dict[str, float],
    current_mtimes: dict[str, float],
    last_change_at: float | None,
    now: float,
    grace_secs: float = _HOT_RESTART_GRACE_SECS,
) -> tuple[bool, float | None]:
    """Pure decision function for hot-restart.

    Returns (ready_to_restart, new_last_change_at).

    Stability-window logic: a change must be stable (no further mtime shifts)
    for >= grace_secs before restart is authorised, preventing restarts on
    half-written files or edit/save flurries.
    """
    changed = current_mtimes != baseline_mtimes

    if not changed:
        # No change, or files reverted to original baseline — cancel pending restart.
        return False, None

    if last_change_at is None:
        # First detection this cycle — record timestamp; not ready yet.
        return False, now

    if now - last_change_at >= grace_secs:
        return True, last_change_at

    return False, last_change_at


def _collect_mtimes(src_dir: Path, entry_script: Path | None = None) -> dict[str, float]:
    """Stat all src/*.py files plus the optional entry script; return {str(path): mtime}."""
    paths: list[Path] = sorted(src_dir.glob("*.py"))
    if entry_script and entry_script.exists():
        paths.append(entry_script)
    result: dict[str, float] = {}
    for p in paths:
        try:
            result[str(p)] = p.stat().st_mtime
        except OSError:
            pass
    return result


def _maybe_hot_restart(
    baseline_mtimes: dict[str, float],
    state: dict,
    src_dir: Path,
    entry_script: Path | None = None,
) -> None:
    """Thin wrapper: stat files, call should_hot_restart, and re-exec if ready.

    ``state`` is a mutable dict with keys:
      - ``last_change_at``: float | None
      - ``prev_current_mtimes``: dict[str, float]

    The wrapper resets ``last_change_at`` when it detects the current mtimes
    have shifted since the previous poll (further edit after first detection).
    """
    import sys as _sys

    now = _time.time()
    current = _collect_mtimes(src_dir, entry_script)

    # If mtimes have changed further since the last poll, reset the timer so
    # the grace period restarts from this moment.
    prev = state.get("prev_current_mtimes", {})
    if prev and current != prev and current != baseline_mtimes:
        state["last_change_at"] = None

    state["prev_current_mtimes"] = current

    ready, new_lca = should_hot_restart(
        baseline_mtimes, current, state.get("last_change_at"), now
    )
    state["last_change_at"] = new_lca

    if not ready:
        return

    # Crash-guard: verify new code imports cleanly before re-exec'ing.
    check = subprocess.run(
        [_sys.executable, "-c", "import juggle_watchdog"],
        cwd=str(src_dir),
        capture_output=True,
    )
    if check.returncode != 0:
        _log.warning(
            "hot-restart deferred: new code fails to import: %s",
            check.stderr.decode(errors="replace").strip(),
        )
        return

    _log.info("hot-restart: source changed, re-exec'ing")
    os.execv(_sys.executable, [_sys.executable, *_sys.argv])
