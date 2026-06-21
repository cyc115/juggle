"""Watchdog spawn-lifecycle gates: respawn debounce + freeze sentinel.

Extracted from juggle_watchdog_singleton (2026-06-20 leak hardening) to keep that
module under its LOC budget. Pure file-sidecar helpers, no process/lock logic —
both gates are keyed off small files next to the DB:

  * ``.<db>.watchdog.spawned`` — last ensure-spawn monotonic stamp (debounce).
    Stops the 15s cockpit respawn storm: a slow-booting daemon hasn't taken the
    lock yet, so a naive liveness check would respawn AGAIN; the stamp suppresses
    that within ``min_respawn_interval``.
  * ``.<db>.watchdog.frozen`` — freeze sentinel. While present, ensure_watchdog
    is a hard no-op so ``stop-watchdog --freeze`` actually holds (the cockpit's
    15s ensure can no longer defeat it). Lifted only by an explicit start/unfreeze.
"""
from __future__ import annotations

from pathlib import Path

# ---------------------------------------------------------------------------
# Freeze sentinel
# ---------------------------------------------------------------------------


def freeze_sentinel_path(db_path) -> Path:
    """Sidecar marking this DB's watchdog as frozen (do-not-(re)spawn)."""
    p = Path(db_path)
    return p.parent / f".{p.name}.watchdog.frozen"


def is_watchdog_frozen(db_path) -> bool:
    """True iff the freeze sentinel is present for this DB."""
    try:
        return freeze_sentinel_path(db_path).exists()
    except OSError:
        return False


def freeze_watchdog(db_path) -> None:
    """Set the freeze sentinel so ensure_watchdog stops (re)spawning.

    Defeats the 2026-06-20 respawn churn that nullified stop-watchdog: while the
    sentinel exists, the cockpit's 15s ensure is a hard no-op. Cleared only by
    an explicit start / unfreeze.
    """
    try:
        stamp = freeze_sentinel_path(db_path)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text("frozen")
    except OSError:
        pass


def unfreeze_watchdog(db_path) -> None:
    """Clear the freeze sentinel (an explicit start/unfreeze)."""
    try:
        freeze_sentinel_path(db_path).unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Respawn debounce (spawn-stamp sidecar)
# ---------------------------------------------------------------------------


def spawn_stamp_path(db_path) -> Path:
    """Sidecar that records the last ensure-spawn time for this DB's watchdog."""
    p = Path(db_path)
    return p.parent / f".{p.name}.watchdog.spawned"


def read_last_spawn(db_path) -> float | None:
    try:
        return float(spawn_stamp_path(db_path).read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def record_spawn(db_path, when: float) -> None:
    try:
        stamp = spawn_stamp_path(db_path)
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(when))
    except OSError:
        pass


def default_min_respawn_interval() -> float:
    """Debounce window from ``watchdog.min_respawn_interval_secs`` (default 60)."""
    try:
        from juggle_settings import get_settings
        return float(get_settings().get("watchdog", {}).get("min_respawn_interval_secs", 60))
    except Exception:
        return 60.0


def should_suppress_spawn(db_path, *, now: float, min_respawn_interval: float, force: bool) -> bool:
    """True iff ensure_watchdog must NOT spawn right now (freeze or debounce).

    Freeze wins even under ``force`` (an explicit start/unfreeze is the only
    lifter). ``force`` otherwise bypasses the debounce so a W/R hotkey is never
    throttled. ``now`` is the caller's ``time.monotonic()`` (injected for tests).
    """
    if is_watchdog_frozen(db_path):
        return True
    if force:
        return False
    last = read_last_spawn(db_path)
    return last is not None and (now - last) < min_respawn_interval
