"""JuggleTmuxManager tests: send_task paste/verify flow (split from test_juggle_tmux.py, 2026-06-10)."""

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


def test_send_task_loads_and_pastes(mgr):
    """send_task issues load-buffer, paste-buffer, and send-keys C-m."""
    with (
        patch.object(mgr, "wait_for_ready_to_paste", return_value=True),
        patch.object(mgr, "wait_for_submission", return_value=True),
        patch("subprocess.run") as mock_run,
        patch("time.sleep"),
        patch("juggle_tmux.uuid"),
    ):
        mock_run.return_value = _ok()
        mgr.send_task("%3", "do something")
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert any("load-buffer" in c for c in calls)
    assert any("paste-buffer" in c for c in calls)
    assert any("send-keys" in c for c in calls)


# --- send_task (refactored) ----------------------------------------------


def test_send_task_calls_wait_for_ready_then_wait_for_submission(mgr):
    """send_task must verify readiness before paste and submission after Enter."""
    call_order = []

    def _ready(*args, **kwargs):
        del args, kwargs
        call_order.append("ready")
        return True

    def _submit(*args, **kwargs):
        del args, kwargs
        call_order.append("submit")
        return True

    with (
        patch.object(mgr, "wait_for_ready_to_paste", side_effect=_ready),
        patch.object(mgr, "wait_for_submission", side_effect=_submit),
        patch("subprocess.run", return_value=_ok()),
        patch("time.sleep"),
    ):
        mgr.send_task("%3", "task body")
    assert call_order == ["ready", "submit"], (
        f"expected wait_for_ready before wait_for_submission; got {call_order}"
    )


def test_send_task_raises_when_pane_not_ready(mgr):
    """If wait_for_ready_to_paste returns False, send_task raises RuntimeError."""
    with (
        patch.object(mgr, "wait_for_ready_to_paste", return_value=False),
        patch.object(mgr, "wait_for_submission") as mock_submit,
        patch("subprocess.run", return_value=_ok()),
    ):
        with pytest.raises(RuntimeError, match="not ready"):
            mgr.send_task("%3", "task body")
    mock_submit.assert_not_called()


def test_send_task_raises_on_unverified_submission(mgr):
    """If wait_for_submission returns False, send_task raises RuntimeError.

    Updated from the old warn-only behavior (Fix 1: unsubmitted tasks silently
    left agent at 0 tokens — orchestrator had to manually press Enter x3).
    """
    import pytest
    with (
        patch.object(mgr, "wait_for_ready_to_paste", return_value=True),
        patch.object(mgr, "wait_for_submission", return_value=False),
        patch("subprocess.run", return_value=_ok()),
        patch("time.sleep"),
    ):
        with pytest.raises(RuntimeError, match="submission not verified"):
            mgr.send_task("%3", "task body")


def test_send_task_does_not_raise_when_agent_already_running(mgr):
    """send_task must not raise when a fast agent consumed the prompt before verification.

    Repro: panes %763 and %811 — both tasks submitted and processed correctly,
    but send_task raised 'submission not verified' because wait_for_submission
    didn't recognise the ⏺ running state.
    """
    tool_call_output = "⏺ Read(\"src/juggle_tmux.py\")\n  content here\n"

    def fake_tmux(*args):
        if args[0] == "capture-pane":
            return _ok(stdout=tool_call_output)
        return _ok()

    with (
        patch.object(mgr, "wait_for_ready_to_paste", return_value=True),
        patch.object(mgr, "_run_tmux", side_effect=fake_tmux),
        patch("time.sleep"),
    ):
        # Must not raise RuntimeError
        pane_hash = mgr.send_task("%763", "implement the new feature")


# --- false-negative: Enter landed but the input-box heuristic lagged ---------


def test_send_task_succeeds_when_submission_lagged_but_agent_busy(mgr):
    """Dispatch false-negative repro (incident BK/of-init-optional-key).

    paste-buffer + C-m landed and the agent ran the task, but the
    submission/activity markers never rendered inside wait_for_submission's
    polling window. The input box is CLEARED (prompt is gone). A live
    JUGGLE_IS_AGENT process confirms the dispatch via side-effect — send_task
    must NOT raise the spurious 'submission not verified' RuntimeError.
    """
    cleared = "  earlier scrollback line\n  another line of output\n"

    def fake_tmux(*args):
        if args[0] == "capture-pane":
            return _ok(stdout=cleared)
        return _ok()

    with (
        patch.object(mgr, "wait_for_ready_to_paste", return_value=True),
        patch.object(mgr, "_run_tmux", side_effect=fake_tmux),
        patch("juggle_tmux._pane_has_juggle_agent_env", return_value=True),
        patch("time.sleep"),
    ):
        pane_hash = mgr.send_task("%763", "implement the feature end to end")
    assert pane_hash  # no RuntimeError


def test_wait_for_submission_confirmed_by_agent_busy_side_effect(mgr):
    """Box cleared + no markers + live agent process → confirmed submitted (True)."""
    cleared = "  scrollback\n  more output\n"
    with (
        patch.object(mgr, "_run_tmux", return_value=_ok(stdout=cleared)),
        patch("juggle_tmux._pane_has_juggle_agent_env", return_value=True),
        patch("time.sleep"),
    ):
        result = mgr.wait_for_submission(
            "%3", "some prompt text here", timeout=2, max_enter_retries=0
        )
    assert result is True


def test_wait_for_submission_false_when_cleared_but_no_live_agent(mgr):
    """Box cleared but NO live agent process → cannot confirm → False (no false-positive)."""
    cleared = "  idle scrollback only\n"
    with (
        patch.object(mgr, "_run_tmux", return_value=_ok(stdout=cleared)),
        patch("juggle_tmux._pane_has_juggle_agent_env", return_value=False),
        patch("time.sleep"),
    ):
        result = mgr.wait_for_submission(
            "%3", "prompt body text", timeout=2, max_enter_retries=0
        )
    assert result is False


def test_wait_for_submission_false_when_stuck_despite_live_agent(mgr):
    """A genuinely stuck prompt (still in the input box) stays False even when the
    agent process is alive — side-effect confirmation only rescues a CLEARED box,
    never a box still holding unsubmitted input."""
    prompt = "stuck prompt body that never submits to the agent"
    stuck = _ok(stdout=f"> {prompt} still here\n")
    with (
        patch.object(mgr, "_run_tmux", return_value=stuck),
        patch("juggle_tmux._pane_has_juggle_agent_env", return_value=True),
        patch("time.sleep"),
    ):
        result = mgr.wait_for_submission("%3", prompt, timeout=2, max_enter_retries=1)
    assert result is False
