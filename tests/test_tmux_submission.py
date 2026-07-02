"""JuggleTmuxManager tests: wait_for_ready_to_paste + wait_for_submission incl. collapsed-paste and scrollback-tail regressions, v1.47.1 extended submitted-state detection (split from test_juggle_tmux.py, 2026-06-10)."""

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
        result = mgr.wait_for_ready_to_paste("%3", attempts=10, interval=1)
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
        assert mgr.wait_for_ready_to_paste("%3", attempts=5, interval=1) is True


def test_wait_for_ready_to_paste_returns_false_after_timeout(mgr):
    """Returns False if no readiness marker appears before timeout."""
    with (
        patch.object(
            mgr, "_run_tmux", return_value=MagicMock(stdout="zsh:1: command not found")
        ),
        patch("time.sleep"),
    ):
        result = mgr.wait_for_ready_to_paste("%3", attempts=2, interval=1)
    assert result is False


def test_wait_for_ready_to_paste_uses_settings_defaults(mgr):
    """With no args, attempts/interval come from settings; no sleep after the
    final attempt; total polls == configured attempts."""
    cfg = {
        "session_name": "juggle",
        "ready_poll_attempts": 4,
        "ready_poll_interval_secs": 10,
    }
    with (
        patch.object(
            mgr, "_run_tmux", return_value=MagicMock(stdout="no marker here")
        ) as mock_tmux,
        patch("juggle_tmux._get_settings", return_value={"tmux": cfg}),
        patch("time.sleep") as mock_sleep,
    ):
        result = mgr.wait_for_ready_to_paste("%3")
    assert result is False
    assert mock_tmux.call_count == 4  # configured attempts
    assert mock_sleep.call_count == 3  # attempts - 1 (no sleep after last)
    mock_sleep.assert_called_with(10)  # configured interval


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


def test_wait_for_submission_returns_true_for_unenumerated_spinner_glyph_2026_07_02(mgr):
    """Regression: 2026-07-02 false-positive stall nudge on agent ZJ — same glyph
    enumeration fragility applies here (a submitted-but-unenumerated-glyph pane
    could be mistaken for stuck, spamming Enter retries). The structural
    active-status-line pattern (elapsed-time + '↓ tokens' suffix) must be
    recognised as evidence of submission even when the glyph ('✢') isn't in
    _SUBMISSION_MARKERS.
    """
    prompt = "hello"
    active_output = "✢ Waddling… (24m 30s · ↓ 29.7k tokens)\n"

    with (
        patch.object(mgr, "_run_tmux", return_value=MagicMock(stdout=active_output)),
        patch("time.sleep"),
    ):
        result = mgr.wait_for_submission("%3", prompt, timeout=3, max_enter_retries=0)

    assert result is True, (
        "unenumerated spinner glyph with a structural active-status line must "
        "still be recognised as submitted/active"
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


def test_wait_for_submission_sends_escape_before_enter_in_insert_mode(mgr):
    """When pane is in vim INSERT mode, Escape is sent before C-m to exit the mode first."""
    # First poll: vim INSERT mode with CC status bar (wait_for_ready_to_paste would say ready)
    insert_output = "-- INSERT -- ⏵⏵ bypass permissions on\n"
    marker_output = "✻ Working… (esc to interrupt)\n"

    outputs = iter([insert_output, marker_output])
    key_calls: list = []

    def fake_run(*args):
        if args[0] == "capture-pane":
            return MagicMock(stdout=next(outputs))
        if args[0] == "send-keys":
            key_calls.append(list(args))
        return MagicMock(stdout="")

    with patch.object(mgr, "_run_tmux", side_effect=fake_run), patch("time.sleep"):
        result = mgr.wait_for_submission("%3", "hello", timeout=10, max_enter_retries=3)

    assert result is True
    sent_keys = [c[-1] for c in key_calls]
    assert "Escape" in sent_keys, (
        f"INSERT mode must trigger an Escape before C-m; sent keys: {sent_keys}"
    )
    escape_idx = sent_keys.index("Escape")
    cm_indices = [i for i, k in enumerate(sent_keys) if k == "C-m"]
    assert cm_indices, f"C-m must still be sent after Escape; sent keys: {sent_keys}"
    assert escape_idx < cm_indices[0], (
        f"Escape must precede C-m; sent keys: {sent_keys}"
    )


def test_wait_for_submission_normal_mode_no_escape(mgr):
    """When stuck on collapsed paste (no INSERT mode), Escape is NOT sent — only C-m."""
    collapsed = "  ❯ [Pasted text #1 +5 lines]\n"
    marker_output = "✻ Working… (esc to interrupt)\n"

    outputs = iter([collapsed, marker_output])
    key_calls: list = []

    def fake_run(*args):
        if args[0] == "capture-pane":
            return MagicMock(stdout=next(outputs))
        if args[0] == "send-keys":
            key_calls.append(list(args))
        return MagicMock(stdout="")

    with patch.object(mgr, "_run_tmux", side_effect=fake_run), patch("time.sleep"):
        result = mgr.wait_for_submission("%3", "hello", timeout=10, max_enter_retries=3)

    assert result is True
    sent_keys = [c[-1] for c in key_calls]
    assert "Escape" not in sent_keys, (
        f"Non-INSERT stuck must NOT send Escape; sent keys: {sent_keys}"
    )
    assert "C-m" in sent_keys, f"C-m must be sent; sent keys: {sent_keys}"


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



# ── v1.47.1: extended submitted-state detection (false-negative fix) ─────────


def test_wait_for_submission_returns_true_when_agent_already_running_with_tool_call(mgr):
    """Agent consumed prompt and started tool calls before verification snapshot.

    Repro (v1.47.0): send_task to a fast agent — prompt left the input box,
    but submission markers ('esc to interrupt' / '✻') had already scrolled off
    by the time wait_for_submission polled. Pane shows ⏺ tool output with no
    prompt text in input box. Old code: no marker → timeout → raises. Fix: ⏺
    activity marker + empty input box → submitted.
    """
    prompt = "implement the new feature across the codebase"
    # Pane shows tool call output — prompt is gone, no submission marker, ⏺ present
    tool_call_output = "⏺ Bash(\"find . -name '*.py'\")\n  file1.py\n  file2.py\n"

    with (
        patch.object(mgr, "_run_tmux", return_value=MagicMock(stdout=tool_call_output)),
        patch("time.sleep"),
    ):
        result = mgr.wait_for_submission("%763", prompt, timeout=3, max_enter_retries=0)

    assert result is True, (
        "⏺ activity marker with empty input box must be treated as submitted; "
        "agent consumed prompt faster than verification snapshot — this is the v1.47.0 false-negative"
    )


def test_wait_for_submission_returns_true_for_queued_indicator(mgr):
    """'Press up to edit queued messages' means the message landed in Claude Code's queue.

    This is the same queued-state logic that send_message handles (v1.46.1). Both
    send_task and send_message now share one verifier that recognises this state.
    """
    prompt = "steer the agent this way"
    queued_output = "Agent is processing…\nPress up to edit queued messages\n> \n"

    with (
        patch.object(mgr, "_run_tmux", return_value=MagicMock(stdout=queued_output)),
        patch("time.sleep"),
    ):
        result = mgr.wait_for_submission("%811", prompt, timeout=3, max_enter_retries=0)

    assert result is True, (
        "'Press up to edit queued messages' must be treated as submitted; "
        "agent is busy and the message is queued — this is a success state, not a failure"
    )


def test_wait_for_submission_genuinely_stuck_prompt_in_box_still_returns_false(mgr):
    """Prompt text still visible in input box after retries → False (genuine failure).

    Ensures the extended detection does NOT accidentally return True when the task
    is genuinely unsubmitted (prompt text still in the input box).
    """
    prompt = "implement the new feature across the codebase"
    head = prompt[:40]
    # Input box still shows the prompt head
    stuck_output = f"❯ {head} more words here\n"

    with (
        patch.object(mgr, "_run_tmux", return_value=MagicMock(stdout=stuck_output)),
        patch("time.sleep"),
    ):
        result = mgr.wait_for_submission("%3", prompt, timeout=2, max_enter_retries=0)

    assert result is False, (
        "prompt text visible in input box must still return False — "
        "genuine stuck state must not be masked by extended detection"
    )


