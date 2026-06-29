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
from collections.abc import Callable
from pathlib import Path

import pytest

from juggle_watchdog_singleton import find_watchdog_pids
from juggle_reaper import read_proc_db_path

FIXTURE_DIR = Path(__file__).parent / "fixtures"
TEST_SESSION = "juggle-watchdog-test"

# Skips the entire module gracefully until juggle_watchdog ships.
_watchdog = pytest.importorskip(
    "juggle_watchdog", reason="juggle_watchdog not yet implemented"
)
inspect_agent = _watchdog.inspect_agent


def _scoped_leaks(before_pids: set[int], after_pids: set[int], own_db_path: str, db_path_reader: Callable[[int], str | None]) -> set[int]:
    """Return NEW pids (after - before) whose db path resolves to OUR own tmp DB.

    `own_db_path` is resolved defensively here (idempotent), so callers may pass a
    resolved or unresolved path. For each new pid we read its
    JUGGLE_DB_PATH via `db_path_reader(pid)`; None (env unreadable) or a path that
    resolves to anything else (prod, a foreign tmp DB) is EXCLUDED. Only positive
    matches to our own path are flagged — conservative, so foreign/cockpit daemons
    never count as this test's leak (the 2026-06-20 false-positive fix).
    """
    own = str(Path(own_db_path).resolve())
    leaked: set[int] = set()
    for pid in after_pids - before_pids:
        raw = db_path_reader(pid)
        if raw is None:
            continue
        if str(Path(raw).resolve()) == own:
            leaked.add(pid)
    return leaked


@pytest.fixture(autouse=True)
def assert_no_leaked_daemons(test_db):
    """REGRESSION PIN (#4713, scoped 2026-06-20): every test in this module must
    leave zero new daemon processes behind THAT TARGET ITS OWN tmp DB. A global
    PID diff falsely blamed the live cockpit's prod daemons and concurrent
    tmp-DB daemons on the test (2026-06-20 CP integrate false-positive); the
    guard is now SCOPED to daemons whose JUGGLE_DB_PATH resolves to this test's
    own tmp DB. Module-level autouse means it only covers test_watchdog_active.py.
    """
    own = str(Path(test_db.db_path).resolve())
    before = set(find_watchdog_pids())
    yield
    after = set(find_watchdog_pids())
    leaks = _scoped_leaks(before, after, own, read_proc_db_path)
    assert not leaks, (
        f"Watchdog daemon(s) leaked after test (scoped to this test's own tmp "
        f"DB {own}): PIDs {sorted(leaks)}. Every test that can spawn a daemon "
        "for its own DB MUST kill it in teardown (#4713 lineage)."
    )


def test_scoped_leaks_only_flags_own_db():
    """REGRESSION PIN: 2026-06-20 CP integrate false-positive — global PID diff
    blamed cockpit/concurrent daemons on the test. Carries forward the #4713
    leak-guard lineage: the guard must flag ONLY a daemon whose JUGGLE_DB_PATH
    resolves to THIS test's own tmp DB, never prod or a foreign tmp DB.

    Pure unit test with a fake db_path_reader (dict-backed) — no real `ps`, so
    it is host-independent and unaffected by live cockpit/concurrent daemons.
    """
    own = str(Path("/tmp/own-abc/juggle.db").resolve())
    prod_like = str((Path.home() / ".claude" / "juggle" / "juggle.db").resolve())
    reader = {
        10: "/tmp/own-abc/juggle.db",   # resolves equal to own -> OUR leak (counts)
        11: prod_like,                  # prod daemon -> NOT ours (excluded)
        12: "/tmp/other-xyz/juggle.db",  # different tmp DB -> NOT ours (excluded)
        13: None,                       # env unreadable -> excluded (conservative)
        9: "/tmp/own-abc/juggle.db",    # pre-existing (in before) -> never flagged
    }.get

    before = {9}
    after = {9, 10, 11, 12, 13}

    leaks = _scoped_leaks(before, after, own, lambda pid: reader(pid))

    # Assertion A (the 2026-06-20 incident): a NEW pid on an UNRELATED db —
    # both a prod-like path AND a different tmp path — does NOT count.
    assert 11 not in leaks  # prod daemon is not this test's leak
    assert 12 not in leaks  # foreign tmp-DB daemon is not this test's leak
    # Assertion B (intent preserved): a NEW pid on our OWN db DOES count.
    # pid 10's reader value is NON-resolved; it must be resolved before compare.
    assert leaks == {10}
    # Assertion C: a NEW pid whose db path is None is EXCLUDED — only flag
    # positive matches to our own path (conservative; mirrors prod default-None).
    assert 13 not in leaks
    # A pid present in BOTH before and after (pre-existing) is NEVER flagged
    # even though its db path == own.
    assert 9 not in leaks


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
    assert thread["state"] == "failed-exec"
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
