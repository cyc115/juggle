"""Phase 2 pin — ONE watchdog launch path (the flock), no second pidfile launcher.

Incident: 2026-06-20 watchdog-daemon-leak RCA §P2.
Symptom: two uncoordinated prod launchers — juggle_cmd_threads._start_watchdog
(pidfile path, `_watchdog_pid_file`) vs start_watchdog_detached/ensure_watchdog
(flock path). Each had its own notion of "singleton", so daemons repeatedly
"killed the previous instance". The flock must be the ONE coordination primitive.

This pin asserts the dead pidfile launcher is gone and lifecycle routes through
the flock-based singleton helpers in juggle_watchdog_singleton.
"""

import sys
from pathlib import Path
from unittest.mock import Mock, patch

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_no_pidfile_launcher_symbols_remain():
    """The pidfile-based second launcher (and its helpers) must not exist —
    the flock is the only singleton primitive."""
    import juggle_cmd_threads as cmd

    for sym in ("_start_watchdog", "_watchdog_pid_file", "_stop_watchdog"):
        assert not hasattr(cmd, sym), (
            f"juggle_cmd_threads.{sym} is a dead pidfile-launcher symbol — the "
            f"flock-based singleton (juggle_watchdog_singleton) is the only "
            f"coordination primitive; remove it."
        )


def test_cmd_stop_routes_through_flock_stop_watchdog(tmp_path, monkeypatch):
    """cmd_stop must stop the watchdog via the flock helper stop_watchdog(db_path),
    not a pidfile read — so the single coordination primitive owns lifecycle."""
    import juggle_cmd_threads as cmd
    import juggle_watchdog_singleton as ws

    db_path = tmp_path / "x.db"
    db_path.write_text("")
    db = Mock()
    db.db_path = db_path
    db.set_active = Mock()
    db.set_orchestrator_session_id = Mock()
    db.get_all_threads = Mock(return_value=[])

    calls = []
    monkeypatch.setattr(ws, "stop_watchdog", lambda p, **k: calls.append(str(p)))

    with patch("juggle_cmd_threads.get_db", return_value=db), \
         patch("builtins.print"):
        cmd.cmd_stop(None)

    assert calls == [str(db_path)], (
        "cmd_stop must call the flock stop_watchdog(db_path) — one primitive."
    )
