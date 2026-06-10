"""JuggleTmuxManager tests: sessions, pane spawn/verify/kill, start_claude env, spawn/decommission agents, parallel-spawn + reap-grace regressions (split from test_juggle_tmux.py, 2026-06-10)."""

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


def test_start_claude_denials_go_to_settings_file_not_command_line(mgr, tmp_path):
    """A large deny list must NOT bloat the pasted command. It is written to a
    `--settings <file>` overlay (short, fixed token) instead of a long
    `--disallowedTools a,b,c,...` flag, which pastes unreliably."""
    import json as _json
    from pathlib import Path as _Path

    import juggle_agent_settings as _jas

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

    tmux_settings = {
        "agent": {"claude_launch_command": "claude --dangerously-skip-permissions"}
    }
    overlay_settings = {
        "paths": {"config_dir": str(tmp_path)},
        "agent": {
            "settings_overlay_base": {"permissions": {"deny": big_denied}},
            "settings_overlay_by_role": {
                "coder": {"permissions": {"deny": ["NotebookEdit"]}}
            },
        },
    }
    with (
        patch("juggle_tmux._get_settings", return_value=tmux_settings),
        patch.object(_jas, "get_settings", return_value=overlay_settings),
        patch.object(mgr, "_run_tmux", side_effect=capture_tmux),
    ):
        mgr.start_claude_in_pane("%5", role="coder")

    assert written_content, "load-buffer was never called"
    cmd = written_content[0]
    # The fragile long list is OFF the command line...
    assert "--disallowedTools" not in cmd
    assert "mcp__tool_0__action" not in cmd
    # ...and replaced by a short, fixed --settings token.
    assert "--settings " in cmd
    settings_path = _Path(cmd.split("--settings ")[1].split()[0].strip("'\""))
    overlay = _json.loads(settings_path.read_text())
    deny = overlay["permissions"]["deny"]
    assert all(f"mcp__tool_{i}__action" in deny for i in range(200))
    assert "NotebookEdit" in deny  # role-specific denial included
    assert "JUGGLE_AGENT_AUDIT" not in cmd  # audit mode off by default


def test_start_claude_sets_audit_env_when_audit_mode(mgr, tmp_path):
    """With agent.audit_mode on, the agent is tagged JUGGLE_AGENT_AUDIT=1 so its
    PreToolUse telemetry is recorded as 'audit'."""
    from pathlib import Path as _Path

    import juggle_agent_settings as _jas

    written = []

    def capture_tmux(*args):
        m = MagicMock()
        m.returncode = 0
        m.stdout = ""
        if args[0] == "load-buffer":
            written.append(_Path(args[-1]).read_text())
        return m

    tmux_settings = {
        "agent": {"claude_launch_command": "claude -p", "audit_mode": True}
    }
    overlay_settings = {
        "paths": {"config_dir": str(tmp_path)},
        "agent": {
            "settings_overlay_base": {},
            "settings_overlay_by_role": {"coder": {}},
            "audit_mode": True,
        },
    }
    with (
        patch("juggle_tmux._get_settings", return_value=tmux_settings),
        patch.object(_jas, "get_settings", return_value=overlay_settings),
        patch.object(mgr, "_run_tmux", side_effect=capture_tmux),
    ):
        mgr.start_claude_in_pane("%1", role="coder")

    assert written
    assert "JUGGLE_AGENT_AUDIT=1" in written[0]


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


# ---------------------------------------------------------------------------
# Parallel-spawn regression tests (root cause: split-window + reap grace)
#
# Bug: second concurrent agent fails because:
#   (A) spawn_pane uses split-window on first window → tiny unusable pane
#   (B) reap_stale_agents deletes agents within cold-start (no grace period)
#       even though agent_boot_grace_secs=120 already exists in settings.
# ---------------------------------------------------------------------------


def test_spawn_pane_uses_new_window_not_split(mgr):
    """spawn_pane must use new-window, never split-window on the first window.

    RED before fix: spawn_pane calls split-window first; a crowded first window
    produces a tiny pane that Claude cannot render in.
    GREEN after fix: spawn_pane always calls new-window so each agent gets a
    full-size independent window.
    """
    tmux_calls = []

    def fake_run(*args):
        tmux_calls.append(list(args))
        m = MagicMock()
        m.stdout = "%10\n"
        m.stderr = ""
        m.returncode = 0
        return m

    with patch.object(mgr, "_run_tmux", side_effect=fake_run):
        pane_id = mgr.spawn_pane()

    assert pane_id == "%10"
    cmds = [c[0] for c in tmux_calls]
    assert "new-window" in cmds, (
        f"spawn_pane must call new-window (got {cmds}); "
        "split-window on a crowded first window creates tiny unusable panes"
    )
    assert "split-window" not in cmds, (
        f"spawn_pane must not call split-window (got {cmds})"
    )


def test_reap_grace_protects_fresh_agent_with_missing_pane(tmp_path):
    """Agent just created (within cold-start grace) must NOT be reaped even
    if its pane is already gone.

    RED before fix: reap_stale_agents unconditionally deletes on verify_pane=False.
    GREEN after fix: agent_boot_grace_secs (default 120) creates a window during
    which a dead-pane agent is left alone to finish booting.
    """
    from unittest.mock import patch

    from juggle_db import JuggleDB
    from juggle_tmux import JuggleTmuxManager, reap_stale_agents

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    db.set_active(True)

    # Agent created right now — within any reasonable grace period.
    agent_id = db.create_agent(role="coder", pane_id="%new-agent")

    mock_mgr = MagicMock(spec=JuggleTmuxManager)
    mock_mgr.verify_pane.return_value = False  # pane appears dead (cold-start race)
    mock_mgr.session_name = "juggle-test"

    mock_settings = {"agent_idle_ttl_secs": 43200, "agent_boot_grace_secs": 120}

    with (
        patch("juggle_settings.get_settings", return_value=mock_settings),
        patch("subprocess.run", return_value=MagicMock(stdout="", returncode=0)),
    ):
        reaped = reap_stale_agents(db, mock_mgr)

    assert db.get_agent(agent_id) is not None, (
        "Agent within cold-start grace must not be deleted even if pane is gone; "
        "current code unconditionally deletes on verify_pane=False"
    )
    assert reaped == 0, f"Expected 0 reaped (within grace), got {reaped}"


def test_reap_grace_expires_and_deletes_old_missing_pane_agent(tmp_path):
    """Agent past cold-start grace with a dead pane SHOULD be reaped.

    Ensures the grace window does not accidentally protect genuinely stale agents.
    """
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    from juggle_db import JuggleDB
    from juggle_tmux import JuggleTmuxManager, reap_stale_agents

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    db.set_active(True)

    agent_id = db.create_agent(role="coder", pane_id="%old-agent")

    # Backdate created_at to 200s ago (past the 120s grace).
    old_ts = (datetime.now(timezone.utc) - timedelta(seconds=200)).isoformat()
    with db._connect() as conn:
        conn.execute("UPDATE agents SET created_at = ? WHERE id = ?", (old_ts, agent_id))
        conn.commit()

    mock_mgr = MagicMock(spec=JuggleTmuxManager)
    mock_mgr.verify_pane.return_value = False
    mock_mgr.session_name = "juggle-test"

    mock_settings = {"agent_idle_ttl_secs": 43200, "agent_boot_grace_secs": 120}

    with (
        patch("juggle_settings.get_settings", return_value=mock_settings),
        patch("subprocess.run", return_value=MagicMock(stdout="", returncode=0)),
    ):
        reaped = reap_stale_agents(db, mock_mgr)

    assert db.get_agent(agent_id) is None, (
        "Agent past cold-start grace with dead pane should be reaped"
    )
    assert reaped >= 1


# ── send_message ─────────────────────────────────────────────────────────────
