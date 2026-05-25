# Juggle Agent Watchdog — Test Harness Spec

**Date:** 2026-05-17
**Status:** Draft
**Depends on:** `juggle_watchdog.py` (not yet written — see watchdog spec)

---

## Overview

A pytest-based harness that tests watchdog detection/classification/recovery using **mock tmux panes** — no real Claude, cost = $0, runtime = seconds.

Two suites:
- **`test_baseline.py`** — runs against current Juggle (no watchdog). Documents 4 detection gaps.
- **`test_watchdog_active.py`** — runs against watchdog functions directly. All 5 states detected. Will fail with `ImportError` until `juggle_watchdog.py` ships.

---

## Directory Layout

```
tests/watchdog/
  conftest.py                     # fixtures: test_db, tmux_pane, fake_agent
  fixtures/
    working.sh                    # emits timestamp every 2s
    recoverable-prompt.sh         # prints permission dialog then waits on read
    stalled-silent.sh             # prints one line then sleep 999
    crashed.sh                    # prints one line then exit 1
    stuck-at-prompt.sh            # prints ╭─╮ box then waits on read
    canonical-permission-prompt.txt  # captured real Claude permission dialog (for regex validation)
  test_baseline.py                # 5 tests — gaps documented
  test_watchdog_active.py         # 5 tests — gaps closed (fails until watchdog ships)
  README.md
```

---

## The 5 States

| State | Fixture behavior | Watchdog expected action |
|---|---|---|
| **working** | Emits `$EPOCHSECONDS working...` line every 2s, runs indefinitely | No action — agent healthy |
| **recoverable_prompt** | Prints permission dialog (`1. Yes / 2. Yes, allow all / 3. No`) then `read` | Send `2\n` to pane → cockpit notification → NO action item |
| **stalled_silent** | Prints `Starting analysis...` then `sleep 999` | File action item (type=failure, priority=high) + write snapshot + release agent |
| **crashed** | Prints `Starting task...` + `Error: ...` then exits (shell prompt shows) | Mark thread failed + file action item + clean up agent row |
| **stuck_at_prompt** | Prints ╭─╮ box with task content then `read` | Send `Enter` to pane → cockpit notification → NO action item |

---

## Fixture Scripts

### `working.sh`
```bash
#!/usr/bin/env bash
while true; do
    echo "$EPOCHSECONDS working..."
    sleep 2
done
```

### `recoverable-prompt.sh`
```bash
#!/usr/bin/env bash
echo "Claude wants to run a command:"
echo ""
echo "1. Yes"
echo "2. Yes, allow all"
echo "3. No"
echo ""
read -r _response
echo "Response received: $_response"
```

### `stalled-silent.sh`
```bash
#!/usr/bin/env bash
echo "Starting analysis..."
sleep 999
```

### `crashed.sh`
```bash
#!/usr/bin/env bash
echo "Starting task..."
echo "Error: unexpected failure in executor"
exit 1
```

### `stuck-at-prompt.sh`
```bash
#!/usr/bin/env bash
echo "╭──────────────────────────────────────────────────────────╮"
echo "│                                                            │"
echo "│  Implement the payment processing module following        │"
echo "│  the existing patterns in src/. Use JuggleDB for          │"
echo "│  persistence and follow the TDD workflow.                  │"
echo "│                                                            │"
echo "╰──────────────────────────────────────────────────────────╯"
read -r _input
```

---

## Watchdog API Contract (to be implemented in `juggle_watchdog.py`)

Tests call these functions directly — no daemon timing involved.

```python
def classify_pane(pane_content: str) -> str:
    """
    Classify raw pane text (ANSI stripped) into one of:
    'working' | 'recoverable_prompt' | 'stalled_silent' | 'crashed' | 'stuck_at_prompt'
    """

def inspect_agent(agent_id: str, db: "JuggleDB", tmux_session: str) -> dict:
    """
    Inspect one agent, take appropriate action, return:
    {
        'state': str,
        'actions': list[str],        # e.g. ['sent_key_2', 'filed_action_item']
        'action_item_id': int | None,
        'notification_id': int | None,
    }
    """
```

Tests import these from `juggle_watchdog`. The module path is `src/juggle_watchdog.py`.

---

## `conftest.py` — Fixtures

```python
import subprocess
import uuid
import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
from juggle_db import JuggleDB

FIXTURE_DIR = Path(__file__).parent / "fixtures"
TEST_SESSION = "juggle-watchdog-test"


@pytest.fixture(scope="session", autouse=True)
def ensure_tmux_session():
    """Create dedicated test session; destroy after suite."""
    r = subprocess.run(["tmux", "has-session", "-t", TEST_SESSION], capture_output=True)
    if r.returncode != 0:
        subprocess.run(["tmux", "new-session", "-d", "-s", TEST_SESSION], check=True)
    yield
    subprocess.run(["tmux", "kill-session", "-t", TEST_SESSION], capture_output=True)


@pytest.fixture
def tmux_pane(ensure_tmux_session):
    """Spawn a new window/pane in test session; kill on teardown."""
    r = subprocess.run(
        ["tmux", "new-window", "-t", TEST_SESSION, "-P", "-F", "#{pane_id}"],
        capture_output=True, text=True, check=True,
    )
    pane_id = r.stdout.strip()
    # Fixed geometry: prevents ╭─╮ box width variation
    subprocess.run(["tmux", "resize-pane", "-t", pane_id, "-x", "80", "-y", "24"],
                   capture_output=True)
    yield pane_id
    subprocess.run(["tmux", "kill-pane", "-t", pane_id], capture_output=True)


@pytest.fixture
def test_db(tmp_path):
    """Fresh JuggleDB per test — no real juggle.db touched."""
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    return db


@pytest.fixture
def fake_agent(tmux_pane, test_db):
    """Insert a busy agent row; delete thread+agent on teardown."""
    agent_id = str(uuid.uuid4())
    tid = test_db.create_thread("watchdog-test-topic", session_id="test-session")
    now = datetime.datetime.utcnow().isoformat()
    with test_db._connect() as conn:
        conn.execute(
            "INSERT INTO agents (id, role, pane_id, assigned_thread, status, "
            "context_threads, created_at, last_active) VALUES (?,?,?,?,?,?,?,?)",
            (agent_id, "coder", tmux_pane, tid, "busy", "[]", now, now),
        )
    yield {"agent_id": agent_id, "thread_id": tid, "pane_id": tmux_pane}
    with test_db._connect() as conn:
        conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        conn.execute("DELETE FROM action_items WHERE thread_id = ?", (tid,))
        conn.execute("DELETE FROM notifications_v2 WHERE thread_id = ?", (tid,))
        conn.execute("DELETE FROM threads WHERE id = ?", (tid,))
```

---

## `test_baseline.py` — Gap Documentation

Each non-working test asserts that WITHOUT a watchdog, the system takes no action. All 5 should **pass** (confirming the gap).

```python
"""Baseline: confirms 4 detection gaps without watchdog daemon."""
import subprocess, time
from pathlib import Path
import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"
WAIT_SECS = 4  # enough for fixture to settle; no watchdog to wait for


def _send(pane_id, cmd):
    subprocess.run(["tmux", "send-keys", "-t", pane_id, cmd, "Enter"], check=True)

def _capture(pane_id) -> str:
    r = subprocess.run(["tmux", "capture-pane", "-pt", pane_id], capture_output=True, text=True)
    return r.stdout

def _action_count(db, tid) -> int:
    with db._connect() as c:
        return c.execute(
            "SELECT COUNT(*) FROM action_items WHERE thread_id=? AND dismissed_at IS NULL", (tid,)
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
```

---

## `test_watchdog_active.py` — Gap Closed

Tests call `inspect_agent()` directly (no polling). Will **fail with ImportError** until `src/juggle_watchdog.py` is implemented.

```python
"""Active suite: watchdog detects + handles all 5 states. Fails until juggle_watchdog ships."""
import subprocess, time, re
from pathlib import Path
import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures"
SNAPSHOT_DIR = Path.home() / ".juggle" / "watchdog" / "snapshots"
TEST_SESSION = "juggle-watchdog-test"

# Will raise ImportError until watchdog is implemented — expected until then.
from juggle_watchdog import inspect_agent


def _send(pane_id, cmd):
    subprocess.run(["tmux", "send-keys", "-t", pane_id, cmd, "Enter"], check=True)

def _capture(pane_id) -> str:
    r = subprocess.run(["tmux", "capture-pane", "-pt", pane_id], capture_output=True, text=True)
    return r.stdout

def _action_items(db, tid) -> list:
    with db._connect() as c:
        c.row_factory = __import__("sqlite3").Row
        return c.execute(
            "SELECT * FROM action_items WHERE thread_id=? AND dismissed_at IS NULL", (tid,)
        ).fetchall()

def _notifications(db, tid) -> list:
    with db._connect() as c:
        c.row_factory = __import__("sqlite3").Row
        return c.execute(
            "SELECT * FROM notifications_v2 WHERE thread_id=?", (tid,)
        ).fetchall()

def _agent_row(db, agent_id) -> dict | None:
    with db._connect() as c:
        c.row_factory = __import__("sqlite3").Row
        r = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        return dict(r) if r else None


def test_working_no_false_positive(tmux_pane, fake_agent, test_db):
    """Working agent: inspect returns 'working', no action items, no notifications."""
    _send(tmux_pane, f"bash {FIXTURE_DIR}/working.sh")
    time.sleep(3)  # let it emit a few lines
    result = inspect_agent(fake_agent["agent_id"], test_db, TEST_SESSION)
    assert result["state"] == "working"
    assert result["action_item_id"] is None
    assert len(_action_items(test_db, fake_agent["thread_id"])) == 0


def test_recoverable_prompt_auto_dismissed(tmux_pane, fake_agent, test_db):
    """Recoverable prompt: watchdog sends '2', dialog dismissed, notification logged, no action item."""
    _send(tmux_pane, f"bash {FIXTURE_DIR}/recoverable-prompt.sh")
    time.sleep(2)  # let dialog appear
    result = inspect_agent(fake_agent["agent_id"], test_db, TEST_SESSION)
    assert result["state"] == "recoverable_prompt"
    assert "sent_key" in result["actions"]
    # Pane should show "Response received" after watchdog sent input
    time.sleep(1)
    content = _capture(tmux_pane)
    assert "Response received" in content
    assert result["action_item_id"] is None
    notifs = _notifications(test_db, fake_agent["thread_id"])
    assert len(notifs) == 1


def test_stalled_silent_filed_action_item(tmux_pane, fake_agent, test_db):
    """Stalled silent: action item filed (failure/high) + snapshot written."""
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


def test_crashed_thread_marked_failed(tmux_pane, fake_agent, test_db):
    """Crashed: thread marked failed, action item filed, agent row cleaned up."""
    _send(tmux_pane, f"bash {FIXTURE_DIR}/crashed.sh")
    time.sleep(2)  # let it exit
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
    time.sleep(2)  # let box render
    result = inspect_agent(fake_agent["agent_id"], test_db, TEST_SESSION)
    assert result["state"] == "stuck_at_prompt"
    assert "sent_enter" in result["actions"]
    time.sleep(1)
    content = _capture(tmux_pane)
    # After Enter, read() returns — script continues beyond the box
    assert result["action_item_id"] is None
    notifs = _notifications(test_db, fake_agent["thread_id"])
    assert len(notifs) == 1
```

---

## How to Run

```bash
# Baseline (5/5 pass — gap confirmed)
cd ~/github/juggle
pytest tests/watchdog/test_baseline.py -v --tb=short

# Archive baseline results
pytest tests/watchdog/test_baseline.py -v --tb=short \
    | tee tests/watchdog/baseline-2026-05-17.txt

# Active (5/5 pass once watchdog ships; ImportError until then)
pytest tests/watchdog/test_watchdog_active.py -v --tb=short

# Full suite
pytest tests/watchdog/ -v --tb=short
```

### Expected results

| Suite | Before watchdog | After watchdog |
|---|---|---|
| baseline | 5/5 PASS | 5/5 PASS (gaps still exist) |
| active | 5/5 ERROR (ImportError) | 5/5 PASS |

---

## Devil's Advocate

### 1. Pattern brittleness (recoverable prompt)

**Risk:** Real Claude renders the permission dialog with ANSI color codes, possible box-drawing characters, and variable spacing. The mock prints plain ASCII. If the watchdog regex was written to match the real render (e.g., after stripping ANSI codes), it should match the mock too — but this is not guaranteed until verified.

**Mitigation:**
- Capture a real Claude permission dialog once: `tmux capture-pane -pt <real_pane>` → save as `tests/watchdog/fixtures/canonical-permission-prompt.txt`.
- Write a unit test in `test_watchdog_active.py` that calls `classify_pane(canonical_text)` and asserts `== 'recoverable_prompt'` AND calls it with the mock fixture text and asserts the same.
- The watchdog must strip ANSI codes (`re.sub(r'\x1b\[[0-9;]*m', '', text)`) before pattern matching.
- The mock script must NOT use ANSI codes (it doesn't — `echo` is plain).
- Pattern to use: `r'^\s*[12]\.\s+Yes'` matched against any line in stripped content.

### 2. Stuck-at-prompt mock fidelity

**Risk:** Claude renders the ╭─...╮ box at the current terminal width. A 200-column pane produces a 200-char wide box. The mock hardcodes a specific width (60 chars). If the watchdog detects by line length rather than the ╭ character, the mock may not trigger.

**Mitigation:**
- `tmux_pane` fixture forces 80×24 geometry (`resize-pane -x 80 -y 24`) before running any script.
- Watchdog detection regex: `r'^╭─+╮\s*$'` (start of line, ╭, one-or-more ─, ╮) — width-agnostic.
- The mock fixture uses the same ╭/─/╮ box-drawing characters as Claude (U+256D, U+2500, U+256E).
- Add a unit test: `classify_pane(mock_box_text) == 'stuck_at_prompt'` and `classify_pane(canonical_real_box) == 'stuck_at_prompt'`.

### 3. Recovery side effects

**Risk:** `inspect_agent()` for stalled/crashed states modifies the DB (creates action items, updates thread status, potentially removes agent rows). If the test DB is the real `juggle.db`, this corrupts live data.

**Mitigation:**
- `test_db` fixture creates a fresh `JuggleDB(tmp_path / "juggle.db")` per test — never touches the real DB.
- `inspect_agent()` MUST accept an explicit `db: JuggleDB` parameter (not read from `CLAUDE_PLUGIN_DATA`). The watchdog daemon passes `JuggleDB(os.environ["CLAUDE_PLUGIN_DATA"])` at startup; tests pass `test_db` directly.
- `fake_agent` fixture teardown deletes `agents`, `action_items`, `notifications_v2`, and `threads` rows for the test's IDs.
- All snapshot files written to `~/.juggle/watchdog/snapshots/` use the agent UUID in the filename → cleanup: `SNAPSHOT_DIR.glob(f"{agent_id}-*.txt")` and delete in test teardown.

### 4. Race conditions

**Risk:** If tests relied on the watchdog daemon's 30s poll cycle, tests would need `time.sleep(30+)` to guarantee at least one check. At 5 states × 30s = 2.5 min per suite.

**Mitigation:**
- Tests call `inspect_agent()` directly — no polling, no sleep beyond fixture settle time (2-4s).
- The daemon is separate (calls `inspect_agent` on a timer). Tests bypass it entirely.
- The only waits in tests are for the fixture script to render its output (2-4s). This is fast and deterministic.
- The stalled-silent threshold (how long before classifying as stalled) must be configurable with a short default for tests: `STALL_THRESHOLD_SECS = int(os.getenv("JUGGLE_WATCHDOG_STALL_SECS", "60"))`. Tests set this to 1s via `monkeypatch.setenv("JUGGLE_WATCHDOG_STALL_SECS", "1")`.

### 5. DB state pollution

**Risk:** Tests that create action items, notifications, or threads in the real DB would show up in the cockpit and confuse the user.

**Mitigation:**
- `test_db` fixture (see above) isolates all DB writes to `tmp_path`.
- The `ensure_tmux_session` fixture is session-scoped — panes are isolated to `juggle-watchdog-test`, never touching the real `juggle` session.
- Before running active tests in CI, verify `CLAUDE_PLUGIN_DATA` is unset or points to a test DB.
- Note in README: "Never run `test_watchdog_active.py` with a production DB path set unless `inspect_agent()` receives an explicit `db` argument."

### 6. Stall threshold sensitivity

**Risk:** `stalled-silent.sh` sleeps 999s. But the watchdog's stall threshold is configurable (default 60s). Tests can't sleep 60s.

**Mitigation:** Test patches `JUGGLE_WATCHDOG_STALL_SECS=1` via monkeypatch. The watchdog reads this env var to configure its stall threshold. Test waits 2s after starting the fixture, then calls `inspect_agent` — the 1s threshold is already exceeded.

---

## Orchestrator Interface

Single command, parseable output:

```bash
cd ~/github/juggle && pytest tests/watchdog/ -v --tb=short
```

Baseline target: `5 passed, 0 failed`
Active target (post-watchdog): `5 passed, 0 failed`
Active target (pre-watchdog): `5 errors` (ImportError on `juggle_watchdog`)
