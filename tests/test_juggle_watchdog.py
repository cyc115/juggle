"""Regression tests — stale-code detection and cold-start cascade dedup."""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# _is_source_stale — pure function
# ---------------------------------------------------------------------------


def test_is_source_stale_detects_newer_file(tmp_path):
    from juggle_watchdog import _is_source_stale

    f = tmp_path / "watchdog.py"
    f.write_text("v1")
    old_mtime = f.stat().st_mtime
    time.sleep(0.02)
    f.write_text("v2")
    assert _is_source_stale(old_mtime, f) is True


def test_is_source_stale_returns_false_when_unchanged(tmp_path):
    from juggle_watchdog import _is_source_stale

    f = tmp_path / "watchdog.py"
    f.write_text("v1")
    mtime = f.stat().st_mtime
    assert _is_source_stale(mtime, f) is False


def test_is_source_stale_returns_false_for_missing_file(tmp_path):
    from juggle_watchdog import _is_source_stale

    assert _is_source_stale(0.0, tmp_path / "nonexistent.py") is False


# ---------------------------------------------------------------------------
# _record_cold_start_failure — unit tests
# ---------------------------------------------------------------------------


def test_cascade_not_triggered_below_threshold():
    from juggle_watchdog import (
        _record_cold_start_failure,
        _cold_start_failures,
        _cascade_filed,
    )

    _cold_start_failures.clear()
    _cascade_filed.clear()
    thread_id = "t-below-threshold"
    now = time.time()
    assert _record_cold_start_failure(thread_id, _now=now) == "normal"
    assert _record_cold_start_failure(thread_id, _now=now + 1) == "normal"


def test_cascade_fire_at_threshold():
    from juggle_watchdog import (
        _record_cold_start_failure,
        _cold_start_failures,
        _cascade_filed,
        _CASCADE_THRESHOLD,
    )

    _cold_start_failures.clear()
    _cascade_filed.clear()
    thread_id = "t-fire"
    now = time.time()
    for i in range(_CASCADE_THRESHOLD - 1):
        result = _record_cold_start_failure(thread_id, _now=now + i)
        assert result == "normal"
    # Exactly at threshold
    assert (
        _record_cold_start_failure(thread_id, _now=now + _CASCADE_THRESHOLD)
        == "cascade_fire"
    )


def test_cascade_suppress_after_fire():
    from juggle_watchdog import (
        _record_cold_start_failure,
        _cold_start_failures,
        _cascade_filed,
        _CASCADE_THRESHOLD,
    )

    _cold_start_failures.clear()
    _cascade_filed.clear()
    thread_id = "t-suppress"
    now = time.time()
    for i in range(_CASCADE_THRESHOLD):
        _record_cold_start_failure(thread_id, _now=now + i)
    # Additional failures should be suppressed
    assert _record_cold_start_failure(thread_id, _now=now + 60) == "cascade_suppress"
    assert _record_cold_start_failure(thread_id, _now=now + 61) == "cascade_suppress"


def test_cascade_skip_when_no_thread_id():
    from juggle_watchdog import (
        _record_cold_start_failure,
        _cold_start_failures,
        _cascade_filed,
    )

    _cold_start_failures.clear()
    _cascade_filed.clear()
    assert _record_cold_start_failure(None) == "skip"


def test_clear_cold_start_failures_resets_state():
    from juggle_watchdog import (
        _record_cold_start_failure,
        _clear_cold_start_failures,
        _cold_start_failures,
        _cascade_filed,
        _CASCADE_THRESHOLD,
    )

    _cold_start_failures.clear()
    _cascade_filed.clear()
    thread_id = "t-clear"
    now = time.time()
    for i in range(_CASCADE_THRESHOLD):
        _record_cold_start_failure(thread_id, _now=now + i)
    assert thread_id in _cascade_filed

    _clear_cold_start_failures(thread_id)
    assert thread_id not in _cold_start_failures
    assert thread_id not in _cascade_filed


def test_cascade_resets_after_window_clears():
    """After the window expires, cascade state resets and normal items fire again."""
    from juggle_watchdog import (
        _record_cold_start_failure,
        _cold_start_failures,
        _cascade_filed,
        _CASCADE_THRESHOLD,
        _CASCADE_WINDOW_SECS,
    )

    _cold_start_failures.clear()
    _cascade_filed.clear()
    thread_id = "t-window"
    now = time.time()
    for i in range(_CASCADE_THRESHOLD):
        _record_cold_start_failure(thread_id, _now=now + i)
    assert thread_id in _cascade_filed

    # Simulate a failure well after the window — all prior failures pruned
    far_future = now + _CASCADE_WINDOW_SECS + _CASCADE_THRESHOLD + 10
    result = _record_cold_start_failure(thread_id, _now=far_future)
    assert result == "normal"
    assert thread_id not in _cascade_filed


# ---------------------------------------------------------------------------
# execute_recovery integration: cascade dedup files one consolidated item
# ---------------------------------------------------------------------------


def test_cascade_dedup_files_one_consolidated_item(tmp_path):
    """4 cold-start failures: first 2 normal, 3rd fires cascade, 4th suppressed."""
    from juggle_db import JuggleDB
    from juggle_watchdog import (
        _cold_start_failures,
        _cascade_filed,
        _CASCADE_THRESHOLD,
        execute_recovery,
    )

    _cold_start_failures.clear()
    _cascade_filed.clear()

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("cascade test", session_id="")

    mgr = MagicMock()
    mgr.verify_pane.return_value = False  # dead pane — triggers recovery
    mgr.spawn_agent.return_value = {
        "id": "new-agent-id",
        "pane_id": "%99",
        "status": "busy",
    }
    mgr.send_task.side_effect = RuntimeError("tmux spawn truncated")

    recovery_dir = tmp_path / "recovery"

    for i in range(_CASCADE_THRESHOLD + 1):
        agent_id = db.create_agent(role="coder", pane_id="%5")
        db.update_agent(
            agent_id,
            status="busy",
            assigned_thread=thread_id,
            last_task="do work",
            watchdog_retried=0,
        )
        db.update_thread(thread_id, status="active")
        agent = db.get_agent(agent_id)
        execute_recovery(
            db, mgr, agent, "pane content", recovery_dir=recovery_dir, session_id="sid"
        )

    items = db.get_open_action_items()
    cascade_items = [it for it in items if "WATCHDOG-CASCADE-DETECTED" in it["message"]]
    cold_start_items = [it for it in items if "COLD-START-FAILED" in it["message"]]

    # Exactly one cascade item
    assert len(cascade_items) == 1
    # Only pre-threshold failures filed individually (threshold - 1)
    assert len(cold_start_items) == _CASCADE_THRESHOLD - 1


def test_cascade_cleared_on_successful_recovery(tmp_path):
    """After cascade, a successful recovery clears state and dismisses cold-start items."""
    from juggle_db import JuggleDB
    from juggle_watchdog import (
        _cold_start_failures,
        _cascade_filed,
        _CASCADE_THRESHOLD,
        execute_recovery,
    )

    _cold_start_failures.clear()
    _cascade_filed.clear()

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("cascade clear test", session_id="")

    mgr = MagicMock()
    mgr.verify_pane.return_value = False
    mgr.spawn_agent.return_value = {
        "id": "new-agent-id",
        "pane_id": "%99",
        "status": "busy",
    }
    mgr.send_task.side_effect = RuntimeError("truncated")

    recovery_dir = tmp_path / "recovery"

    # Build up cascade
    for i in range(_CASCADE_THRESHOLD):
        agent_id = db.create_agent(role="coder", pane_id="%5")
        db.update_agent(
            agent_id,
            status="busy",
            assigned_thread=thread_id,
            last_task="do work",
            watchdog_retried=0,
        )
        db.update_thread(thread_id, status="active")
        execute_recovery(
            db,
            mgr,
            db.get_agent(agent_id),
            "content",
            recovery_dir=recovery_dir,
            session_id="sid",
        )

    assert thread_id in _cascade_filed

    # Successful recovery clears cascade state
    mgr.send_task.side_effect = None  # no longer fails
    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="do work",
        watchdog_retried=0,
    )
    db.update_thread(thread_id, status="active")
    execute_recovery(
        db,
        mgr,
        db.get_agent(agent_id),
        "content",
        recovery_dir=recovery_dir,
        session_id="sid",
    )

    assert thread_id not in _cascade_filed
    # Cold-start items dismissed
    open_items = db.get_open_action_items()
    remaining_cold_start = [
        it
        for it in open_items
        if it.get("thread_id") == thread_id
        and "COLD-START-FAILED" in it.get("message", "")
    ]
    assert len(remaining_cold_start) == 0
