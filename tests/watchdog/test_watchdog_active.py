"""Active suite: watchdog detects + handles all 5 states.
Skipped automatically when src/juggle_watchdog.py is not yet implemented.
Isolation guarantees (incident #4713):
  - test_db always uses a temp path (NEVER prod DB — enforced in conftest)
  - no real time.sleep() — pane content is polled; stall is injected via DB timestamp
  - assert_no_leaked_daemons autouse fixture catches any future regression
"""

import sqlite3
import subprocess
import time
import datetime
from pathlib import Path

import pytest

from juggle_watchdog_singleton import find_watchdog_pids

FIXTURE_DIR = Path(__file__).parent / "fixtures"
TEST_SESSION = "juggle-watchdog-test"

# Skips the entire module gracefully until juggle_watchdog ships.
_watchdog = pytest.importorskip(
    "juggle_watchdog", reason="juggle_watchdog not yet implemented"
)
inspect_agent = _watchdog.inspect_agent


@pytest.fixture(autouse=True)
def assert_no_leaked_daemons():
    """REGRESSION PIN (#4713): every test in this module must leave zero new
    daemon processes behind. Module-level autouse means it only covers
    test_watchdog_active.py — not other watchdog tests that spawn intentionally.
    """
    before = set(find_watchdog_pids())
    yield
    after = set(find_watchdog_pids())
    new_leaks = after - before
    assert not new_leaks, (
        f"Watchdog daemon(s) leaked after test: PIDs {sorted(new_leaks)}. "
        "Every test that can spawn a daemon MUST kill it in teardown."
    )


def _wait_pane(pane_id: str, marker: str, timeout: float = 5.0, interval: float = 0.05) -> str:
    """Poll tmux pane until marker appears or timeout; returns final content."""
    deadline = time.monotonic() + timeout
    content = ""
    while time.monotonic() < deadline:
        r = subprocess.run(
            ["tmux", "capture-pane", "-pt", pane_id],
            capture_output=True, text=True,
        )
        content = r.stdout
        if marker in content:
            return content
        time.sleep(interval)
    return content


def _send(pane_id, cmd):
    subprocess.run(["tmux", "send-keys", "-t", pane_id, cmd, "Enter"], check=True)


def _action_items(db, tid) -> list:
    with db._connect() as c:
        c.row_factory = sqlite3.Row
        return c.execute(
            "SELECT * FROM action_items WHERE thread_id=? AND dismissed_at IS NULL",
            (tid,),
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


def _set_last_active_past(db, agent_id: str, seconds_ago: float) -> None:
    """Backdate an agent's last_active so stall detection fires immediately."""
    past = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=seconds_ago)
    ).isoformat()
    with db._connect() as conn:
        conn.execute(
            "UPDATE agents SET last_active=? WHERE id=?", (past, agent_id)
        )


def test_working_no_false_positive(tmux_pane, fake_agent, test_db):
    """Working agent: inspect returns 'working', no spurious action items."""
    _send(tmux_pane, f"bash {FIXTURE_DIR}/working.sh")
    _wait_pane(tmux_pane, "working...")  # deterministic: poll instead of sleep
    result = inspect_agent(fake_agent["agent_id"], test_db, TEST_SESSION)
    assert result["state"] == "working"
    assert result["action_item_id"] is None
    assert len(_action_items(test_db, fake_agent["thread_id"])) == 0


def test_recoverable_prompt_auto_dismissed(tmux_pane, fake_agent, test_db):
    """Recoverable prompt: watchdog sends '2', dialog clears, notification logged, no action item."""
    _send(tmux_pane, f"bash {FIXTURE_DIR}/recoverable-prompt.sh")
    _wait_pane(tmux_pane, "1. Yes")  # poll until prompt appears
    result = inspect_agent(fake_agent["agent_id"], test_db, TEST_SESSION)
    assert result["state"] == "recoverable_prompt"
    assert "sent_key" in result["actions"]
    _wait_pane(tmux_pane, "Response received")  # poll for watchdog's key response
    assert result["action_item_id"] is None
    notifs = _notifications(test_db, fake_agent["thread_id"])
    assert len(notifs) == 1


def test_stalled_silent_action_item_filed(tmux_pane, fake_agent, test_db, monkeypatch):
    """Stalled silent: action item filed (failure/high) + snapshot written.

    Deterministic: last_active is backdated 120s so stall fires immediately
    without any real time.sleep(). JUGGLE_WATCHDOG_STALL_SECS=60 is the
    threshold; stall_for=120 >= 60 always passes.
    """
    monkeypatch.setenv("JUGGLE_WATCHDOG_STALL_SECS", "60")
    _send(tmux_pane, f"bash {FIXTURE_DIR}/stalled-silent.sh")
    _wait_pane(tmux_pane, "Starting analysis...")  # poll until script is running
    # Inject stall by backdating last_active — no sleep needed
    _set_last_active_past(test_db, fake_agent["agent_id"], seconds_ago=120)
    result = inspect_agent(fake_agent["agent_id"], test_db, TEST_SESSION)
    assert result["state"] == "stalled_silent"
    assert result["action_item_id"] is not None
    items = _action_items(test_db, fake_agent["thread_id"])
    assert len(items) == 1
    assert items[0]["type"] == "failure"
    assert items[0]["priority"] == "high"
    snapshot_dir = Path.home() / ".juggle" / "watchdog" / "snapshots"
    snapshots = list(snapshot_dir.glob(f"{fake_agent['agent_id']}-*.txt"))
    assert len(snapshots) >= 1
    for s in snapshots:
        s.unlink(missing_ok=True)


def test_crashed_thread_marked_failed(tmux_pane, fake_agent, test_db):
    """Crashed: thread marked failed, action item filed, agent record cleaned up."""
    _send(tmux_pane, f"bash {FIXTURE_DIR}/crashed.sh")
    # Poll for shell prompt — indicates the script exited and shell returned
    _wait_pane(tmux_pane, "$ ")
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
    _wait_pane(tmux_pane, "╭")  # poll until box appears
    result = inspect_agent(fake_agent["agent_id"], test_db, TEST_SESSION)
    assert result["state"] == "stuck_at_prompt"
    assert "sent_enter" in result["actions"]
    _wait_pane(tmux_pane, "Executing task...")  # poll for post-Enter response
    assert result["action_item_id"] is None
    notifs = _notifications(test_db, fake_agent["thread_id"])
    assert len(notifs) == 1
