"""Baseline: confirms detection gaps without watchdog daemon. All 5 MUST pass."""

import subprocess
import time
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures"
WAIT_SECS = 4


def _send(pane_id, cmd):
    subprocess.run(["tmux", "send-keys", "-t", pane_id, cmd, "Enter"], check=True)


def _capture(pane_id) -> str:
    r = subprocess.run(
        ["tmux", "capture-pane", "-pt", pane_id], capture_output=True, text=True
    )
    return r.stdout


def _action_count(db, tid) -> int:
    with db._connect() as c:
        return c.execute(
            "SELECT COUNT(*) FROM action_items WHERE thread_id=? AND dismissed_at IS NULL",
            (tid,),
        ).fetchone()[0]


def _notif_count(db, tid) -> int:
    with db._connect() as c:
        return c.execute(
            "SELECT COUNT(*) FROM notifications_v2 WHERE thread_id=?", (tid,)
        ).fetchone()[0]


def _agent_status(db, agent_id) -> str:
    with db._connect() as c:
        row = c.execute("SELECT status FROM agents WHERE id=?", (agent_id,)).fetchone()
        return row[0] if row else "missing"


def test_working_control(tmux_pane, fake_agent, test_db):
    """Control: working agent emits output. No intervention expected."""
    _ = fake_agent, test_db  # fixtures needed for DB setup; not queried in control case
    _send(tmux_pane, f"bash {FIXTURE_DIR}/working.sh")
    time.sleep(WAIT_SECS)
    content = _capture(tmux_pane)
    assert "working..." in content


def test_recoverable_prompt_gap(tmux_pane, fake_agent, test_db):
    """GAP: permission dialog not auto-dismissed without watchdog."""
    _send(tmux_pane, f"bash {FIXTURE_DIR}/recoverable-prompt.sh")
    time.sleep(WAIT_SECS)
    assert _action_count(test_db, fake_agent["thread_id"]) == 0
    assert _notif_count(test_db, fake_agent["thread_id"]) == 0
    assert _agent_status(test_db, fake_agent["agent_id"]) == "busy"


def test_stalled_silent_gap(tmux_pane, fake_agent, test_db):
    """GAP: silent stall not detected without watchdog."""
    _send(tmux_pane, f"bash {FIXTURE_DIR}/stalled-silent.sh")
    time.sleep(WAIT_SECS)
    assert _action_count(test_db, fake_agent["thread_id"]) == 0
    assert _notif_count(test_db, fake_agent["thread_id"]) == 0
    assert _agent_status(test_db, fake_agent["agent_id"]) == "busy"


def test_crashed_gap(tmux_pane, fake_agent, test_db):
    """GAP: crash not detected without watchdog."""
    _send(tmux_pane, f"bash {FIXTURE_DIR}/crashed.sh")
    time.sleep(WAIT_SECS)
    assert _action_count(test_db, fake_agent["thread_id"]) == 0
    assert _notif_count(test_db, fake_agent["thread_id"]) == 0
    assert _agent_status(test_db, fake_agent["agent_id"]) == "busy"


def test_stuck_at_prompt_gap(tmux_pane, fake_agent, test_db):
    """GAP: stuck-at-prompt not detected without watchdog."""
    _send(tmux_pane, f"bash {FIXTURE_DIR}/stuck-at-prompt.sh")
    time.sleep(WAIT_SECS)
    assert _action_count(test_db, fake_agent["thread_id"]) == 0
    assert _notif_count(test_db, fake_agent["thread_id"]) == 0
    assert _agent_status(test_db, fake_agent["agent_id"]) == "busy"
