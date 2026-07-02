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
      "spool: 0"   — empty backlog
      "spool: 3"   — small backlog
      "spool: 12!" — backlog over threshold (alert)
    """
    from dbops.spool import read_pending

    depth = len(read_pending(Path(spool_dir)))
    marker = "!" if depth > _BACKLOG_THRESHOLD else ""
    return f"spool: {depth}{marker}"
