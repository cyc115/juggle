"""No-survive pin: ensure/restart must report failure when the daemon never comes up.

Incident: 2026-06-17 watchdog-start-fix.
Symptom: ensure/restart reported success even when the daemon never came up —
the spawn function returned and the caller assumed liveness without verifying
that the daemon actually acquired the singleton lock. Cockpit then claimed the
watchdog had started/restarted while the status dot stayed red.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from juggle_watchdog_singleton import ensure_watchdog, restart_watchdog


def _noop_spawn(db_path, *, repo_path=None):
    """A spawn that does nothing — never acquires the singleton lock."""
    return None


def test_ensure_reports_false_when_daemon_never_survives(tmp_path):
    db_path = tmp_path / "juggle.db"
    assert ensure_watchdog(db_path, spawn=_noop_spawn, survive_timeout=0.3) is False


def test_restart_reports_false_when_daemon_never_survives(tmp_path):
    db_path = tmp_path / "juggle.db"
    assert restart_watchdog(db_path, spawn=_noop_spawn, survive_timeout=0.3) is False
