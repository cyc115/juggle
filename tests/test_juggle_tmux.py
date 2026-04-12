"""Tests for JuggleTmuxManager — subprocess calls are mocked."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def mgr():
    from juggle_tmux import JuggleTmuxManager
    return JuggleTmuxManager(session_name="juggle-test")


def _ok(stdout=""):
    """Return a mock CompletedProcess with returncode=0."""
    m = MagicMock()
    m.returncode = 0
    m.stdout = stdout
    return m


def _fail():
    m = MagicMock()
    m.returncode = 1
    m.stdout = ""
    return m


def test_ensure_session_creates_when_missing(mgr):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [_fail(), _ok()]  # has-session fail, new-session ok
        mgr.ensure_session()
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert any("new-session" in c for c in calls)


def test_ensure_session_skips_if_exists(mgr):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _ok()
        mgr.ensure_session()
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert all("new-session" not in c for c in calls)


def test_ensure_session_raises_if_no_tmux(mgr):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError("tmux")
        with pytest.raises(RuntimeError, match="tmux not found"):
            mgr.ensure_session()


def test_spawn_pane_returns_pane_id(mgr):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _ok(stdout="%5\n")
        pane_id = mgr.spawn_pane()
    assert pane_id == "%5"


def test_verify_pane_true_when_present(mgr):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _ok(stdout="%1\n%3\n%5\n")
        assert mgr.verify_pane("%3") is True


def test_verify_pane_false_when_absent(mgr):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _ok(stdout="%1\n%2\n")
        assert mgr.verify_pane("%9") is False


def test_kill_pane_calls_tmux(mgr):
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _ok()
        mgr.kill_pane("%3")
    args = mock_run.call_args.args[0]
    assert "kill-pane" in args
    assert "-t" in args
    assert "%3" in args


def test_send_task_loads_and_pastes(mgr):
    with patch("subprocess.run") as mock_run, \
         patch("time.sleep"), \
         patch("juggle_tmux.uuid"):
        mock_run.return_value = _ok()
        mgr.send_task("%3", "do something", is_new=False)
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert any("load-buffer" in c for c in calls)
    assert any("paste-buffer" in c for c in calls)
    assert any("send-keys" in c for c in calls)


def test_send_task_spawns_bg_subprocess_when_new(mgr):
    # is_new=True: spawns background subprocess with sleep+paste+retry
    with patch("subprocess.Popen") as mock_popen:
        mgr.send_task("%3", "do something", is_new=True)
    mock_popen.assert_called_once()
    args = mock_popen.call_args
    script = args[0][0][2]  # ["bash", "-c", script]
    assert "sleep 5" in script
    assert "paste-buffer" in script
    assert "sleep 10" in script


def test_send_task_existing_agent_has_delay_and_retry(mgr):
    # is_new=False: paste + 1s delay + C-m + background retry
    with patch("subprocess.run") as mock_run, \
         patch("time.sleep") as mock_sleep, \
         patch("subprocess.Popen") as mock_popen:
        mock_run.return_value = _ok()
        mgr.send_task("%3", "do something", is_new=False)
    # 1s delay between paste and Enter
    mock_sleep.assert_called_once_with(1)
    # Background retry Popen spawned
    mock_popen.assert_called_once()


def test_spawn_agent_creates_db_record(mgr, tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()

    with patch.object(mgr, "ensure_session"), \
         patch.object(mgr, "spawn_pane", return_value="%7"), \
         patch.object(mgr, "start_claude_in_pane"):
        agent = mgr.spawn_agent(db, role="coder")

    assert agent["role"] == "coder"
    assert agent["pane_id"] == "%7"
    assert agent["status"] == "idle"


def test_decommission_agent_kills_pane_and_deletes(mgr, tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    agent_id = db.create_agent(role="coder", pane_id="%3")

    with patch.object(mgr, "kill_pane") as mock_kill:
        mgr.decommission_agent(db, agent_id)

    mock_kill.assert_called_once_with("%3")
    assert db.get_agent(agent_id) is None


def test_get_pane_last_used_returns_int(mgr):
    mock = MagicMock(return_value=MagicMock(stdout="1712800000\n"))
    with patch.object(mgr, "_run_tmux", mock):
        result = mgr.get_pane_last_used("%5")
    mock.assert_called_once_with("display", "-pt", "%5", "#{pane_last_used}")
    assert result == 1712800000


def test_get_pane_last_used_returns_zero_on_empty(mgr):
    with patch.object(mgr, "_run_tmux", MagicMock(return_value=MagicMock(stdout=""))):
        assert mgr.get_pane_last_used("%5") == 0


def test_get_pane_last_used_returns_zero_on_non_int(mgr):
    with patch.object(mgr, "_run_tmux", MagicMock(return_value=MagicMock(stdout="not-a-number"))):
        assert mgr.get_pane_last_used("%5") == 0
