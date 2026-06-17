"""juggle_cockpit_flush_status — compact flush-status string for the cockpit.

Used by the cockpit footer/status bar to show how stale the last DB flush is.
Only shown when db.mode=tmpfs; silent in direct mode.
"""
from __future__ import annotations

from pathlib import Path

_STALE_THRESHOLD_S = 60  # alert if last flush was >60 s ago


def get_flush_status_line(durable_path: Path) -> str:
    """Return a short status string (≤20 chars) for the cockpit footer.

    Examples:
      "flush 3s ago"   — recent flush
      "flush 45s ago!" — stale flush (alert)
      "flush: never"   — no flush timestamp found
      ""               — direct mode (no flush file)
    """
    from juggle_cmd_db_flush import flush_status, _ts_path
    durable_path = Path(durable_path)

    if not _ts_path(durable_path).exists():
        return "flush: never"

    status = flush_status(durable_path)
    if status["last_flush_at"] is None:
        return "flush: never"

    age = status["age_s"]
    if age is None:
        return "flush: ?"

    age_int = int(age)
    stale = age_int > _STALE_THRESHOLD_S
    marker = "!" if stale else ""
    if age_int < 60:
        return f"flush {age_int}s ago{marker}"
    mins = age_int // 60
    return f"flush {mins}m ago{marker}"
