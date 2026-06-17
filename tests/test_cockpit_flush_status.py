"""Tests for cockpit flush-status helper (Task 9)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_get_flush_status_line_no_flush(tmp_path):
    """Returns a safe string when no flush has occurred."""
    from juggle_cockpit_flush_status import get_flush_status_line
    durable = tmp_path / "juggle.db"
    line = get_flush_status_line(durable)
    assert isinstance(line, str)
    assert "flush" in line.lower() or "never" in line.lower() or line == ""


def test_get_flush_status_line_after_flush(tmp_path):
    """Returns a string with age info after a flush."""
    from juggle_cmd_db_flush import flush_once
    from juggle_cockpit_flush_status import get_flush_status_line
    from juggle_db import JuggleDB

    live = tmp_path / "live.db"
    durable = tmp_path / "durable.db"
    JuggleDB(db_path=str(live)).init_db()
    flush_once(live, durable)

    line = get_flush_status_line(durable)
    assert isinstance(line, str)
    assert len(line) > 0


def test_get_flush_status_line_stale_alert(tmp_path):
    """Returns an alert marker when flush is stale (old timestamp)."""
    from juggle_cockpit_flush_status import get_flush_status_line, _STALE_THRESHOLD_S
    from juggle_db import JuggleDB
    from juggle_cmd_db_flush import _ts_path
    from datetime import datetime, timezone, timedelta

    durable = tmp_path / "durable.db"
    # Write a very old timestamp
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=_STALE_THRESHOLD_S + 60)).isoformat()
    _ts_path(durable).write_text(old_ts)

    line = get_flush_status_line(durable)
    assert "!" in line or "stale" in line.lower() or "⚠" in line, (
        f"Expected stale alert in {line!r}"
    )


def test_flush_status_line_fits_footer(tmp_path):
    """get_flush_status_line returns a short string (≤20 chars for footer)."""
    from juggle_cockpit_flush_status import get_flush_status_line
    from juggle_cmd_db_flush import flush_once
    from juggle_db import JuggleDB

    live = tmp_path / "live.db"
    durable = tmp_path / "durable.db"
    JuggleDB(db_path=str(live)).init_db()
    flush_once(live, durable)

    line = get_flush_status_line(durable)
    assert len(line) <= 20, f"Status line too long for footer: {line!r}"
