"""Baseline: confirms detection gaps without the watchdog daemon. All cases MUST pass.

No watchdog daemon runs in these tests, so the negative ("gap NOT auto-handled")
holds by construction — nothing but the fixtures ever writes to the temp DB. We
poll the tmux pane until the gap state materializes (its marker prints) instead
of a flat ``sleep(4s)``: this proves the pane genuinely reached the stuck state
AND that no process auto-recovered it (no action item, no notification, agent
still ``busy``). Same assertion meaning, a fraction of the wall-clock — poll
~50ms up to a 5s ceiling, common case returns in tens of ms once the marker
prints, vs. 4s of dead sleep per test.
"""

import subprocess
import time
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"
POLL_INTERVAL = 0.05
# Generous ceiling for pane-settle under `-n auto` load; the common case returns
# in tens of ms as soon as the fixture script prints its marker.
POLL_TIMEOUT = 5.0


def _send(pane_id, cmd):
    subprocess.run(["tmux", "send-keys", "-t", pane_id, cmd, "Enter"], check=True)


def _capture(pane_id) -> str:
    r = subprocess.run(
        ["tmux", "capture-pane", "-pt", pane_id], capture_output=True, text=True
    )
    return r.stdout


def _wait_for(pane_id, marker, timeout=POLL_TIMEOUT, interval=POLL_INTERVAL) -> str:
    """Poll pane content until `marker` appears or timeout; return final content."""
    deadline = time.monotonic() + timeout
    content = ""
    while time.monotonic() < deadline:
        content = _capture(pane_id)
        if marker in content:
            return content
        time.sleep(interval)
    return content  # caller asserts on whatever we got


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
    content = _wait_for(tmux_pane, "working...")
    assert "working..." in content


# Each case: (fixture script, marker proving the gap state materialized). The
# param id carries the original per-test description so nothing is lost by
# folding the four near-identical GAP tests into one parametrized test.
GAP_CASES = [
    pytest.param(
        "recoverable-prompt.sh",
        "1. Yes",
        id="permission-dialog-not-auto-dismissed",
    ),
    pytest.param(
        "stalled-silent.sh",
        "Starting analysis...",
        id="silent-stall-not-detected",
    ),
    pytest.param(
        "crashed.sh",
        "Error: unexpected failure in executor",
        id="crash-not-detected",
    ),
    pytest.param(
        "stuck-at-prompt.sh",
        "payment processing",
        id="stuck-at-prompt-not-detected",
    ),
]


@pytest.mark.parametrize("script, marker", GAP_CASES)
def test_gap_not_auto_handled(script, marker, tmux_pane, fake_agent, test_db):
    """GAP: the case's stuck state is not auto-handled without the watchdog.

    Poll until the gap state prints (`marker`) — proving the pane reached the
    stuck state — then assert nothing auto-recovered it: no action item, no
    notification, agent still `busy`.
    """
    _send(tmux_pane, f"bash {FIXTURE_DIR}/{script}")
    content = _wait_for(tmux_pane, marker)
    assert marker in content, f"gap state never materialized for {script}"
    assert _action_count(test_db, fake_agent["thread_id"]) == 0
    assert _notif_count(test_db, fake_agent["thread_id"]) == 0
    assert _agent_status(test_db, fake_agent["agent_id"]) == "busy"
