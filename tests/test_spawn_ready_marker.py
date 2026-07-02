"""Defect E (2026-07-01): model-agnostic, banner-proof spawn readiness.

Regression pins for the readiness-detection half of defect E. The old ready
markers were ``("bypass permissions on", "/effort")``:

  * juggle spawns agents WITHOUT --dangerously-skip-permissions, so a settled
    ready pane shows ``accept edits on`` — never ``bypass permissions on``;
  * ``/effort`` is a transient boot-time widget that disappears once the pane
    goes idle.

So a fully-ready, settled pane matched NEITHER marker: detection only
succeeded by racing the transient ``/effort`` flash during boot, and any
interstitial (folder-trust prompt, first-run banner) that consumed that window
caused a false "never ready" and a multi-minute block. Readiness must key on a
STABLE structural marker of the interactive input box — the mode-cycle hint,
present in every permission mode and model.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# A real settled ready pane captured from Claude Code v2.1.198 (accept-edits
# mode, model=sonnet): NO "bypass permissions on", NO "/effort".
_SETTLED_ACCEPT_EDITS_PANE = "\n".join(
    [
        "─" * 40,
        "❯ ",
        "─" * 40,
        "  5h[  14%  ]⏰3h46m | 7d[ 3% ]⏰4d21h | Sonnet 5(0/1.0M) | all good~",
        "  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents",
    ]
)


def test_default_claude_readiness_markers_include_stable_structural_marker():
    """The built-in default claude harness must key on the stable structural
    marker, not the transient/mode-specific chrome that defect E rode on."""
    from juggle_harness_defaults import HARNESS_DEFAULTS

    markers = HARNESS_DEFAULTS["claude"]["readiness_markers"]
    assert "shift+tab to cycle" in markers


def _markers_from_defaults(monkeypatch):
    """Force wait_for_ready_to_paste to use the built-in DEFAULT markers,
    independent of the developer's live ~/.juggle/config.json."""
    import juggle_tmux
    from juggle_harness_defaults import HARNESS_DEFAULTS

    ready = tuple(HARNESS_DEFAULTS["claude"]["readiness_markers"])
    sub = tuple(HARNESS_DEFAULTS["claude"]["submission_markers"])
    monkeypatch.setattr(juggle_tmux, "_harness_markers", lambda: (ready, sub))


def test_settled_accept_edits_pane_detected_ready(monkeypatch):
    """A settled accept-edits pane (no bypass/effort chrome) must read ready."""
    from juggle_tmux import JuggleTmuxManager

    monkeypatch.delenv("JUGGLE_TMUX_MOCK_NOT_READY_PANES", raising=False)
    _markers_from_defaults(monkeypatch)
    mgr = JuggleTmuxManager(session_name="juggle")
    monkeypatch.setattr(
        mgr, "_run_tmux",
        lambda *a, **k: SimpleNamespace(stdout=_SETTLED_ACCEPT_EDITS_PANE, returncode=0),
    )

    assert mgr.wait_for_ready_to_paste("%1", attempts=1, interval=0) is True


def test_folder_trust_prompt_not_detected_ready(monkeypatch):
    """The folder-trust gate must NOT be mistaken for a ready pane."""
    from juggle_tmux import JuggleTmuxManager

    trust_screen = (
        " Quick safety check: Is this a project you created or one you trust?\n"
        " ❯ 1. Yes, I trust this folder\n"
        "   2. No, exit\n"
        " Enter to confirm · Esc to cancel\n"
    )
    monkeypatch.delenv("JUGGLE_TMUX_MOCK_NOT_READY_PANES", raising=False)
    _markers_from_defaults(monkeypatch)
    mgr = JuggleTmuxManager(session_name="juggle")
    monkeypatch.setattr(
        mgr, "_run_tmux",
        lambda *a, **k: SimpleNamespace(stdout=trust_screen, returncode=0),
    )

    assert mgr.wait_for_ready_to_paste("%1", attempts=1, interval=0) is False


def test_wait_for_ready_beats_heartbeat_while_blocking_in_watchdog(monkeypatch):
    """Defect E: a long readiness wait must NOT freeze the watchdog heartbeat.

    The spawn readiness poll runs inside the watchdog tick; when it blocked for
    minutes the heartbeat went stale and every CLI call warned "watchdog not
    running or unresponsive". Inside the watchdog process the poll must refresh
    the heartbeat as it waits."""
    import juggle_spawn_readiness
    from juggle_tmux import JuggleTmuxManager

    beats = []
    monkeypatch.setattr(juggle_spawn_readiness, "write_heartbeat",
                        lambda *a, **k: beats.append(1), raising=False)
    monkeypatch.setenv("JUGGLE_WATCHDOG_SANCTIONED", "1")
    monkeypatch.delenv("JUGGLE_TMUX_MOCK_NOT_READY_PANES", raising=False)
    mgr = JuggleTmuxManager(session_name="juggle")
    monkeypatch.setattr(
        mgr, "_run_tmux",
        lambda *a, **k: SimpleNamespace(stdout="still booting", returncode=0),
    )

    assert mgr.wait_for_ready_to_paste("%1", attempts=3, interval=0) is False
    assert len(beats) >= 1, "readiness wait must refresh the watchdog heartbeat"


def test_wait_for_ready_no_heartbeat_outside_watchdog(monkeypatch):
    """Outside the watchdog process (plain CLI dispatch) the readiness poll must
    NOT touch the heartbeat — that would mask a genuinely dead watchdog."""
    import juggle_spawn_readiness
    from juggle_tmux import JuggleTmuxManager

    beats = []
    monkeypatch.setattr(juggle_spawn_readiness, "write_heartbeat",
                        lambda *a, **k: beats.append(1), raising=False)
    monkeypatch.delenv("JUGGLE_WATCHDOG_SANCTIONED", raising=False)
    monkeypatch.delenv("JUGGLE_TMUX_MOCK_NOT_READY_PANES", raising=False)
    mgr = JuggleTmuxManager(session_name="juggle")
    monkeypatch.setattr(
        mgr, "_run_tmux",
        lambda *a, **k: SimpleNamespace(stdout="still booting", returncode=0),
    )

    assert mgr.wait_for_ready_to_paste("%1", attempts=3, interval=0) is False
    assert beats == []
