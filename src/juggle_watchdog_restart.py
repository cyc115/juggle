"""juggle_watchdog_restart — stale-code detection for the watchdog daemon.

Owns: pure decision logic for detecting that the plugin's code has advanced past
the version the daemon booted on. On drift the daemon exits cleanly and the
cockpit's periodic ``ensure_watchdog`` respawns a fresh process on the new code
(the "always run latest merged code" contract — Defect B, 2026-07-01).

Must not own: agent state classification, recovery/action-item logic, DB ops.

Note: juggle_watchdog.py imports and re-exports names from this module so that
``scripts/juggle-agent-watchdog`` (which imports from juggle_watchdog) keeps
working unchanged.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

_log = logging.getLogger(__name__)


def current_code_version(repo_path: Path) -> str | None:
    """Fingerprint the plugin's current code as the git HEAD sha of ``repo_path``.

    The daemon runs from the canonical main worktree, which fast-forwards on every
    integrate, so ``git rev-parse HEAD`` is the precise "main advanced past what I
    loaded" signal covering the WHOLE tracked source tree (not just one file).

    Returns None when the sha can't be determined (git missing / not a repo /
    timeout). Callers treat None as "unknown" and never exit on it — a respawn we
    can't verify is worse than continuing on current code.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def should_exit_for_stale_code(
    boot_version: str | None, current_version: str | None
) -> bool:
    """Pure decision: should the daemon exit because its code is stale?

    True only when BOTH fingerprints are known AND differ. This applies
    REGARDLESS of supervisor: an unsupervised daemon relies on the cockpit's
    periodic ``ensure_watchdog`` to respawn a fresh process on the new code — the
    lock releases on exit and the next ensure re-launches from canonical main.

    When either fingerprint is unknown (None) we return False (fail-safe: keep
    ticking rather than exit into an unverifiable respawn).
    """
    if boot_version is None or current_version is None:
        return False
    return boot_version != current_version


def should_replace_incumbent(
    *, same_code: bool | None, heartbeat_age: float | None, stale_after: float
) -> bool:
    """Idempotent respawn gate (2026-07-01 churn fix).

    A daemon booting finds a PID already recorded in the singleton pidfile. This
    decides whether to kill-and-replace that incumbent or defer to it. Kill ONLY
    when the incumbent runs OLD code (``same_code is False`` — different boot
    HEAD) or is HUNG (``heartbeat_age`` older than ``stale_after``). A fresh,
    same-code, live incumbent is left untouched: the newcomer then fails to take
    the singleton flock and exits as the redundant loser, so two near-simultaneous
    respawns never mutually SIGTERM each other (the 4-restarts-in-64s churn).

    ``same_code is None`` means the incumbent's boot HEAD couldn't be read — treat
    it as a same-code peer (fail-safe toward NOT churning); it is still replaced if
    its heartbeat is stale (a genuinely hung/abandoned instance).
    """
    if same_code is False:
        return True
    if heartbeat_age is not None and heartbeat_age > stale_after:
        return True
    return False
