import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from unittest.mock import Mock, MagicMock, patch


@pytest.fixture
def mock_db():
    db = Mock()
    db.init_db = Mock()
    db.set_active = Mock()
    db.get_all_threads = Mock(return_value=[])
    db.create_thread = Mock(return_value="thread-uuid-1")
    db.set_current_thread = Mock()
    db.get_thread = Mock(return_value={"id": "thread-uuid-1", "label": "General"})
    return db


@pytest.fixture
def mock_get_db(mock_db):
    with patch("juggle_cmd_threads.get_db", return_value=mock_db):
        yield mock_db


def test_cmd_start_creates_db_and_watchdog(mock_get_db, tmp_path):
    from juggle_cmd_threads import cmd_start

    with patch("juggle_cmd_threads._DATA_DIR", tmp_path):
        with patch("juggle_cmd_threads._start_watchdog_for_cmd_start"):
            with patch("juggle_cmd_threads._maybe_start_talkback"):
                with patch("juggle_cmd_threads._get_version", return_value="1.0"):
                    with patch("builtins.print") as mock_print:
                        cmd_start(None)

    mock_get_db.init_db.assert_called_once()
    mock_get_db.set_active.assert_called_once_with(True)
    assert mock_print.called


def test_cmd_start_uses_existing_thread(mock_get_db):
    from juggle_cmd_threads import cmd_start

    existing_thread = {
        "id": "existing-uuid",
        "label": "Existing",
        "status": "active",
        "last_active": "2026-05-18T10:00:00Z",
    }
    mock_get_db.get_all_threads.return_value = [existing_thread]
    mock_get_db.get_thread.return_value = existing_thread

    with patch("juggle_cmd_threads._DATA_DIR"):
        with patch("juggle_cmd_threads._start_watchdog_for_cmd_start"):
            with patch("juggle_cmd_threads._maybe_start_talkback"):
                with patch("juggle_cmd_threads._get_version", return_value="1.0"):
                    with patch(
                        "juggle_context.build_startup_output", return_value="startup"
                    ):
                        with patch("builtins.print"):
                            cmd_start(None)

    mock_get_db.set_current_thread.assert_called_with("existing-uuid")


def test_cmd_stop_sets_inactive(mock_get_db):
    from juggle_cmd_threads import cmd_stop

    mock_get_db.get_all_threads.return_value = []

    with patch("juggle_cmd_threads._stop_watchdog"):
        with patch("builtins.print"):
            cmd_stop(None)

    mock_get_db.set_active.assert_called_once_with(False)


def test_cmd_stop_prints_threads(mock_get_db):
    from juggle_cmd_threads import cmd_stop

    threads = [
        {"id": "t1", "label": "A", "topic": "topic1", "status": "active"},
        {"id": "t2", "user_label": "B", "topic": "topic2", "status": "done"},
    ]
    mock_get_db.get_all_threads.return_value = threads

    with patch("juggle_cmd_threads._stop_watchdog"):
        with patch("builtins.print") as mock_print:
            cmd_stop(None)

    assert mock_print.call_count >= 3


def test_cmd_create_thread_creates_and_sets_current(mock_get_db):
    from juggle_cmd_threads import cmd_create_thread
    import argparse

    args = argparse.Namespace(topic="New Topic")

    with patch("juggle_cmd_threads._generate_title_for_thread"):
        with patch("juggle_cmd_threads.threading.Thread"):
            with patch("builtins.print"):
                cmd_create_thread(args)

    mock_get_db.create_thread.assert_called_once_with("New Topic", session_id="")
    mock_get_db.set_current_thread.assert_called_with("thread-uuid-1")



def test_cmd_create_thread_prints_output(mock_get_db):
    from juggle_cmd_threads import cmd_create_thread
    import argparse

    args = argparse.Namespace(topic="Test")
    mock_get_db.get_thread.return_value = {"id": "uuid", "label": "Test Label"}

    with patch("juggle_cmd_threads._generate_title_for_thread"):
        with patch("juggle_cmd_threads.threading.Thread"):
            with patch("builtins.print") as mock_print:
                cmd_create_thread(args)

    mock_print.assert_called_once()
    assert "Created Topic" in str(mock_print.call_args)


# ---------------------------------------------------------------------------
# Change B — session-scoped watchdog pidfile + kill-then-restart
# ---------------------------------------------------------------------------


def test_watchdog_pid_file_session_scoped(tmp_path, monkeypatch):
    """When CLAUDE_CODE_SESSION_ID is set, pidfile is session-scoped."""
    import juggle_cmd_threads

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-abc123")
    with patch("juggle_settings.get_settings", return_value={"paths": {"config_dir": str(tmp_path)}}):
        result = juggle_cmd_threads._watchdog_pid_file()
    assert result.name == "watchdog-sess-abc123.pid"
    assert result.parent == tmp_path


def test_watchdog_pid_file_fallback_when_no_session(tmp_path, monkeypatch):
    """When CLAUDE_CODE_SESSION_ID is unset, falls back to watchdog.pid."""
    import juggle_cmd_threads

    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    with patch("juggle_settings.get_settings", return_value={"paths": {"config_dir": str(tmp_path)}}):
        result = juggle_cmd_threads._watchdog_pid_file()
    assert result.name == "watchdog.pid"
    assert result.parent == tmp_path


def test_start_watchdog_kills_prior_pid_same_session(tmp_path, monkeypatch):
    """Second _start_watchdog() in same session SIGTERMs the old PID then starts fresh."""
    import juggle_cmd_threads

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-xyz")
    old_pid = 99991
    pid_file = tmp_path / "watchdog-sess-xyz.pid"
    pid_file.write_text(str(old_pid))
    script_path = tmp_path / "juggle-agent-watchdog"
    script_path.touch()

    mock_proc = MagicMock()
    mock_proc.pid = 99992
    killed = []

    def fake_kill(pid, sig):
        if sig == signal.SIGTERM:
            killed.append(pid)
        elif sig == 0:
            # old pid alive until SIGTERMed, then dead
            if pid == old_pid and old_pid not in killed:
                return
            raise OSError("not found")

    with patch("juggle_settings.get_settings", return_value={"paths": {"config_dir": str(tmp_path)}}):
        with patch.object(juggle_cmd_threads, "_watchdog_script", return_value=script_path):
            with patch("subprocess.run", return_value=MagicMock(returncode=0)):
                with patch("subprocess.Popen", return_value=mock_proc):
                    with patch("os.kill", side_effect=fake_kill):
                        with patch("time.sleep"):
                            juggle_cmd_threads._start_watchdog()

    assert old_pid in killed, "Expected SIGTERM sent to old PID"
    # New PID must be written to this session's pidfile
    assert pid_file.read_text().strip() == str(mock_proc.pid)


def test_start_watchdog_does_not_touch_other_session_pidfile(tmp_path, monkeypatch):
    """_start_watchdog() must never read or kill another session's pidfile."""
    import juggle_cmd_threads

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-mine")
    other_pid = 88881
    other_pid_file = tmp_path / "watchdog-sess-other.pid"
    other_pid_file.write_text(str(other_pid))
    script_path = tmp_path / "juggle-agent-watchdog"
    script_path.touch()

    mock_proc = MagicMock()
    mock_proc.pid = 88882
    killed = []

    def fake_kill(pid, sig):
        killed.append((pid, sig))
        if sig == 0 and pid != other_pid:
            raise OSError("not found")

    with patch("juggle_settings.get_settings", return_value={"paths": {"config_dir": str(tmp_path)}}):
        with patch.object(juggle_cmd_threads, "_watchdog_script", return_value=script_path):
            with patch("subprocess.run", return_value=MagicMock(returncode=0)):
                with patch("subprocess.Popen", return_value=mock_proc):
                    with patch("os.kill", side_effect=fake_kill):
                        with patch("time.sleep"):
                            juggle_cmd_threads._start_watchdog()

    for pid, _ in killed:
        assert pid != other_pid, "Must not touch another session's PID"

    for pid, _ in killed:
        assert pid != other_pid, "Must not touch another session's PID"


# ── Fix 4: watchdog pkill sweep ─────────────────────────────────────────────

def test_start_watchdog_pkill_called(monkeypatch, tmp_path):
    """Verify _start_watchdog calls pkill for stale watchdogs."""
    import subprocess as _sp
    import os as _os
    import sys
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent / "src"))

    pkill_calls = []
    def fake_run(cmd, **kwargs):
        if isinstance(cmd, list) and any("pkill" in str(c) for c in cmd):
            pkill_calls.append(cmd)
        return _sp.CompletedProcess(cmd, 0)

    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setattr(_os, "kill", lambda *a: None)

    # Process-scope isolation (T-watchdog-test-proc-scope, 2026-06-16): NEVER let
    # this test launch a REAL watchdog. The unmocked Popen used to leak a live
    # worktree daemon that then killed the host canonical watchdog during the
    # harness-gate full-suite run. Mock Popen so no host process is ever spawned.
    popen_calls = []

    class _FakeProc:
        pid = 999999

    def fake_popen(cmd, **kwargs):
        popen_calls.append(cmd)
        return _FakeProc()

    monkeypatch.setattr(_sp, "Popen", fake_popen)

    # Mock pidfile path
    from juggle_cmd_threads import _start_watchdog, _watchdog_pid_file
    monkeypatch.setattr("juggle_cmd_threads._watchdog_pid_file", lambda: tmp_path / "watchdog.pid")

    _start_watchdog()
    assert len(pkill_calls) >= 1, f"Expected at least 1 pkill call, got {len(pkill_calls)}"
    # The launch path must have gone through the MOCK — no real watchdog spawned.
    assert popen_calls, "expected the watchdog launch to be invoked (via mocked Popen)"


def test_start_watchdog_idempotent(monkeypatch, tmp_path):
    """Call _start_watchdog twice — should not raise."""
    import subprocess as _sp
    import os as _os
    import sys
    sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent / "src"))

    def fake_run(cmd, **kwargs):
        return _sp.CompletedProcess(cmd, 0)

    class _FakeProc:
        pid = 999998

    monkeypatch.setattr(_sp, "run", fake_run)
    monkeypatch.setattr(_sp, "Popen", lambda *a, **k: _FakeProc())  # no real launch
    monkeypatch.setattr(_os, "kill", lambda *a: None)

    from juggle_cmd_threads import _start_watchdog
    monkeypatch.setattr("juggle_cmd_threads._watchdog_pid_file", lambda: tmp_path / "watchdog.pid")
    monkeypatch.setattr("juggle_cmd_threads._watchdog_script", lambda: tmp_path / "fake_script.py")
    # Ensure script exists
    (tmp_path / "fake_script.py").write_text("import time; time.sleep(0.1)")

    _start_watchdog()
    _start_watchdog()  # should not raise
