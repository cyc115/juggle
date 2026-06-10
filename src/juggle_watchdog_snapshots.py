"""juggle_watchdog_snapshots — pane snapshot file helpers for the watchdog.

Owns: reading/writing the per-agent pane snapshot used for stall detection
and the pruned recovery snapshots written before a recovery attempt.
Must not own: pane-state classification or the recovery flow (juggle_watchdog).

Extracted mechanically from juggle_watchdog.py (2026-06-10, LOC gate);
juggle_watchdog re-exports these names so existing imports keep working.
"""

from __future__ import annotations

import time as _time
from pathlib import Path


def read_snapshot(agent_id: str, snapshot_dir: Path) -> str | None:
    path = snapshot_dir / f"{agent_id}.txt"
    return path.read_text() if path.exists() else None


def write_snapshot(agent_id: str, content: str, snapshot_dir: Path) -> None:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / f"{agent_id}.txt").write_text(content)


def write_recovery_snapshot(agent_id: str, content: str, recovery_dir: Path) -> Path:
    """Write a recovery snapshot; prune to last 100 per agent (DA-4 fix)."""
    recovery_dir.mkdir(parents=True, exist_ok=True)
    ts = _time.time_ns()  # nanosecond precision avoids collisions in rapid succession
    path = recovery_dir / f"{agent_id}-{ts}.txt"
    path.write_text(content)
    agent_snaps = sorted(
        recovery_dir.glob(f"{agent_id}-*.txt"), key=lambda p: p.stat().st_mtime
    )
    for old in agent_snaps[:-100]:
        try:
            old.unlink()
        except FileNotFoundError:
            pass
    return path
