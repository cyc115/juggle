"""juggle_cockpit_spool_status — compact spool-depth string for the cockpit.

Used by the cockpit chrome to show how many spooled agent-context events are
still waiting to be drained by the watchdog tick.
"""
from __future__ import annotations

from pathlib import Path

_BACKLOG_THRESHOLD = 10  # alert if this many events are pending


def get_spool_status_line(spool_dir: Path) -> str:
    """Return a short status string (<=20 chars) for the cockpit chrome.

    Examples:
      "spool: 0"             — empty backlog, no dead-letters
      "spool: 3"             — small pending backlog
      "spool: 12!"           — pending backlog over threshold (alert)
      "spool: 0 (dead: 12!)" — dead-lettered events awaiting recovery (RCA D1)
    """
    from dbops.spool import read_pending

    spool_dir = Path(spool_dir)
    depth = len(read_pending(spool_dir))
    marker = "!" if depth > _BACKLOG_THRESHOLD else ""
    line = f"spool: {depth}{marker}"
    dead_dir = spool_dir / "dead"
    dead = len(list(dead_dir.glob("*.json"))) if dead_dir.exists() else 0
    if dead > 0:
        line += f" (dead: {dead}!)"
    return line
