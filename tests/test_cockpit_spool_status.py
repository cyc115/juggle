"""Tests for cockpit spool-depth helper + chrome/start wiring (Task 11)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_get_spool_status_line_empty(tmp_path):
    """Returns a zero-depth string when the spool dir is empty/missing."""
    from juggle_cockpit_spool_status import get_spool_status_line
    line = get_spool_status_line(tmp_path / "spool")
    assert line == "spool: 0"


def test_get_spool_status_line_counts_pending(tmp_path):
    """Reflects the number of pending (undrained) spool events."""
    from dbops.spool import write_event
    from juggle_cockpit_spool_status import get_spool_status_line

    spool_dir = tmp_path / "spool"
    write_event(spool_dir, "agent_complete", "agent-1", "thread-1", {})
    write_event(spool_dir, "agent_complete", "agent-2", "thread-2", {})

    line = get_spool_status_line(spool_dir)
    assert line == "spool: 2"


def test_get_spool_status_line_alert_over_threshold(tmp_path):
    """Marks the line when backlog exceeds the alert threshold."""
    from dbops.spool import write_event
    from juggle_cockpit_spool_status import get_spool_status_line, _BACKLOG_THRESHOLD

    spool_dir = tmp_path / "spool"
    for i in range(_BACKLOG_THRESHOLD + 1):
        write_event(spool_dir, "agent_complete", f"agent-{i}", f"thread-{i}", {})

    line = get_spool_status_line(spool_dir)
    assert "!" in line


def test_spool_status_line_fits_chrome(tmp_path):
    """get_spool_status_line returns a short string (<=20 chars) for chrome."""
    from dbops.spool import write_event
    from juggle_cockpit_spool_status import get_spool_status_line

    spool_dir = tmp_path / "spool"
    write_event(spool_dir, "agent_complete", "agent-1", "thread-1", {})
    line = get_spool_status_line(spool_dir)
    assert len(line) <= 20


def test_spool_status_widget_wired_in_cockpit_compose():
    """The cockpit compose() yields a #spool-status chrome widget."""
    src = Path(__file__).parent.parent / "src" / "juggle_cockpit.py"
    text = src.read_text()
    assert 'id="spool-status"' in text


def test_cmd_start_drains_spool(monkeypatch, tmp_path):
    """`juggle start` drains any backlog left in the spool immediately."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    import juggle_cmd_threads

    calls = []
    monkeypatch.setattr(
        juggle_cmd_threads, "drain_spool", lambda db: calls.append(db) or {}
    )
    monkeypatch.setattr(juggle_cmd_threads, "_maybe_start_talkback", lambda: None)
    monkeypatch.setattr(
        juggle_cmd_threads, "_start_watchdog_for_cmd_start", lambda db: None
    )

    class _FakeDB:
        db_path = str(tmp_path / "juggle.db")

        def init_db(self):
            pass

        def set_active(self, value):
            pass

        def set_orchestrator_session_id(self, sid):
            pass

        def get_all_threads(self):
            return [{"id": "t1", "state": "active", "last_active_at": ""}]

        def set_current_thread(self, uuid):
            pass

        def get_thread(self, uuid):
            return {"uuid": uuid, "user_label": "General"}

    monkeypatch.setattr(juggle_cmd_threads, "get_db", lambda: _FakeDB())
    monkeypatch.setattr(juggle_cmd_threads, "_DATA_DIR", tmp_path / "data")

    import juggle_context
    monkeypatch.setattr(juggle_context, "build_startup_output", lambda db: "")

    juggle_cmd_threads.cmd_start(None)

    assert len(calls) == 1
