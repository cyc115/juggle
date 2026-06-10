"""JuggleTmuxManager tests: send_message delivery, queueing, error paths (split from test_juggle_tmux.py, 2026-06-10)."""

import sys
from pathlib import Path
import os
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



import os


def test_send_message_mock_returns_true(mgr):
    """JUGGLE_TMUX_MOCK_SEND=1 bypasses real tmux and returns True."""
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_SEND": "1"}):
        result = mgr.send_message("%3", "steer this way")
    assert result is True


def test_send_message_uses_load_buffer_not_bare_send_keys(mgr):
    """send_message must paste via load-buffer/paste-buffer, not bare send-keys."""
    tmux_calls = []

    def capture(*args):
        tmux_calls.append(args[0])
        if args[0] == "list-panes":
            return _ok(stdout="%3\n")
        if args[0] == "capture-pane":
            return _ok(stdout="esc to interrupt\n")
        return _ok()

    with patch.object(mgr, "_run_tmux", side_effect=capture):
        with patch("juggle_tmux._pane_has_juggle_agent_env", return_value=True):
            mgr.send_message("%3", "add edge case handling")

    assert "load-buffer" in tmux_calls
    assert "paste-buffer" in tmux_calls


def test_send_message_raises_if_pane_missing(mgr):
    """Raises RuntimeError when pane does not exist in session."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _ok(stdout="%1\n%2\n")  # %99 not listed
        with pytest.raises(RuntimeError, match="not found"):
            mgr.send_message("%99", "hello")


def test_send_message_raises_if_no_live_agent(mgr):
    """Raises RuntimeError when pane exists but no live agent process."""
    with patch.object(mgr, "verify_pane", return_value=True):
        with patch("juggle_tmux._pane_has_juggle_agent_env", return_value=False):
            with pytest.raises(RuntimeError, match="process"):
                mgr.send_message("%3", "hello")


def test_send_message_raises_on_submission_failure(mgr):
    """Raises RuntimeError if wait_for_submission times out."""
    with patch.object(mgr, "verify_pane", return_value=True):
        with patch("juggle_tmux._pane_has_juggle_agent_env", return_value=True):
            with patch.object(mgr, "wait_for_submission", return_value=False):
                with patch.object(mgr, "_run_tmux", return_value=_ok()):
                    with pytest.raises(RuntimeError, match="submission"):
                        mgr.send_message("%3", "hello")


def test_send_message_returns_queued_when_pane_shows_queue_indicator(mgr):
    """send_message on a busy pane returns 'queued' instead of raising RuntimeError.

    When the agent is mid-turn, Claude Code queues the incoming message and shows
    'Press up to edit queued messages'. That IS success — the message landed.
    """
    queue_pane_output = (
        "Agent is processing…\nPress up to edit queued messages\n> \n"
    )

    def fake_run_tmux(*args):
        if args[0] == "capture-pane":
            return _ok(stdout=queue_pane_output)
        return _ok()

    with (
        patch.object(mgr, "verify_pane", return_value=True),
        patch("juggle_tmux._pane_has_juggle_agent_env", return_value=True),
        patch.object(mgr, "wait_for_submission", return_value=False),
        patch.object(mgr, "_run_tmux", side_effect=fake_run_tmux),
        patch("time.sleep"),
    ):
        result = mgr.send_message("%3", "steer this way")

    assert result == "queued", f"expected 'queued', got {result!r}"


def test_send_message_still_raises_when_neither_submitted_nor_queued(mgr):
    """send_message raises RuntimeError when pane shows neither submission nor queue."""
    with (
        patch.object(mgr, "verify_pane", return_value=True),
        patch("juggle_tmux._pane_has_juggle_agent_env", return_value=True),
        patch.object(mgr, "wait_for_submission", return_value=False),
        patch.object(mgr, "_run_tmux", return_value=_ok(stdout="some unrelated output\n")),
        patch("time.sleep"),
    ):
        with pytest.raises(RuntimeError, match="submission"):
            mgr.send_message("%3", "hello")

