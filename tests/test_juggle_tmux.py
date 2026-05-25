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


def test_start_claude_sets_juggle_is_agent(mgr):
    """Agent panes must be launched with JUGGLE_IS_AGENT=1 so PreToolUse hooks can skip blocking."""
    from pathlib import Path as _Path

    launch_cmd_content = []

    def capture_tmux(*args):
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        if args[0] == "load-buffer":
            # args: ("load-buffer", "-b", buf_name, tmp_path)
            try:
                launch_cmd_content.append(_Path(args[-1]).read_text())
            except Exception:
                pass
        return m

    with patch.object(mgr, "_run_tmux", side_effect=capture_tmux):
        mgr.start_claude_in_pane("%5")

    assert launch_cmd_content, (
        "load-buffer was never called — command not written to temp file"
    )
    cmd = launch_cmd_content[0]
    assert cmd.startswith("env -u CLAUDE_PLUGIN_DATA JUGGLE_IS_AGENT=1 "), (
        f"Expected cmd to start with env prefix, got: {cmd!r}"
    )


def test_start_claude_large_command_no_truncation(mgr):
    """A >4KB launch command must arrive intact in the temp file, not via send-keys."""
    from pathlib import Path as _Path

    big_denied = [f"mcp__tool_{i}__action" for i in range(200)]

    written_content = []

    def capture_tmux(*args):
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        if args[0] == "load-buffer":
            try:
                written_content.append(_Path(args[-1]).read_text())
            except Exception:
                pass
        return m

    fake_settings = {
        "agent": {
            "claude_launch_command": "claude --dangerously-skip-permissions",
            "disallowed_tools_universal": big_denied,
            "disallowed_tools_by_role": {},
        }
    }
    with (
        patch("juggle_tmux._get_settings", return_value=fake_settings),
        patch.object(mgr, "_run_tmux", side_effect=capture_tmux),
    ):
        mgr.start_claude_in_pane("%5")

    assert written_content, "load-buffer was never called"
    cmd = written_content[0]
    assert len(cmd) > 4096, f"Expected cmd > 4KB, got {len(cmd)} bytes"
    assert all(f"mcp__tool_{i}__action" in cmd for i in range(200)), (
        "Tool names missing from written command — truncation detected"
    )


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


# --- wait_for_ready_to_paste ---------------------------------------------


def test_wait_for_ready_to_paste_returns_true_when_marker_appears(mgr):
    """Returns True once capture-pane output contains a readiness marker."""
    captures = [
        MagicMock(stdout=""),
        MagicMock(stdout=""),
        MagicMock(stdout=""),
        MagicMock(stdout="some chatter\nbypass permissions on (shift+tab to cycle)\n"),
    ]
    with (
        patch.object(mgr, "_run_tmux", side_effect=captures) as mock_tmux,
        patch("time.sleep"),
    ):
        result = mgr.wait_for_ready_to_paste("%3", timeout=10)
    assert result is True
    assert mock_tmux.call_count == 4
    # Each call should be capture-pane for the pane
    for c in mock_tmux.call_args_list:
        assert c.args[0] == "capture-pane"
        assert "%3" in c.args


def test_wait_for_ready_to_paste_recognises_effort_marker(mgr):
    """The '/effort' status-line slug also counts as readiness."""
    captures = [
        MagicMock(stdout=""),
        MagicMock(stdout="model: claude-opus  /effort high  /context 200k"),
    ]
    with patch.object(mgr, "_run_tmux", side_effect=captures), patch("time.sleep"):
        assert mgr.wait_for_ready_to_paste("%3", timeout=5) is True


def test_wait_for_ready_to_paste_returns_false_after_timeout(mgr):
    """Returns False if no readiness marker appears before timeout."""
    with (
        patch.object(
            mgr, "_run_tmux", return_value=MagicMock(stdout="zsh:1: command not found")
        ),
        patch("time.sleep"),
    ):
        result = mgr.wait_for_ready_to_paste("%3", timeout=2)
    assert result is False


# --- wait_for_submission (collapsed-paste regression) --------------------


def test_wait_for_submission_collapsed_paste_does_not_false_positive(mgr):
    """Collapsed-paste placeholder must NOT count as 'input cleared' (false positive).

    Old code: 'head not in bottom → True' fires immediately on a collapsed placeholder
    because the placeholder text is not the head. This is the root cause of the bug
    where tasks sit unsubmitted at the prompt (0 tokens) until a human presses Enter.

    Fixed code: SUCCESS requires a _SUBMISSION_MARKERS token. No marker → timeout → False.
    """
    prompt = "Implement a comprehensive refactoring across the entire codebase\nmore lines\nhere"
    # Bottom shows collapsed placeholder — head is NOT present, so old code returns True
    collapsed_output = "  ❯ [Pasted text #1 +2 lines]\n"

    with (
        patch.object(mgr, "_run_tmux", return_value=MagicMock(stdout=collapsed_output)),
        patch("time.sleep"),
    ):
        result = mgr.wait_for_submission("%3", prompt, timeout=3, max_enter_retries=0)

    assert result is False, (
        "collapsed-paste placeholder must not trigger 'cleared' false positive; "
        "expected False (no marker), got True"
    )


def test_wait_for_submission_collapsed_paste_retries_enter_then_succeeds(mgr):
    """While stuck on collapsed-paste, retry C-m each stuck poll; succeed when marker fires.

    Old code: returns True on first poll (false positive), never sends any Enter retry.
    Fixed code: detects stuck via '[Pasted text', sends C-m each poll, returns True on marker.
    """
    prompt = "Do a comprehensive refactor of the entire codebase and all modules"

    collapsed = "  ❯ [Pasted text #1 +1 lines]\n"
    marker_output = "✻ Working…  (esc to interrupt)\n"

    # poll 1 & 2: collapsed (stuck); poll 3: submission marker fires
    outputs = iter([collapsed, collapsed, marker_output])
    enter_calls: list = []

    def fake_run(*args):
        if args[0] == "capture-pane":
            return MagicMock(stdout=next(outputs))
        if args[0] == "send-keys":
            enter_calls.append(args)
        return MagicMock(stdout="")

    with patch.object(mgr, "_run_tmux", side_effect=fake_run), patch("time.sleep"):
        result = mgr.wait_for_submission("%3", prompt, timeout=10, max_enter_retries=5)

    assert result is True
    assert len(enter_calls) >= 1, (
        f"should have retried Enter while stuck on collapsed paste; "
        f"got {len(enter_calls)} calls (old code returns True immediately with 0 retries)"
    )
    assert enter_calls[0][-1] == "C-m", (
        f"retry should send C-m, got {enter_calls[0][-1]!r}"
    )


# --- wait_for_submission --------------------------------------------------


def test_wait_for_submission_returns_true_when_cleared_then_marker(mgr):
    """Input clears then submission marker fires → success; no C-m while not stuck."""
    prompt = "do the thing across the codebase quickly and quietly"
    head = prompt[:40]

    # poll 0: stuck (head in bottom); poll 1: cleared input + marker (realistic: Claude
    # clears the input and immediately starts processing)
    outputs = iter([
        MagicMock(stdout=f"> {head} more stuff\n"),        # stuck
        MagicMock(stdout="✻ Thinking… (esc to interrupt)\n"),  # submitted + processing
    ])
    send_key_calls: list = []

    def fake_run(*args):
        if args[0] == "capture-pane":
            return next(outputs)
        if args[0] == "send-keys":
            send_key_calls.append(args)
        return MagicMock(stdout="")

    with patch.object(mgr, "_run_tmux", side_effect=fake_run), patch("time.sleep"):
        result = mgr.wait_for_submission("%3", prompt, timeout=10, max_enter_retries=3)

    assert result is True
    # Only 1 stuck poll before marker fires — exactly 1 C-m (fired immediately on first stuck)
    assert len(send_key_calls) == 1, (
        f"expected 1 Enter retry on the stuck poll; got {len(send_key_calls)}"
    )


def test_wait_for_submission_returns_true_when_processing_marker_present(mgr):
    """'esc to interrupt' / '✻' marker means the prompt was submitted; return True."""
    prompt = "hello"
    # Use a function-based side_effect so send-keys calls don't consume capture outputs
    outputs = iter([
        MagicMock(stdout=f"> {prompt}\n"),                  # poll 0: stuck (head in bottom)
        MagicMock(stdout="✻ Thinking… (esc to interrupt)\n"),  # poll 1: marker
    ])

    def fake_run(*args):
        if args[0] == "capture-pane":
            return next(outputs)
        return MagicMock(stdout="")

    with patch.object(mgr, "_run_tmux", side_effect=fake_run), patch("time.sleep"):
        assert mgr.wait_for_submission("%3", prompt, timeout=10) is True


def test_wait_for_submission_retries_enter_when_stuck_then_marker(mgr):
    """Each stuck poll fires C-m; success when marker eventually appears."""
    prompt = "do the thing across the codebase quickly and quietly"
    head = prompt[:40]
    stuck = MagicMock(stdout=f"> {head} more stuff\n")
    submitted = MagicMock(stdout="✻ Thinking… (esc to interrupt)\n")

    # Sequence: stuck, stuck, submitted.
    # New code fires C-m immediately on each stuck poll (no consecutive_stuck wait).
    capture_outputs = [stuck, stuck, submitted]
    capture_iter = iter(capture_outputs)
    send_key_calls = []

    def fake_run_tmux(*args):
        if args[0] == "capture-pane":
            return next(capture_iter)
        if args[0] == "send-keys":
            send_key_calls.append(args)
            return MagicMock(stdout="")
        return MagicMock(stdout="")

    with patch.object(mgr, "_run_tmux", side_effect=fake_run_tmux), patch("time.sleep"):
        result = mgr.wait_for_submission("%3", prompt, timeout=10, max_enter_retries=3)
    assert result is True
    assert len(send_key_calls) == 2, (
        f"expected 2 Enter retries (one per stuck poll), got {len(send_key_calls)}"
    )
    assert send_key_calls[0][-1] == "C-m"
    assert "%3" in send_key_calls[0]


def test_wait_for_submission_returns_false_after_max_retries(mgr):
    """If the input box never clears, return False after exhausting retries + timeout."""
    prompt = "stuck forever"
    stuck = MagicMock(stdout=f"> {prompt} (still here)\n")

    send_key_calls = []

    def fake_run_tmux(*args):
        if args[0] == "capture-pane":
            return stuck
        if args[0] == "send-keys":
            send_key_calls.append(args)
            return MagicMock(stdout="")
        return MagicMock(stdout="")

    with patch.object(mgr, "_run_tmux", side_effect=fake_run_tmux), patch("time.sleep"):
        result = mgr.wait_for_submission("%3", prompt, timeout=6, max_enter_retries=3)
    assert result is False
    assert len(send_key_calls) == 3, (
        f"expected 3 retries (max), got {len(send_key_calls)}"
    )


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


def test_send_task_warns_but_does_not_raise_when_submission_unverified(mgr, caplog):
    """If wait_for_submission returns False, send_task logs a warning and returns."""
    import logging

    caplog.set_level(logging.WARNING)
    with (
        patch.object(mgr, "wait_for_ready_to_paste", return_value=True),
        patch.object(mgr, "wait_for_submission", return_value=False),
        patch("subprocess.run", return_value=_ok()),
        patch("time.sleep"),
    ):
        # Should not raise
        mgr.send_task("%3", "task body")
    assert any(
        "submission not verified" in r.message.lower()
        or "submission" in r.message.lower()
        for r in caplog.records
    ), f"expected a warning log; got {[r.message for r in caplog.records]}"


def test_spawn_agent_creates_db_record(mgr, tmp_path):
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()

    with (
        patch.object(mgr, "ensure_session"),
        patch.object(mgr, "spawn_pane", return_value="%7"),
        patch.object(mgr, "start_claude_in_pane"),
    ):
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
    with patch.object(
        mgr, "_run_tmux", MagicMock(return_value=MagicMock(stdout="not-a-number"))
    ):
        assert mgr.get_pane_last_used("%5") == 0


# --- scrollback-tail capture (-S flag) tests --------------------------------


def test_wait_for_submission_capture_uses_scrollback_flag(mgr):
    """capture-pane call in wait_for_submission must include -S for scrollback tail.

    Plain 'capture-pane -pt <pane>' only returns the visible region; a small or
    resized pane can hide the submission markers. The fix adds '-S -10' to reach
    10 lines into the scrollback history above the visible top.
    """
    capture_calls: list = []

    def fake_run(*args):
        if args[0] == "capture-pane":
            capture_calls.append(args)
            # Return a submission marker on the first poll so we exit quickly
            return MagicMock(stdout="✻ Working… (esc to interrupt)\n")
        return MagicMock(stdout="")

    with patch.object(mgr, "_run_tmux", side_effect=fake_run), patch("time.sleep"):
        mgr.wait_for_submission("%3", "hello", timeout=5)

    assert capture_calls, "wait_for_submission never called capture-pane"
    first_cap = capture_calls[0]
    assert "-S" in first_cap, (
        f"capture-pane in wait_for_submission must include -S for scrollback tail; "
        f"got args: {first_cap}"
    )


def test_wait_for_submission_detects_marker_in_scrollback_tail(mgr):
    """Submission marker buried in scrollback tail (tall pane) must still be found.

    Simulates a pane that is 50 lines tall but the submission marker only appears
    in the last 10 lines. With plain capture-pane (visible-only on small panes)
    this could be missed; with -S -10 it is always present in the output.
    """
    # Build a 50-line output where the marker is only in lines 41-50
    many_blank_lines = "\n" * 40
    marker_tail = "✻ Working… (esc to interrupt)\nsome output\n"
    tall_pane_output = many_blank_lines + marker_tail

    with (
        patch.object(
            mgr, "_run_tmux", return_value=MagicMock(stdout=tall_pane_output)
        ),
        patch("time.sleep"),
    ):
        result = mgr.wait_for_submission("%3", "hello", timeout=5)

    assert result is True, (
        "submission marker in scrollback tail must be detected; "
        "check that capture-pane uses -S and detection scans the tail"
    )


def test_wait_for_submission_detects_stuck_in_scrollback_tail(mgr):
    """Stuck-at-prompt state buried in scrollback tail is still detected and retried.

    Simulates a tall pane where the [Pasted text placeholder appears only in the
    last 10 lines of a 50-line buffer. The stuck detector must find it and send C-m.
    """
    many_blank_lines = "\n" * 40
    stuck_tail = "  ❯ [Pasted text #1 +2 lines]\n"
    tall_stuck_output = many_blank_lines + stuck_tail
    marker_output = "✻ Working… (esc to interrupt)\n"

    outputs = iter([tall_stuck_output, marker_output])
    enter_calls: list = []

    def fake_run(*args):
        if args[0] == "capture-pane":
            return MagicMock(stdout=next(outputs))
        if args[0] == "send-keys":
            enter_calls.append(args)
        return MagicMock(stdout="")

    with patch.object(mgr, "_run_tmux", side_effect=fake_run), patch("time.sleep"):
        result = mgr.wait_for_submission("%3", "hello", timeout=10, max_enter_retries=3)

    assert result is True
    assert len(enter_calls) >= 1, (
        "stuck-at-prompt in scrollback tail must trigger C-m retry; "
        f"got {len(enter_calls)} retries — stuck detection not scanning the tail"
    )
