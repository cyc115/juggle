"""Juggle Cockpit Sched — scheduled-task discovery for the cockpit pool pane.

Owns: launchd/systemd scheduled-task discovery (``fetch_scheduled_tasks``)
and its plist/launchctl parsing helpers. Zero Rich imports.
Must not own: DB snapshotting (juggle_cockpit_model) or rendering.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduledTask:
    label: str  # short name, e.g. "otter-daily-summary"
    schedule: str  # "every 5m", "daily 19:00", "on-demand"
    status: str  # "running" | "ok" | "failed" | "unknown"
    pid: int | None


def _parse_schedule(data: dict) -> str:
    if "StartInterval" in data:
        secs = int(data["StartInterval"])
        if secs < 60:
            return f"every {secs}s"
        if secs < 3600:
            return f"every {secs // 60}m"
        return f"every {secs // 3600}h"
    if "StartCalendarInterval" in data:
        entry = data["StartCalendarInterval"]
        if isinstance(entry, list):
            entry = entry[0]
        h = entry.get("Hour")
        m = entry.get("Minute", 0)
        if h is not None:
            return f"daily {h:02d}:{m:02d}"
    if "WatchPaths" in data:
        return "on-change"
    return "on-demand"


def _launchctl_status(label: str) -> tuple[int | None, int | None]:
    """Return (pid, last_exit_status) for a launchd label."""
    try:
        r = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True,
            text=True,
            timeout=2,
        )
        pid: int | None = None
        exit_status: int | None = None
        for line in r.stdout.splitlines():
            line = line.strip()
            if '"PID"' in line:
                try:
                    pid = int(line.split("=")[1].strip().rstrip(";"))
                except (ValueError, IndexError):
                    pass
            if '"LastExitStatus"' in line:
                try:
                    exit_status = int(line.split("=")[1].strip().rstrip(";"))
                except (ValueError, IndexError):
                    pass
        return pid, exit_status
    except Exception:
        return None, None


def fetch_scheduled_tasks() -> list[ScheduledTask]:
    """Discover scheduled tasks via the platform-appropriate backend."""
    try:
        from juggle_scheduler import get_backend

        backend = get_backend()
        infos = backend.list_tasks()
    except Exception:
        return []
    return [
        ScheduledTask(label=i.label, schedule=i.schedule, status=i.status, pid=i.pid)
        for i in infos
    ]
