"""Active suite: watchdog detects + handles all 5 states.
Will raise ImportError until src/juggle_watchdog.py is implemented — expected.
"""
import sqlite3
import subprocess
import time
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SNAPSHOT_DIR = Path.home() / ".juggle" / "watchdog" / "snapshots"
TEST_SESSION = "juggle-watchdog-test"

# This import gates the entire suite. ImportError until watchdog ships.
from juggle_watchdog import inspect_agent  # type: ignore[import]  # noqa: E402


def _send(pane_id, cmd):
    subprocess.run(["tmux", "send-keys", "-t", pane_id, cmd, "Enter"], check=True)


def _capture(pane_id) -> str:
    r = subprocess.run(
        ["tmux", "capture-pane", "-pt", pane_id], capture_output=True, text=True
    )
    return r.stdout


def _action_items(db, tid) -> list:
    with db._connect() as c:
        c.row_factory = sqlite3.Row
        return c.execute(
            "SELECT * FROM action_items WHERE thread_id=? AND dismissed_at IS NULL", (tid,)
        ).fetchall()


def _notifications(db, tid) -> list:
    with db._connect() as c:
        c.row_factory = sqlite3.Row
        return c.execute(
            "SELECT * FROM notifications_v2 WHERE thread_id=?", (tid,)
        ).fetchall()


def _agent_row(db, agent_id) -> dict | None:
    with db._connect() as c:
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return dict(r) if r else None


def test_working_no_false_positive(tmux_pane, fake_agent, test_db):
    """Working agent: inspect returns 'working', no spurious action items."""
    _send(tmux_pane, f"bash {FIXTURE_DIR}/working.sh")
    time.sleep(3)
    result = inspect_agent(fake_agent["agent_id"], test_db, TEST_SESSION)
    assert result["state"] == "working"
    assert result["action_item_id"] is None
    assert len(_action_items(test_db, fake_agent["thread_id"])) == 0


def test_recoverable_prompt_auto_dismissed(tmux_pane, fake_agent, test_db):
    """Recoverable prompt: watchdog sends '2', dialog clears, notification logged, no action item."""
    _send(tmux_pane, f"bash {FIXTURE_DIR}/recoverable-prompt.sh")
    time.sleep(2)
    result = inspect_agent(fake_agent["agent_id"], test_db, TEST_SESSION)
    assert result["state"] == "recoverable_prompt"
    assert "sent_key" in result["actions"]
    time.sleep(1)
    content = _capture(tmux_pane)
    assert "Response received" in content
    assert result["action_item_id"] is None
    notifs = _notifications(test_db, fake_agent["thread_id"])
    assert len(notifs) == 1


def test_stalled_silent_action_item_filed(tmux_pane, fake_agent, test_db, monkeypatch):
    """Stalled silent: action item filed (failure/high) + snapshot written."""
    monkeypatch.setenv("JUGGLE_WATCHDOG_STALL_SECS", "1")
    _send(tmux_pane, f"bash {FIXTURE_DIR}/stalled-silent.sh")
    time.sleep(2)
    result = inspect_agent(fake_agent["agent_id"], test_db, TEST_SESSION)
    assert result["state"] == "stalled_silent"
    assert result["action_item_id"] is not None
    items = _action_items(test_db, fake_agent["thread_id"])
    assert len(items) == 1
    assert items[0]["type"] == "failure"
    assert items[0]["priority"] == "high"
    snapshots = list(SNAPSHOT_DIR.glob(f"{fake_agent['agent_id']}-*.txt"))
    assert len(snapshots) >= 1
    for s in snapshots:
        s.unlink(missing_ok=True)


def test_crashed_thread_marked_failed(tmux_pane, fake_agent, test_db):
    """Crashed: thread marked failed, action item filed, agent record cleaned up."""
    _send(tmux_pane, f"bash {FIXTURE_DIR}/crashed.sh")
    time.sleep(2)
    result = inspect_agent(fake_agent["agent_id"], test_db, TEST_SESSION)
    assert result["state"] == "crashed"
    assert result["action_item_id"] is not None
    items = _action_items(test_db, fake_agent["thread_id"])
    assert len(items) == 1
    thread = test_db.get_thread(fake_agent["thread_id"])
    assert thread["status"] == "failed"
    agent = _agent_row(test_db, fake_agent["agent_id"])
    assert agent is None or agent["status"] in ("idle", "dead")


def test_stuck_at_prompt_auto_unstuck(tmux_pane, fake_agent, test_db):
    """Stuck-at-prompt: watchdog sends Enter, pane advances, notification logged, no action item."""
    _send(tmux_pane, f"bash {FIXTURE_DIR}/stuck-at-prompt.sh")
    time.sleep(2)
    result = inspect_agent(fake_agent["agent_id"], test_db, TEST_SESSION)
    assert result["state"] == "stuck_at_prompt"
    assert "sent_enter" in result["actions"]
    time.sleep(1)
    content = _capture(tmux_pane)
    assert "Executing task..." in content
    assert result["action_item_id"] is None
    notifs = _notifications(test_db, fake_agent["thread_id"])
    assert len(notifs) == 1
