# Juggle Agent Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a background daemon that detects stalled/crashed Juggle agents (via tmux pane snapshot diffs), auto-resolves permission prompts, and re-dispatches agents that go silent past an adaptive threshold.

**Architecture:** A new Python module `src/juggle_watchdog.py` holds all pure logic (classifier, threshold, snapshot helpers, recovery). A thin daemon script `scripts/juggle-agent-watchdog` runs the 30-second poll loop. `cmd_start` launches it as a background process (PID file at `~/.juggle/watchdog.pid`); `cmd_stop` sends SIGTERM. New DB schema tracks `busy_since`, `model`, `last_task`, `watchdog_retried` on `agents`, plus an `agent_completions` table for adaptive threshold calculation.

**Tech Stack:** Python 3.11+, SQLite (via existing `JuggleDB`), tmux subprocess, pytest. All code in `~/github/juggle/`. Commit directly to `main`.

**Spec:** `docs/superpowers/specs/2026-05-17-juggle-agent-watchdog.md` — read it before implementing.

---

## File Map

| Action | File | Purpose |
|---|---|---|
| Modify | `src/juggle_db.py` | Schema migrations 20–23; new DB methods |
| Modify | `src/juggle_cmd_agents.py` | Track busy_since/model on get-agent; last_task on send-task; completion on complete-agent; add cmd_set_watchdog |
| Modify | `src/juggle_cmd_threads.py` | cmd_start launches watchdog daemon; cmd_stop kills it |
| Modify | `src/juggle_cli.py` | Wire set-watchdog subcommand |
| **Create** | `src/juggle_watchdog.py` | All pure watchdog logic: snapshot helpers, state classifier, threshold, recovery |
| **Create** | `scripts/juggle-agent-watchdog` | Thin daemon entry point: poll loop + signal handler |
| **Create** | `tests/test_watchdog.py` | Unit tests for juggle_watchdog.py |
| **Create** | `tests/test_db_watchdog.py` | Tests for new DB schema + methods |

---

## Task 1: Schema migrations (juggle_db.py)

Highest existing migration is **19**. We add migrations 20, 21, 22.

**Files:**
- Modify: `src/juggle_db.py`
- Modify (add): the `CREATE_AGENTS` string, `CREATE_AGENT_COMPLETIONS`, `CREATE_WATCHDOG_EVENTS` constants
- Test: `tests/test_db_watchdog.py`

- [ ] **Step 1: Write failing tests for new schema**

Create `tests/test_db_watchdog.py`:

```python
"""Tests for watchdog-related DB schema and methods."""
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


def _col_names(db, table):
    with db._connect() as conn:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def test_agents_has_watchdog_retried(db):
    assert "watchdog_retried" in _col_names(db, "agents")


def test_agents_has_watchdog_threshold_minutes(db):
    assert "watchdog_threshold_minutes" in _col_names(db, "agents")


def test_agents_has_model(db):
    assert "model" in _col_names(db, "agents")


def test_agents_has_last_task(db):
    assert "last_task" in _col_names(db, "agents")


def test_agents_has_busy_since(db):
    assert "busy_since" in _col_names(db, "agents")


def test_agent_completions_table_exists(db):
    with db._connect() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert "agent_completions" in tables


def test_watchdog_events_table_exists(db):
    with db._connect() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert "watchdog_events" in tables


def test_insert_agent_completion(db):
    db.insert_agent_completion(role="coder", duration_secs=120.5)
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM agent_completions").fetchone()
    assert row["role"] == "coder"
    assert abs(row["duration_secs"] - 120.5) < 0.01


def test_get_median_coldstart(db):
    # < 10 samples → None
    db.insert_agent_completion(role="coder", duration_secs=100.0)
    assert db.get_median_duration_secs("coder") is None


def test_get_median_adaptive(db):
    for i in range(10):
        db.insert_agent_completion(role="coder", duration_secs=float(100 + i * 10))
    # values: 100,110,120,...190 → median of 10 values = (140+150)/2 = 145
    median = db.get_median_duration_secs("coder")
    assert median is not None
    assert abs(median - 145.0) < 0.01


def test_add_watchdog_event(db):
    db.add_watchdog_event(
        agent_id="test-agent-id",
        thread_id=None,
        event_type="stalled",
        snapshot_path="/tmp/snap.txt",
    )
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM watchdog_events").fetchone()
    assert row["agent_id"] == "test-agent-id"
    assert row["event_type"] == "stalled"
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd ~/github/juggle && python -m pytest tests/test_db_watchdog.py -v 2>&1 | head -40
```

Expected: many failures (columns/tables/methods don't exist yet).

- [ ] **Step 3: Add table DDL constants at top of juggle_db.py**

After `CREATE_SETTINGS` (line ~106), add:

```python
CREATE_AGENT_COMPLETIONS = """
CREATE TABLE IF NOT EXISTS agent_completions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  role          TEXT NOT NULL,
  duration_secs REAL NOT NULL,
  completed_at  TEXT NOT NULL
);
"""

CREATE_WATCHDOG_EVENTS = """
CREATE TABLE IF NOT EXISTS watchdog_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id      TEXT NOT NULL,
  thread_id     TEXT,
  event_type    TEXT NOT NULL,
  snapshot_path TEXT,
  created_at    TEXT NOT NULL
);
"""
```

- [ ] **Step 4: Add migrations 20–22 in `init_db()` in juggle_db.py**

In the `init_db` method, after the last existing migration block (migration 19), add:

```python
        # Migration 20: watchdog columns on agents
        agents_cols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
        try:
            if "watchdog_retried" not in agents_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN watchdog_retried INTEGER NOT NULL DEFAULT 0")
            if "watchdog_threshold_minutes" not in agents_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN watchdog_threshold_minutes INTEGER")
            if "model" not in agents_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN model TEXT")
            if "last_task" not in agents_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN last_task TEXT")
            if "busy_since" not in agents_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN busy_since TEXT")
            conn.commit()
            _log.info("Migration 20: watchdog columns added to agents")
        except Exception as e:
            _log.warning("Migration 20 (watchdog agent cols) skipped: %s", e)

        # Migration 21: agent_completions table
        try:
            conn.execute(CREATE_AGENT_COMPLETIONS)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_agent_completions_role_date "
                "ON agent_completions(role, completed_at)"
            )
            conn.commit()
            _log.info("Migration 21: agent_completions table created")
        except Exception as e:
            _log.warning("Migration 21 (agent_completions) skipped: %s", e)

        # Migration 22: watchdog_events table
        try:
            conn.execute(CREATE_WATCHDOG_EVENTS)
            conn.commit()
            _log.info("Migration 22: watchdog_events table created")
        except Exception as e:
            _log.warning("Migration 22 (watchdog_events) skipped: %s", e)
```

Also update `CREATE_AGENTS` constant to include the new columns (so fresh DBs get them without migrations):

```python
CREATE_AGENTS = """
CREATE TABLE IF NOT EXISTS agents (
  id                         TEXT PRIMARY KEY,
  role                       TEXT NOT NULL,
  pane_id                    TEXT NOT NULL,
  assigned_thread            TEXT,
  status                     TEXT NOT NULL DEFAULT 'idle',
  context_threads            TEXT NOT NULL DEFAULT '[]',
  created_at                 TEXT NOT NULL,
  last_active                TEXT NOT NULL,
  watchdog_retried           INTEGER NOT NULL DEFAULT 0,
  watchdog_threshold_minutes INTEGER,
  model                      TEXT,
  last_task                  TEXT,
  busy_since                 TEXT
);
"""
```

- [ ] **Step 5: Add new DB methods to juggle_db.py**

Add to the `JuggleDB` class after the existing agent methods (after `get_agent_by_thread`):

```python
    def insert_agent_completion(self, role: str, duration_secs: float) -> None:
        """Record a completed agent's duration for adaptive threshold calculation."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_completions (role, duration_secs, completed_at) VALUES (?, ?, ?)",
                (role, duration_secs, now),
            )
            conn.commit()

    def get_median_duration_secs(
        self, role: str, days: int = 30, min_samples: int = 10
    ) -> float | None:
        """Return median completion duration for the role, or None if < min_samples."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT duration_secs FROM agent_completions "
                "WHERE role = ? AND completed_at > ? ORDER BY duration_secs",
                (role, cutoff),
            ).fetchall()
        vals = [r["duration_secs"] for r in rows]
        if len(vals) < min_samples:
            return None
        mid = len(vals) // 2
        if len(vals) % 2 == 0:
            return (vals[mid - 1] + vals[mid]) / 2.0
        return vals[mid]

    def add_watchdog_event(
        self,
        agent_id: str,
        thread_id: str | None,
        event_type: str,
        snapshot_path: str | None = None,
    ) -> None:
        """Record a watchdog event for telemetry."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO watchdog_events (agent_id, thread_id, event_type, snapshot_path, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent_id, thread_id, event_type, snapshot_path, now),
            )
            conn.commit()

    def cleanup_watchdog_events(self, days: int = 30) -> int:
        """Delete watchdog_events older than `days`. Returns deleted count."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM watchdog_events WHERE created_at < ?", (cutoff,)
            )
            conn.commit()
        return cur.rowcount
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd ~/github/juggle && python -m pytest tests/test_db_watchdog.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
cd ~/github/juggle
git add src/juggle_db.py tests/test_db_watchdog.py
git commit -m "feat(watchdog): schema migrations 20-22 — watchdog cols, agent_completions, watchdog_events"
```

---

## Task 2: Agent metadata tracking (get-agent, send-task, complete-agent)

**Files:**
- Modify: `src/juggle_cmd_agents.py`
- Test: `tests/test_db_watchdog.py` (extend)

The three CLI commands need small additions: `get-agent` writes `busy_since` + `model`, `send-task` writes `last_task`, `complete-agent` inserts into `agent_completions`.

- [ ] **Step 1: Write failing tests for metadata tracking**

Add to `tests/test_db_watchdog.py`:

```python
def test_get_agent_sets_busy_since(tmp_path, monkeypatch):
    """get-agent sets busy_since and model on the assigned agent."""
    import os, subprocess, sys
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("JUGGLE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JUGGLE_TMUX_MOCK_PANE", "%5")

    d = JuggleDB(db_path)
    d.init_db()
    thread_id = d.create_thread("test topic", session_id="")
    agent_id_raw = d.create_agent(role="coder", pane_id="%5")
    d.update_agent(agent_id_raw, status="idle")

    # Run get-agent via CLI
    result = subprocess.run(
        [sys.executable, "src/juggle_cli.py", "get-agent", thread_id,
         "--role", "coder", "--model", "claude-sonnet-4-6"],
        cwd=str(Path(__file__).parent.parent),
        capture_output=True, text=True,
        env={**os.environ, "JUGGLE_DATA_DIR": str(tmp_path), "JUGGLE_TMUX_MOCK_PANE": "%5"},
    )
    agent_id = result.stdout.strip().split()[0]
    agent = d.get_agent(agent_id)
    assert agent["busy_since"] is not None
    assert agent["model"] == "claude-sonnet-4-6"


def test_send_task_stores_last_task(tmp_path, monkeypatch):
    """send-task writes prompt content to agents.last_task."""
    import os, subprocess, sys
    db_path = str(tmp_path / "test.db")
    task_file = tmp_path / "task.txt"
    task_file.write_text("do something useful")

    d = JuggleDB(db_path)
    d.init_db()
    agent_id = d.create_agent(role="coder", pane_id="%5")
    d.update_agent(agent_id, status="busy")

    result = subprocess.run(
        [sys.executable, "src/juggle_cli.py", "send-task", agent_id, str(task_file)],
        cwd=str(Path(__file__).parent.parent),
        capture_output=True, text=True,
        env={**os.environ, "JUGGLE_DATA_DIR": str(tmp_path),
             "JUGGLE_TMUX_MOCK_SEND": "1", "JUGGLE_TMUX_MOCK_PANE": "%5"},
    )
    agent = d.get_agent(agent_id)
    assert agent["last_task"] == "do something useful"


def test_complete_agent_inserts_completion(tmp_path, monkeypatch):
    """complete-agent inserts a row into agent_completions when busy_since is set."""
    import os, subprocess, sys
    from datetime import datetime, timezone, timedelta
    db_path = str(tmp_path / "test.db")

    d = JuggleDB(db_path)
    d.init_db()
    thread_id = d.create_thread("test topic", session_id="")
    agent_id = d.create_agent(role="coder", pane_id="%5")
    busy_since = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    d.update_agent(agent_id, status="busy", assigned_thread=thread_id, busy_since=busy_since)

    subprocess.run(
        [sys.executable, "src/juggle_cli.py", "complete-agent", thread_id, "Done. All good."],
        cwd=str(Path(__file__).parent.parent),
        capture_output=True, text=True,
        env={**os.environ, "JUGGLE_DATA_DIR": str(tmp_path)},
    )
    rows = []
    with d._connect() as conn:
        rows = conn.execute("SELECT * FROM agent_completions").fetchall()
    assert len(rows) == 1
    assert rows[0]["role"] == "coder"
    assert rows[0]["duration_secs"] >= 100  # ≥ 2 min (120s ± margin)
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd ~/github/juggle && python -m pytest tests/test_db_watchdog.py::test_get_agent_sets_busy_since tests/test_db_watchdog.py::test_send_task_stores_last_task tests/test_db_watchdog.py::test_complete_agent_inserts_completion -v
```

Expected: 3 failures.

- [ ] **Step 3: Update `cmd_get_agent` in juggle_cmd_agents.py**

Find the block (around line 450):
```python
    now = datetime.now(timezone.utc).isoformat()
    db.update_agent(agent["id"], status="busy", assigned_thread=thread_uuid,
                    last_active=now)
```

Replace with:
```python
    now = datetime.now(timezone.utc).isoformat()
    _update_kw: dict = dict(status="busy", assigned_thread=thread_uuid,
                            last_active=now, busy_since=now)
    _model_arg = getattr(args, "model", None)
    if _model_arg:
        _update_kw["model"] = _model_arg
    db.update_agent(agent["id"], **_update_kw)
```

- [ ] **Step 4: Update `cmd_send_task` in juggle_cmd_agents.py**

Find this block (around line 574):
```python
    now = datetime.now(timezone.utc).isoformat()
    db.update_agent(args.agent_id, last_active=now)
    mgr.send_task(pane_id, full_prompt, is_new=is_new)
    print(f"Task sent to agent {args.agent_id[:8]} (pane {pane_id}).")
```

Replace with:
```python
    now = datetime.now(timezone.utc).isoformat()
    db.update_agent(args.agent_id, last_active=now)
    mgr.send_task(pane_id, full_prompt, is_new=is_new)
    db.update_agent(args.agent_id, last_task=full_prompt)
    print(f"Task sent to agent {args.agent_id[:8]} (pane {pane_id}).")
```

- [ ] **Step 5: Update `cmd_complete_agent` in juggle_cmd_agents.py**

Find the block that does `db.get_agent_by_thread` (around line 114):
```python
    agent = db.get_agent_by_thread(thread_uuid)
    if agent:
        db.update_agent(agent["id"], status="idle", assigned_thread=None)
```

Replace with:
```python
    agent = db.get_agent_by_thread(thread_uuid)
    if agent:
        # Record completion duration for adaptive threshold
        busy_since = agent.get("busy_since")
        if busy_since:
            try:
                busy_dt = datetime.fromisoformat(busy_since.replace("Z", "+00:00"))
                if busy_dt.tzinfo is None:
                    busy_dt = busy_dt.replace(tzinfo=timezone.utc)
                duration = (datetime.now(timezone.utc) - busy_dt).total_seconds()
                db.insert_agent_completion(role=agent["role"], duration_secs=duration)
            except (ValueError, TypeError):
                pass
        db.update_agent(agent["id"], status="idle", assigned_thread=None)
```

(`datetime` and `timezone` are already imported at the top of `juggle_cmd_agents.py`.)

- [ ] **Step 6: Run tests to verify they pass**

```bash
cd ~/github/juggle && python -m pytest tests/test_db_watchdog.py -v
```

Expected: all tests pass (including the 3 new ones).

- [ ] **Step 7: Commit**

```bash
cd ~/github/juggle
git add src/juggle_cmd_agents.py tests/test_db_watchdog.py
git commit -m "feat(watchdog): track busy_since/model/last_task on agents, insert agent_completions on complete"
```

---

## Task 3: Core watchdog module — snapshot helpers and state classifier

**Files:**
- Create: `src/juggle_watchdog.py`
- Test: `tests/test_watchdog.py`

This module holds all pure, testable functions. The daemon script will import from it.

- [ ] **Step 1: Write failing tests**

Create `tests/test_watchdog.py`:

```python
"""Tests for juggle_watchdog pure functions."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def test_classify_working():
    from juggle_watchdog import classify_pane_state
    state, key = classify_pane_state(
        content="new output line\nstill running",
        prev_content="old output",
        stalled_for=0.0,
        threshold=60.0,
    )
    assert state == "working"
    assert key is None


def test_classify_crashed_pane_gone():
    from juggle_watchdog import classify_pane_state
    state, key = classify_pane_state(
        content=None,
        prev_content="some previous",
        stalled_for=0.0,
        threshold=60.0,
    )
    assert state == "crashed"


def test_classify_crashed_shell_prompt():
    from juggle_watchdog import classify_pane_state
    state, key = classify_pane_state(
        content="some output\nmikechen@host:~$ ",
        prev_content="some output\nmikechen@host:~$ ",
        stalled_for=200.0,
        threshold=60.0,
    )
    assert state == "crashed"


def test_classify_prompt_permission():
    from juggle_watchdog import classify_pane_state
    content = "Claude wants to run a command\n1. Yes / 2. Yes, allow always / 3. No"
    state, key = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=300.0,
        threshold=60.0,
    )
    assert state == "prompt"
    assert key == "2"


def test_classify_prompt_plan_mode():
    from juggle_watchdog import classify_pane_state
    content = "Review the plan\n1. Yes, auto-accept / 2. Yes, manually approve / 3. No"
    state, key = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=300.0,
        threshold=60.0,
    )
    assert state == "prompt"
    assert key == "2"


def test_classify_prompt_press_enter():
    from juggle_watchdog import classify_pane_state
    content = "long output\nPress Enter to continue"
    state, key = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=300.0,
        threshold=60.0,
    )
    assert state == "prompt"
    assert key == ""


def test_classify_quiet_thinking():
    from juggle_watchdog import classify_pane_state
    content = "doing stuff\nThinking…"
    state, key = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=300.0,
        threshold=60.0,
    )
    assert state == "quiet"


def test_classify_quiet_within_threshold():
    from juggle_watchdog import classify_pane_state
    state, key = classify_pane_state(
        content="unchanged",
        prev_content="unchanged",
        stalled_for=30.0,
        threshold=120.0,
    )
    assert state == "quiet"


def test_classify_stalled():
    from juggle_watchdog import classify_pane_state
    state, key = classify_pane_state(
        content="unchanged",
        prev_content="unchanged",
        stalled_for=400.0,
        threshold=120.0,
    )
    assert state == "stalled"


def test_get_threshold_disabled():
    from juggle_watchdog import get_threshold_seconds
    db = MagicMock()
    agent = {"watchdog_threshold_minutes": -1, "role": "coder"}
    assert get_threshold_seconds(db, agent) == float("inf")


def test_get_threshold_override():
    from juggle_watchdog import get_threshold_seconds
    db = MagicMock()
    agent = {"watchdog_threshold_minutes": 10, "role": "coder"}
    assert get_threshold_seconds(db, agent) == 600.0


def test_get_threshold_coldstart():
    from juggle_watchdog import get_threshold_seconds
    db = MagicMock()
    db.get_median_duration_secs.return_value = None
    agent = {"watchdog_threshold_minutes": None, "role": "coder"}
    assert get_threshold_seconds(db, agent) == 300.0  # 5 min cold-start default


def test_get_threshold_coldstart_planner():
    from juggle_watchdog import get_threshold_seconds
    db = MagicMock()
    db.get_median_duration_secs.return_value = None
    agent = {"watchdog_threshold_minutes": None, "role": "planner"}
    assert get_threshold_seconds(db, agent) == 180.0  # 3 min


def test_get_threshold_adaptive():
    from juggle_watchdog import get_threshold_seconds
    db = MagicMock()
    db.get_median_duration_secs.return_value = 90.0  # 1.5 min median
    agent = {"watchdog_threshold_minutes": None, "role": "coder"}
    assert get_threshold_seconds(db, agent) == 180.0  # 2 × 90


def test_snapshot_roundtrip(tmp_path):
    from juggle_watchdog import read_snapshot, write_snapshot
    write_snapshot("agent-123", "hello world", snapshot_dir=tmp_path)
    result = read_snapshot("agent-123", snapshot_dir=tmp_path)
    assert result == "hello world"


def test_read_snapshot_missing(tmp_path):
    from juggle_watchdog import read_snapshot
    assert read_snapshot("no-such-agent", snapshot_dir=tmp_path) is None
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd ~/github/juggle && python -m pytest tests/test_watchdog.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'juggle_watchdog'`.

- [ ] **Step 3: Create `src/juggle_watchdog.py` with snapshot helpers and classifier**

```python
"""Juggle agent watchdog — pure functions for the watchdog daemon.

Designed for testability: all I/O is injected (snapshot_dir, db). The daemon
script (scripts/juggle-agent-watchdog) runs the poll loop and imports from here.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Allowlist: (pane-tail substring, key to send). Empty key → Enter only.
# ---------------------------------------------------------------------------
_ALLOWLIST: list[tuple[str, str]] = [
    ("1. Yes / 2. Yes, allow always / 3. No", "2"),
    ("1. Yes, auto-accept / 2. Yes, manually approve / 3. No", "2"),
    ("Press Enter to continue", ""),
]

_SHELL_SUFFIXES = ("$ ", "% ", "> ")
_COLD_START_DEFAULTS: dict[str, float] = {
    "coder": 300.0,
    "planner": 180.0,
    "researcher": 120.0,
}


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------

def read_snapshot(agent_id: str, snapshot_dir: Path) -> str | None:
    path = snapshot_dir / f"{agent_id}.txt"
    return path.read_text() if path.exists() else None


def write_snapshot(agent_id: str, content: str, snapshot_dir: Path) -> None:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / f"{agent_id}.txt").write_text(content)


def write_recovery_snapshot(agent_id: str, content: str, recovery_dir: Path) -> Path:
    """Write a 500-line recovery snapshot; prune to last 100 total across all agents."""
    recovery_dir.mkdir(parents=True, exist_ok=True)
    import time
    ts = int(time.time())
    path = recovery_dir / f"{agent_id}-{ts}.txt"
    path.write_text(content)
    all_snaps = sorted(recovery_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime)
    for old in all_snaps[:-100]:
        try:
            old.unlink()
        except FileNotFoundError:
            pass
    return path


# ---------------------------------------------------------------------------
# State classifier
# ---------------------------------------------------------------------------

def classify_pane_state(
    content: str | None,
    prev_content: str | None,
    stalled_for: float,
    threshold: float,
) -> tuple[str, str | None]:
    """Classify agent pane state.

    Returns (state, key_to_send):
      - ("working", None)
      - ("crashed", None)      — pane gone or bare shell prompt
      - ("prompt", key)        — allowlist match; key is text to type before Enter
      - ("quiet", None)        — unchanged but not yet stalled
      - ("stalled", None)      — unchanged past threshold

    `stalled_for`: seconds since content last changed.
    `threshold`: seconds before classifying as stalled.
    `content=None` means the pane no longer exists.
    """
    if content is None:
        return "crashed", None

    tail = "\n".join(content.splitlines()[-15:])

    # Allowlist check (before crash/stall — prompts look like stalls)
    for pattern, key in _ALLOWLIST:
        if pattern in tail:
            return "prompt", key

    # Bare shell prompt → crashed
    last_nonempty = next(
        (line for line in reversed(content.splitlines()) if line.strip()), ""
    )
    if any(last_nonempty.endswith(suffix) for suffix in _SHELL_SUFFIXES):
        return "crashed", None

    # Content changed → working
    if content != prev_content:
        return "working", None

    # Thinking grace or within 60-second minimum window
    if "Thinking" in tail or stalled_for < 60:
        return "quiet", None

    # Past threshold → stalled; within → quiet
    if stalled_for >= threshold:
        return "stalled", None
    return "quiet", None


# ---------------------------------------------------------------------------
# Threshold
# ---------------------------------------------------------------------------

def get_threshold_seconds(db: Any, agent: dict) -> float:
    """Return the stall threshold in seconds for this agent.

    Priority: per-agent override → adaptive (2× median) → cold-start default.
    Returns float('inf') if watchdog is disabled for this agent (-1).
    """
    override = agent.get("watchdog_threshold_minutes")
    if override is not None:
        if override == -1:
            return float("inf")
        if override > 0:
            return float(override) * 60.0

    role = agent.get("role", "researcher")
    median = db.get_median_duration_secs(role)
    if median is not None:
        return 2.0 * median

    return _COLD_START_DEFAULTS.get(role, 180.0)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/github/juggle && python -m pytest tests/test_watchdog.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd ~/github/juggle
git add src/juggle_watchdog.py tests/test_watchdog.py
git commit -m "feat(watchdog): core module — snapshot helpers, state classifier, threshold logic"
```

---

## Task 4: Recovery logic in juggle_watchdog.py

**Files:**
- Modify: `src/juggle_watchdog.py` (add `execute_recovery`, `handle_prompt`, `get_session_id`)
- Modify: `tests/test_watchdog.py` (add recovery tests)

- [ ] **Step 1: Write failing tests for recovery**

Add to `tests/test_watchdog.py`:

```python
def test_execute_recovery_no_last_task(tmp_path):
    """Recovery aborts and files action item when last_task is None."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from juggle_watchdog import execute_recovery
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id,
                    last_task=None, watchdog_retried=0)

    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    recovery_dir = tmp_path / "recovery"

    execute_recovery(db, mgr, db.get_agent(agent_id), "pane content",
                     recovery_dir=recovery_dir, session_id="")

    items = db.get_open_action_items()
    assert any("no task content" in it["message"] for it in items)
    # Agent should be decommissioned
    assert db.get_agent(agent_id) is None


def test_execute_recovery_second_stall_blocked(tmp_path):
    """Recovery does not re-dispatch if watchdog_retried == 1."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from juggle_watchdog import execute_recovery
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id,
                    last_task="do work", watchdog_retried=1)

    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    recovery_dir = tmp_path / "recovery"

    execute_recovery(db, mgr, db.get_agent(agent_id), "pane content",
                     recovery_dir=recovery_dir, session_id="")

    items = db.get_open_action_items()
    assert any("stalled AGAIN" in it["message"] for it in items)
    # Must NOT spawn a new agent (spawn_agent not called)
    mgr.spawn_agent.assert_not_called()


def test_execute_recovery_full_flow(tmp_path):
    """Successful recovery: decommissions old agent, spawns new, re-sends task."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from juggle_watchdog import execute_recovery
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id,
                    last_task="do the work", watchdog_retried=0, model="claude-sonnet-4-6")

    # New agent returned by spawn_agent mock
    new_agent_id = db.create_agent(role="coder", pane_id="%6")
    new_agent = db.get_agent(new_agent_id)

    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    mgr.spawn_agent.return_value = new_agent
    recovery_dir = tmp_path / "recovery"

    execute_recovery(db, mgr, db.get_agent(agent_id), "pane content",
                     recovery_dir=recovery_dir, session_id="")

    # Old agent gone
    assert db.get_agent(agent_id) is None

    # Thread back to background
    thread = db.get_thread(thread_id)
    assert thread["status"] == "background"

    # New agent is busy and has watchdog_retried=1
    updated_new = db.get_agent(new_agent_id)
    assert updated_new["watchdog_retried"] == 1
    assert updated_new["status"] == "busy"

    # Task was sent
    mgr.send_task.assert_called_once_with("%6", "do the work")

    # Action items: one high-priority stall item + one normal re-dispatch item
    items = db.get_open_action_items()
    priorities = {it["priority"] for it in items}
    assert "high" in priorities
    assert any("auto-re-dispatched" in it["message"] for it in items)
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd ~/github/juggle && python -m pytest tests/test_watchdog.py::test_execute_recovery_no_last_task tests/test_watchdog.py::test_execute_recovery_second_stall_blocked tests/test_watchdog.py::test_execute_recovery_full_flow -v
```

Expected: 3 failures (`execute_recovery` not found).

- [ ] **Step 3: Add helper functions to `src/juggle_watchdog.py`**

Add after the `get_threshold_seconds` function:

```python
# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------

def get_session_id(db: Any) -> str:
    with db._connect() as conn:
        row = conn.execute("SELECT value FROM session WHERE key='session_id'").fetchone()
    return row["value"] if row else ""


def _get_thread_label(db: Any, thread_id: str) -> str:
    thread = db.get_thread(thread_id)
    if not thread:
        return thread_id[:8]
    return thread.get("user_label") or thread.get("label") or thread_id[:8]


# ---------------------------------------------------------------------------
# Prompt auto-resolution
# ---------------------------------------------------------------------------

def handle_prompt(db: Any, mgr: Any, agent: dict, pane_id: str, key: str) -> None:
    """Auto-send safe key for a known permission prompt pattern."""
    if key:
        mgr._run_tmux("send-keys", "-t", pane_id, key, "Enter")
    else:
        mgr._run_tmux("send-keys", "-t", pane_id, "Enter")
    thread_id = agent.get("assigned_thread")
    label = _get_thread_label(db, thread_id) if thread_id else agent["id"][:8]
    session_id = get_session_id(db)
    db.add_notification_v2(
        thread_id=thread_id,
        message=f"[Watchdog] [{label}] auto-resolved permission prompt (key={key!r})",
        session_id=session_id,
    )
    _log.info("Watchdog: prompt resolved for agent %s pane %s key=%r", agent["id"][:8], pane_id, key)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def execute_recovery(
    db: Any,
    mgr: Any,
    agent: dict,
    pane_content: str,
    *,
    recovery_dir: Path,
    session_id: str,
) -> None:
    """Decommission a stalled/crashed agent and (if eligible) re-dispatch it.

    Steps:
    1. Save recovery snapshot.
    2. Decommission stuck agent (kill pane + delete from DB).
    3. Mark thread failed.
    4. If watchdog_retried == 1: file high-priority action item, stop.
    5. If last_task is None: file action item asking for manual re-dispatch, stop.
    6. Spawn new agent, assign to thread, re-send task, set watchdog_retried=1.
    7. File action items.
    """
    agent_id = agent["id"]
    pane_id = agent["pane_id"]
    thread_id = agent.get("assigned_thread")
    role = agent.get("role", "researcher")
    model = agent.get("model")
    last_task = agent.get("last_task")
    label = _get_thread_label(db, thread_id) if thread_id else agent_id[:8]

    # 1. Save recovery snapshot
    snap_path = write_recovery_snapshot(agent_id, pane_content, recovery_dir)
    _log.info("Watchdog: recovery snapshot saved to %s", snap_path)

    # 2. Decommission stuck agent (kill pane + delete record)
    mgr.decommission_agent(db, agent_id)

    # 3. Mark thread failed
    if thread_id:
        db.update_thread(thread_id, status="failed")

    # 4. Guard: already retried once
    if agent.get("watchdog_retried", 0) == 1:
        if thread_id:
            db.add_action_item(
                thread_id=thread_id,
                message=f"🛑 [{label}] agent stalled AGAIN after watchdog retry — manual intervention required. Snapshot: {snap_path}",
                type_="failure",
                priority="high",
            )
        db.add_watchdog_event(agent_id=agent_id, thread_id=thread_id,
                              event_type="retry_blocked", snapshot_path=str(snap_path))
        _log.warning("Watchdog: retry blocked for %s (already retried once)", agent_id[:8])
        return

    # 5. Guard: no task to replay
    if not last_task:
        if thread_id:
            db.add_action_item(
                thread_id=thread_id,
                message=f"🚨 [{label}] agent stalled — no task content to replay; re-dispatch manually. Snapshot: {snap_path}",
                type_="failure",
                priority="high",
            )
        db.add_watchdog_event(agent_id=agent_id, thread_id=thread_id,
                              event_type="stalled", snapshot_path=str(snap_path))
        return

    # File first action item (stall detected)
    if thread_id:
        db.add_action_item(
            thread_id=thread_id,
            message=f"🚨 [{label}] agent stalled/crashed — snapshot at {snap_path}, auto-retrying",
            type_="failure",
            priority="high",
        )

    # 6. Spawn new agent
    new_agent = mgr.spawn_agent(db, role=role, model=model)
    new_agent_id = new_agent["id"]
    new_pane_id = new_agent["pane_id"]

    # Assign to thread, mark as retry
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db.update_agent(new_agent_id,
                    status="busy",
                    assigned_thread=thread_id,
                    last_active=now,
                    busy_since=now,
                    watchdog_retried=1,
                    last_task=last_task)
    if thread_id:
        db.update_thread(thread_id, status="background")

    # Re-send task
    mgr.send_task(new_pane_id, last_task)

    # 7. File re-dispatch action item
    if thread_id:
        db.add_action_item(
            thread_id=thread_id,
            message=f"⚠️ [{label}] agent auto-re-dispatched after stall — verify result when complete",
            type_="manual_step",
            priority="normal",
        )

    db.add_watchdog_event(agent_id=agent_id, thread_id=thread_id,
                          event_type="recovered", snapshot_path=str(snap_path))
    _log.info("Watchdog: re-dispatched agent %s → %s for thread %s",
              agent_id[:8], new_agent_id[:8], (thread_id or "")[:8])
```

- [ ] **Step 4: Run recovery tests**

```bash
cd ~/github/juggle && python -m pytest tests/test_watchdog.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd ~/github/juggle
git add src/juggle_watchdog.py tests/test_watchdog.py
git commit -m "feat(watchdog): recovery logic — decommission, re-dispatch, retry guard"
```

---

## Task 5: Daemon script (juggle-agent-watchdog)

**Files:**
- Create: `scripts/juggle-agent-watchdog`

The thin daemon entry point. Imports from `juggle_watchdog.py`. Handles SIGTERM gracefully.

- [ ] **Step 1: Create `scripts/juggle-agent-watchdog`**

```python
#!/usr/bin/env python3
"""juggle-agent-watchdog — polls for stalled/crashed agents every 30s and recovers them."""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from juggle_db import JuggleDB, DB_PATH
from juggle_settings import get_settings
from juggle_tmux import JuggleTmuxManager
from juggle_watchdog import (
    classify_pane_state,
    execute_recovery,
    get_session_id,
    get_threshold_seconds,
    handle_prompt,
    read_snapshot,
    write_snapshot,
)

_POLL_INTERVAL = int(os.environ.get("JUGGLE_WATCHDOG_INTERVAL", "30"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(
            Path(get_settings()["paths"]["config_dir"]) / "watchdog.log"
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
_log = logging.getLogger("juggle-watchdog")

_running = True


def _handle_sigterm(signum, frame):
    global _running
    _log.info("Watchdog: SIGTERM received, shutting down")
    _running = False


def _get_dirs() -> tuple[Path, Path]:
    config_dir = Path(get_settings()["paths"]["config_dir"])
    return (
        config_dir / "watchdog" / "snapshots",
        config_dir / "watchdog" / "recovery",
    )


# Per-agent in-memory state (reset on each agent's recovery)
_last_changed: dict[str, float] = {}


def _capture_pane(mgr: JuggleTmuxManager, pane_id: str, lines: int = 80) -> str | None:
    """Return last `lines` of pane content; None if pane is gone."""
    if not mgr.verify_pane(pane_id):
        return None
    result = mgr._run_tmux("capture-pane", "-pt", pane_id, "-S", f"-{lines}")
    if result.returncode != 0:
        return None
    return result.stdout or ""


def _poll_once(db: JuggleDB, mgr: JuggleTmuxManager) -> None:
    snapshot_dir, recovery_dir = _get_dirs()
    now = time.time()
    session_id = get_session_id(db)
    agents = [a for a in db.get_all_agents() if a["status"] == "busy"]

    for agent in agents:
        agent_id = agent["id"]
        pane_id = agent["pane_id"]

        prev = read_snapshot(agent_id, snapshot_dir)
        content = _capture_pane(mgr, pane_id)

        # Compute stalled_for from when content last changed
        if agent_id not in _last_changed:
            _last_changed[agent_id] = now  # first time seen — treat as just changed

        # If content changed or this is first observation, update last_changed
        if content is not None and content != prev:
            _last_changed[agent_id] = now

        stalled_for = now - _last_changed.get(agent_id, now)
        threshold = get_threshold_seconds(db, agent)

        state, key = classify_pane_state(
            content=content,
            prev_content=prev,
            stalled_for=stalled_for,
            threshold=threshold,
        )

        if state == "working":
            write_snapshot(agent_id, content, snapshot_dir)

        elif state == "prompt":
            handle_prompt(db, mgr, agent, pane_id, key or "")
            write_snapshot(agent_id, content, snapshot_dir)
            _last_changed[agent_id] = now

        elif state in ("stalled", "crashed"):
            _log.warning(
                "Watchdog: agent %s is %s (stalled_for=%.0fs threshold=%.0fs)",
                agent_id[:8], state, stalled_for, threshold,
            )
            execute_recovery(
                db, mgr, agent, content or "",
                recovery_dir=recovery_dir,
                session_id=session_id,
            )
            _last_changed.pop(agent_id, None)

        # "quiet" — no action, wait for next cycle


def main() -> None:
    config_dir = Path(get_settings()["paths"]["config_dir"])
    pid_file = config_dir / "watchdog.pid"
    pid_file.write_text(str(os.getpid()))

    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    db = JuggleDB(str(DB_PATH))
    db.init_db()
    db.cleanup_watchdog_events()

    mgr = JuggleTmuxManager()
    _log.info("Watchdog started (PID=%d, interval=%ds)", os.getpid(), _POLL_INTERVAL)

    while _running:
        try:
            _poll_once(db, mgr)
        except Exception:
            _log.exception("Watchdog: unhandled error in poll — continuing")
        time.sleep(_POLL_INTERVAL)

    pid_file.unlink(missing_ok=True)
    _log.info("Watchdog stopped.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make the script executable**

```bash
chmod +x ~/github/juggle/scripts/juggle-agent-watchdog
```

- [ ] **Step 3: Smoke test the script imports**

```bash
cd ~/github/juggle && python scripts/juggle-agent-watchdog --help 2>&1 || python -c "
import sys; sys.path.insert(0, 'src')
# Just verify imports work
from juggle_watchdog import classify_pane_state, execute_recovery, get_threshold_seconds
from juggle_db import JuggleDB
print('imports OK')
"
```

Expected: `imports OK` (the script has no argparse so --help exits 2 — use the import test).

- [ ] **Step 4: Commit**

```bash
cd ~/github/juggle
git add scripts/juggle-agent-watchdog
git commit -m "feat(watchdog): daemon script — poll loop, signal handling, snapshot tracking"
```

---

## Task 6: Daemon lifecycle (cmd_start and cmd_stop)

**Files:**
- Modify: `src/juggle_cmd_threads.py`

`cmd_start` launches the watchdog as a detached background process and writes its PID. `cmd_stop` kills it.

- [ ] **Step 1: Add watchdog lifecycle helpers in juggle_cmd_threads.py**

Add this block after the imports in `juggle_cmd_threads.py` (near the top, after existing imports):

```python
import os
import signal
import subprocess
from pathlib import Path as _Path


def _watchdog_script() -> _Path:
    """Absolute path to the juggle-agent-watchdog script."""
    return _Path(__file__).parent.parent / "scripts" / "juggle-agent-watchdog"


def _watchdog_pid_file() -> _Path:
    from juggle_settings import get_settings
    return _Path(get_settings()["paths"]["config_dir"]) / "watchdog.pid"


def _start_watchdog() -> None:
    """Launch the watchdog daemon as a detached background process."""
    pid_file = _watchdog_pid_file()
    # Already running?
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # raises OSError if process is gone
            return  # already running
        except (OSError, ValueError):
            pid_file.unlink(missing_ok=True)

    log_path = pid_file.parent / "watchdog.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    script = _watchdog_script()
    if not script.exists():
        _log.warning("Watchdog script not found at %s — skipping watchdog start", script)
        return

    with open(log_path, "a") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    # The script writes its own PID file; give it 1s to start
    import time
    time.sleep(1)
    _log.info("Watchdog started (PID=%d)", proc.pid)


def _stop_watchdog() -> None:
    """Send SIGTERM to the watchdog daemon if running."""
    pid_file = _watchdog_pid_file()
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        _log.info("Watchdog stopped (PID=%d)", pid)
    except (OSError, ValueError, ProcessLookupError):
        pass
    finally:
        pid_file.unlink(missing_ok=True)
```

Also add `import sys` and `import logging` if not already present (check the top of `juggle_cmd_threads.py` — add only what's missing). Add `_log = logging.getLogger(__name__)` if not already there.

- [ ] **Step 2: Call `_start_watchdog()` in `cmd_start`**

In `cmd_start`, after `db.set_active(True)`, add:

```python
    _start_watchdog()
```

- [ ] **Step 3: Call `_stop_watchdog()` in `cmd_stop`**

In `cmd_stop`, before `print("Juggle stopped.")`, add:

```python
    _stop_watchdog()
```

- [ ] **Step 4: Verify start/stop manually**

```bash
# Start
cd ~/github/juggle && python src/juggle_cli.py start

# Verify PID file written
cat ~/.juggle/watchdog.pid

# Verify process is running
ps aux | grep juggle-agent-watchdog | grep -v grep

# Stop
python src/juggle_cli.py stop

# Verify PID file cleaned up
ls ~/.juggle/watchdog.pid 2>/dev/null && echo "FAIL: PID file still exists" || echo "PASS: PID file removed"
```

- [ ] **Step 5: Commit**

```bash
cd ~/github/juggle
git add src/juggle_cmd_threads.py
git commit -m "feat(watchdog): wire daemon lifecycle into juggle start/stop"
```

---

## Task 7: set-watchdog CLI command

**Files:**
- Modify: `src/juggle_cmd_agents.py` (add `cmd_set_watchdog`)
- Modify: `src/juggle_cli.py` (wire subcommand)
- Test: `tests/test_db_watchdog.py` (extend)

- [ ] **Step 1: Write failing tests**

Add to `tests/test_db_watchdog.py`:

```python
def test_set_watchdog_minutes(tmp_path):
    """set-watchdog <agent_id> 15 sets watchdog_threshold_minutes=15."""
    import os, subprocess, sys
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    agent_id = d.create_agent(role="coder", pane_id="%5")

    result = subprocess.run(
        [sys.executable, "src/juggle_cli.py", "set-watchdog", agent_id, "15"],
        cwd=str(Path(__file__).parent.parent),
        capture_output=True, text=True,
        env={**os.environ, "JUGGLE_DATA_DIR": str(tmp_path)},
    )
    assert result.returncode == 0
    agent = d.get_agent(agent_id)
    assert agent["watchdog_threshold_minutes"] == 15


def test_set_watchdog_off(tmp_path):
    """set-watchdog <agent_id> off sets watchdog_threshold_minutes=-1."""
    import os, subprocess, sys
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    agent_id = d.create_agent(role="coder", pane_id="%5")

    subprocess.run(
        [sys.executable, "src/juggle_cli.py", "set-watchdog", agent_id, "off"],
        cwd=str(Path(__file__).parent.parent),
        capture_output=True, text=True,
        env={**os.environ, "JUGGLE_DATA_DIR": str(tmp_path)},
    )
    agent = d.get_agent(agent_id)
    assert agent["watchdog_threshold_minutes"] == -1
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd ~/github/juggle && python -m pytest tests/test_db_watchdog.py::test_set_watchdog_minutes tests/test_db_watchdog.py::test_set_watchdog_off -v
```

Expected: 2 failures (subcommand not found).

- [ ] **Step 3: Add `cmd_set_watchdog` to juggle_cmd_agents.py**

Add after `cmd_decommission_agent`:

```python
def cmd_set_watchdog(args):
    db = get_db()
    agent = db.get_agent(args.agent_id)
    if agent is None:
        print(f"Error: Agent {args.agent_id} not found.")
        sys.exit(1)
    if args.value == "off":
        db.update_agent(args.agent_id, watchdog_threshold_minutes=-1)
        print(f"Watchdog disabled for agent {args.agent_id[:8]}.")
    else:
        try:
            minutes = int(args.value)
            if minutes <= 0:
                raise ValueError
        except ValueError:
            print(f"Error: value must be a positive integer or 'off', got {args.value!r}")
            sys.exit(1)
        db.update_agent(args.agent_id, watchdog_threshold_minutes=minutes)
        print(f"Watchdog threshold for agent {args.agent_id[:8]} set to {minutes} min.")
```

- [ ] **Step 4: Wire set-watchdog in juggle_cli.py**

In `juggle_cli.py`, find where other agent subcommands are registered (near `decommission-agent`, `release-agent`). Add:

```python
    # set-watchdog
    p_set_watchdog = subparsers.add_parser(
        "set-watchdog", help="Set per-agent watchdog threshold or disable it"
    )
    p_set_watchdog.add_argument("agent_id", help="Agent UUID")
    p_set_watchdog.add_argument(
        "value", help="Threshold in minutes, or 'off' to disable"
    )
    p_set_watchdog.set_defaults(func=cmd_set_watchdog)
```

Also add `cmd_set_watchdog` to the import from `juggle_cmd_agents`:

```python
from juggle_cmd_agents import (
    # ... existing imports ...
    cmd_set_watchdog,
)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd ~/github/juggle && python -m pytest tests/test_db_watchdog.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
cd ~/github/juggle
git add src/juggle_cmd_agents.py src/juggle_cli.py tests/test_db_watchdog.py
git commit -m "feat(watchdog): set-watchdog CLI command for per-agent threshold override"
```

---

## Task 8: Integration smoke test

**Files:**
- Create: `tests/test_watchdog_integration.py`

Verify the full watchdog poll cycle: stalled agent → recovery → action item filed.

- [ ] **Step 1: Create `tests/test_watchdog_integration.py`**

```python
"""Integration smoke: watchdog poll detects stall and fires recovery."""
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB
from juggle_watchdog import (
    classify_pane_state,
    execute_recovery,
    get_threshold_seconds,
    read_snapshot,
    write_snapshot,
)


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


def test_full_stall_recovery_cycle(db, tmp_path):
    """Simulate: agent busy → same pane content × threshold → recovery fires."""
    thread_id = db.create_thread("integration test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%9")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id,
                    last_task="do the work", watchdog_retried=0, model=None,
                    watchdog_threshold_minutes=1)  # 1 min override for speed

    snapshot_dir = tmp_path / "snapshots"
    recovery_dir = tmp_path / "recovery"

    pane_content = "Working on stuff\nstill here"

    # First observation — content "changes" (none→content)
    write_snapshot(agent_id, pane_content, snapshot_dir)

    # Classify with stalled_for=70s (> 60s threshold from 1-min override)
    agent = db.get_agent(agent_id)
    threshold = get_threshold_seconds(db, agent)
    assert threshold == 60.0  # 1 min = 60s

    state, key = classify_pane_state(
        content=pane_content,
        prev_content=pane_content,
        stalled_for=70.0,
        threshold=threshold,
    )
    assert state == "stalled"

    # Spawn mock returns a new agent
    new_agent_id = db.create_agent(role="coder", pane_id="%10")
    new_agent = db.get_agent(new_agent_id)

    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    mgr.spawn_agent.return_value = new_agent

    execute_recovery(
        db, mgr, agent, pane_content,
        recovery_dir=recovery_dir, session_id="",
    )

    # Old agent decommissioned
    assert db.get_agent(agent_id) is None

    # Thread re-opened to background
    thread = db.get_thread(thread_id)
    assert thread["status"] == "background"

    # New agent is busy + watchdog_retried=1
    new = db.get_agent(new_agent_id)
    assert new["watchdog_retried"] == 1
    assert new["status"] == "busy"

    # Recovery snapshot file created
    snaps = list(recovery_dir.glob(f"{agent_id}-*.txt"))
    assert len(snaps) == 1

    # Two action items: high stall + normal re-dispatch
    items = db.get_open_action_items()
    assert len(items) == 2
    priorities = {it["priority"] for it in items}
    assert priorities == {"high", "normal"}


def test_allowlist_resolution_no_recovery(db, tmp_path):
    """Permission prompt is auto-resolved — no recovery, no action item."""
    thread_id = db.create_thread("permission test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%7")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id)

    content = "Claude wants to run a bash command\n1. Yes / 2. Yes, allow always / 3. No"
    state, key = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=300.0,
        threshold=60.0,
    )
    assert state == "prompt"
    assert key == "2"
    # Verify no action items were created (handle_prompt only files notification)
    assert db.get_open_action_items() == []
```

- [ ] **Step 2: Run integration tests**

```bash
cd ~/github/juggle && python -m pytest tests/test_watchdog_integration.py -v
```

Expected: both tests pass.

- [ ] **Step 3: Run full test suite**

```bash
cd ~/github/juggle && python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all existing tests pass, no regressions.

- [ ] **Step 4: Commit**

```bash
cd ~/github/juggle
git add tests/test_watchdog_integration.py
git commit -m "test(watchdog): integration smoke — stall→recovery cycle, allowlist no-op"
```

---

## Task 9: Pre-PR quality gate

Before merging, the coder **must** invoke `mike:pre-pr` and resolve all findings.

- [ ] **Step 1: Invoke mike:pre-pr**

```
Invoke mike:pre-pr skill.
```

Fix all issues surfaced before declaring complete. Do NOT open a PR.

- [ ] **Step 2: Final commit with version bump**

Bump version by **minor** (new feature, non-breaking). Check current version in `pyproject.toml` or version file, increment minor, commit:

```bash
cd ~/github/juggle
# Find and bump version (e.g. 1.21.1 → 1.22.0)
# Edit the version string in the appropriate file, then:
git add -A
git commit -m "chore: bump version to vX.Y.Z for watchdog feature"
```

---

## Task 10: Additional schema columns + daemon refactor (migration 23)

**Files:**
- Modify: `src/juggle_db.py`
- Modify: `src/juggle_watchdog.py` (`execute_recovery` — copy dispatch payload to threads)
- Modify: `scripts/juggle-agent-watchdog` (replace `_last_changed` dict with `last_activity_at` DB column)
- Test: `tests/test_db_watchdog.py` (extend)

Adds 3 new `agents` columns (`last_send_task_pane_hash`, `last_send_task_at`, `last_activity_at`) and 3 new `threads` columns (`last_dispatched_task`, `last_dispatched_role`, `last_dispatched_model`). The daemon stops using an in-memory `_last_changed` dict and reads/writes `last_activity_at` from DB instead — making stall tracking survive watchdog restarts.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_db_watchdog.py`:

```python
def test_agents_has_last_send_task_pane_hash(db):
    assert "last_send_task_pane_hash" in _col_names(db, "agents")


def test_agents_has_last_send_task_at(db):
    assert "last_send_task_at" in _col_names(db, "agents")


def test_agents_has_last_activity_at(db):
    assert "last_activity_at" in _col_names(db, "agents")


def test_threads_has_last_dispatched_columns(db):
    cols = _col_names(db, "threads")
    assert "last_dispatched_task" in cols
    assert "last_dispatched_role" in cols
    assert "last_dispatched_model" in cols
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd ~/github/juggle && python -m pytest tests/test_db_watchdog.py::test_agents_has_last_send_task_pane_hash tests/test_db_watchdog.py::test_agents_has_last_activity_at tests/test_db_watchdog.py::test_threads_has_last_dispatched_columns -v
```

Expected: all 4 fail (columns don't exist).

- [ ] **Step 3: Add migration 23 in `juggle_db.py`**

In `init_db()`, after migration 22 block, add:

```python
        # Migration 23: pane hash + last_activity_at on agents; last_dispatched_* on threads
        try:
            agents_cols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
            threads_cols = {r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()}
            if "last_send_task_pane_hash" not in agents_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN last_send_task_pane_hash TEXT")
            if "last_send_task_at" not in agents_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN last_send_task_at TEXT")
            if "last_activity_at" not in agents_cols:
                conn.execute("ALTER TABLE agents ADD COLUMN last_activity_at TEXT")
            if "last_dispatched_task" not in threads_cols:
                conn.execute("ALTER TABLE threads ADD COLUMN last_dispatched_task TEXT")
            if "last_dispatched_role" not in threads_cols:
                conn.execute("ALTER TABLE threads ADD COLUMN last_dispatched_role TEXT")
            if "last_dispatched_model" not in threads_cols:
                conn.execute("ALTER TABLE threads ADD COLUMN last_dispatched_model TEXT")
            conn.commit()
            _log.info("Migration 23: pane hash + last_activity_at + last_dispatched_* columns added")
        except Exception as e:
            _log.warning("Migration 23 skipped: %s", e)
```

Also update `CREATE_AGENTS` to include the 3 new columns (fresh DBs get them without migration):

```python
CREATE_AGENTS = """
CREATE TABLE IF NOT EXISTS agents (
  id                         TEXT PRIMARY KEY,
  role                       TEXT NOT NULL,
  pane_id                    TEXT NOT NULL,
  assigned_thread            TEXT,
  status                     TEXT NOT NULL DEFAULT 'idle',
  context_threads            TEXT NOT NULL DEFAULT '[]',
  created_at                 TEXT NOT NULL,
  last_active                TEXT NOT NULL,
  watchdog_retried           INTEGER NOT NULL DEFAULT 0,
  watchdog_threshold_minutes INTEGER,
  model                      TEXT,
  last_task                  TEXT,
  busy_since                 TEXT,
  last_send_task_pane_hash   TEXT,
  last_send_task_at          TEXT,
  last_activity_at           TEXT
);
"""
```

Update `CREATE_THREADS` similarly — add the 3 `last_dispatched_*` columns at the end:

```sql
  last_dispatched_task  TEXT,
  last_dispatched_role  TEXT,
  last_dispatched_model TEXT
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/github/juggle && python -m pytest tests/test_db_watchdog.py -v
```

Expected: all tests pass, including the 4 new ones.

- [ ] **Step 5: Update `execute_recovery` in `src/juggle_watchdog.py`**

In `execute_recovery`, find step 2 (decommission):
```python
    # 2. Decommission stuck agent (kill pane + delete record)
    mgr.decommission_agent(db, agent_id)
```

Replace with:
```python
    # 2. Copy dispatch payload to thread before deleting agent record
    if thread_id:
        with db._connect() as conn:
            conn.execute(
                "UPDATE threads SET last_dispatched_task=?, last_dispatched_role=?, "
                "last_dispatched_model=? WHERE id=?",
                (last_task, role, model, thread_id),
            )
            conn.commit()
    # Decommission stuck agent (kill pane + delete record)
    mgr.decommission_agent(db, agent_id)
```

- [ ] **Step 6: Update daemon `scripts/juggle-agent-watchdog` to use `last_activity_at` from DB**

Remove the `_last_changed` module-level dict and all references to it. Replace the stall-tracking block inside `_poll_once`:

**Remove** this from the top of the daemon script:
```python
# Per-agent in-memory state (reset on each agent's recovery)
_last_changed: dict[str, float] = {}
```

**Replace** this block in `_poll_once` (before `stalled_for` is computed):
```python
        if agent_id not in _last_changed:
            _last_changed[agent_id] = now  # first time seen — treat as just changed

        # If content changed or this is first observation, update last_changed
        if content is not None and content != prev:
            _last_changed[agent_id] = now

        stalled_for = now - _last_changed.get(agent_id, now)
```

With:
```python
        last_activity_at_str = agent.get("last_activity_at")
        if last_activity_at_str:
            try:
                from datetime import datetime, timezone
                last_dt = datetime.fromisoformat(last_activity_at_str)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                stalled_for = now - last_dt.timestamp()
            except (ValueError, TypeError):
                stalled_for = 0.0
        else:
            stalled_for = 0.0  # first observation — treat as just changed
```

**Replace** the `state == "working"` handler:
```python
        if state == "working":
            write_snapshot(agent_id, content, snapshot_dir)
```

With:
```python
        if state == "working":
            write_snapshot(agent_id, content, snapshot_dir)
            from datetime import datetime, timezone
            db.update_agent(agent_id, last_activity_at=datetime.now(timezone.utc).isoformat())
```

**Remove** `_last_changed.pop(agent_id, None)` from the stalled/crashed recovery block.

- [ ] **Step 7: Commit**

```bash
cd ~/github/juggle
git add src/juggle_db.py src/juggle_watchdog.py scripts/juggle-agent-watchdog tests/test_db_watchdog.py
git commit -m "feat(watchdog): migration 23 — pane hash + last_activity_at + last_dispatched_* columns; daemon uses DB for stall tracking"
```

---

## Task 11: send-task pane hash instrumentation

**Files:**
- Modify: `src/juggle_tmux.py` (`JuggleTmuxManager.send_task` — capture hash pre-Enter, return it)
- Modify: `src/juggle_cmd_agents.py` (`cmd_send_task` — store hash + timestamp in DB)
- Test: `tests/test_db_watchdog.py` (extend)

`send_task` currently pastes the content and sends Enter in one shot. We need to split: paste → capture pane tail → hash → store → send Enter.

- [ ] **Step 1: Write failing test**

Add to `tests/test_db_watchdog.py`:

```python
def test_send_task_stores_pane_hash(tmp_path, monkeypatch):
    """send-task stores last_send_task_pane_hash (16 hex chars) and last_send_task_at."""
    import os, subprocess, sys
    db_path = str(tmp_path / "test.db")
    task_file = tmp_path / "task.txt"
    task_file.write_text("do something useful")

    d = JuggleDB(db_path)
    d.init_db()
    agent_id = d.create_agent(role="coder", pane_id="%5")
    d.update_agent(agent_id, status="busy")

    result = subprocess.run(
        [sys.executable, "src/juggle_cli.py", "send-task", agent_id, str(task_file)],
        cwd=str(Path(__file__).parent.parent),
        capture_output=True, text=True,
        env={**os.environ, "JUGGLE_DATA_DIR": str(tmp_path),
             "JUGGLE_TMUX_MOCK_SEND": "1", "JUGGLE_TMUX_MOCK_PANE": "%5"},
    )
    agent = d.get_agent(agent_id)
    assert agent["last_send_task_pane_hash"] is not None
    assert len(agent["last_send_task_pane_hash"]) == 16
    assert agent["last_send_task_at"] is not None
```

- [ ] **Step 2: Run to verify test fails**

```bash
cd ~/github/juggle && python -m pytest tests/test_db_watchdog.py::test_send_task_stores_pane_hash -v
```

Expected: FAIL (columns exist but not populated by send-task).

- [ ] **Step 3: Modify `JuggleTmuxManager.send_task` in `src/juggle_tmux.py`**

Find the existing `send_task` method. It should end with something like `self._run_tmux("send-keys", "-t", pane_id, "Enter")` or `self._run_tmux("send-keys", "-t", pane_id, "C-m")`.

Split the Enter-send out and add hash capture between paste and Enter. The method return type changes from `None` to `str`:

```python
def send_task(self, pane_id: str, content: str, *, is_new: bool = False) -> str:
    """Paste task content into pane.

    Returns post-paste-pre-Enter pane tail hash (SHA-256, 16 hex chars) for
    stuck-at-prompt detection. Enter is sent AFTER the hash is captured.
    """
    import hashlib
    import time as _time

    # --- existing paste logic (unchanged) ---
    # ... (keep all paste-buffer code exactly as-is) ...
    # --- end paste logic ---

    # Capture pane tail BEFORE sending Enter (brief settle for tmux render)
    _time.sleep(0.15)
    cap = self._run_tmux("capture-pane", "-pt", pane_id, "-S", "-10")
    tail = cap.stdout or "" if hasattr(cap, "stdout") else ""
    pane_hash = hashlib.sha256(tail.encode()).hexdigest()[:16]

    # Now send Enter
    self._run_tmux("send-keys", "-t", pane_id, "Enter")
    return pane_hash
```

Read the actual `send_task` body first (`grep -n "def send_task" src/juggle_tmux.py`) and insert the hash capture + Enter split at the correct point without disrupting the paste logic.

- [ ] **Step 4: Update `cmd_send_task` in `src/juggle_cmd_agents.py`**

Find:
```python
    mgr.send_task(pane_id, full_prompt, is_new=is_new)
    db.update_agent(args.agent_id, last_task=full_prompt)
```

Replace with:
```python
    pane_hash = mgr.send_task(pane_id, full_prompt, is_new=is_new)
    now_iso = datetime.now(timezone.utc).isoformat()
    db.update_agent(
        args.agent_id,
        last_task=full_prompt,
        last_send_task_pane_hash=pane_hash,
        last_send_task_at=now_iso,
    )
```

(`datetime` and `timezone` are already imported at the top of `juggle_cmd_agents.py`.)

- [ ] **Step 5: Run tests**

```bash
cd ~/github/juggle && python -m pytest tests/test_db_watchdog.py -v
```

Expected: all tests pass including `test_send_task_stores_pane_hash`.

- [ ] **Step 6: Commit**

```bash
cd ~/github/juggle
git add src/juggle_tmux.py src/juggle_cmd_agents.py tests/test_db_watchdog.py
git commit -m "feat(watchdog): capture post-paste pane hash in send-task for stuck-at-prompt detection"
```

---

## Task 12: Stuck-at-prompt classifier and Enter retry in daemon

**Files:**
- Modify: `src/juggle_watchdog.py` (`classify_pane_state` — add "stuck" state; add `_hash_tail`, `_has_execution_markers` helpers)
- Modify: `scripts/juggle-agent-watchdog` (handle "stuck" in `_poll_once`; in-memory Enter retry count)
- Test: `tests/test_watchdog.py` (extend)

Adds the 4-condition stuck-at-prompt check to the classifier and a 2× Enter retry loop in the daemon before escalating to aggressive recovery.

- [ ] **Step 1: Write failing tests**

Add to `tests/test_watchdog.py`:

```python
def test_classify_stuck_at_prompt():
    """Pane content bit-identical to last_send_task_pane_hash → stuck."""
    from juggle_watchdog import classify_pane_state, _hash_tail
    content = "╭─────────────────────╮\n│ do something useful │\n╰─────────────────────╯"
    pane_hash = _hash_tail(content)
    state, key = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=90.0,  # >= 60s grace
        threshold=300.0,
        last_send_task_pane_hash=pane_hash,
    )
    assert state == "stuck"
    assert key is None


def test_classify_stuck_not_triggered_within_grace():
    """Stuck-at-prompt NOT fired if stalled_for < 60s (grace window)."""
    from juggle_watchdog import classify_pane_state, _hash_tail
    content = "╭───╮\n│ x │\n╰───╯"
    pane_hash = _hash_tail(content)
    state, _ = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=30.0,  # within grace
        threshold=300.0,
        last_send_task_pane_hash=pane_hash,
    )
    assert state == "quiet"  # grace window, not stuck yet


def test_classify_stuck_not_triggered_with_execution_markers():
    """Stuck-at-prompt NOT fired when execution markers present."""
    from juggle_watchdog import classify_pane_state, _hash_tail
    content = "╭───╮\n│ x │\n╰───╯\n✻ Thinking…"
    pane_hash = _hash_tail(content)
    state, _ = classify_pane_state(
        content=content,
        prev_content=content,
        stalled_for=120.0,
        threshold=300.0,
        last_send_task_pane_hash=pane_hash,
    )
    assert state == "quiet"  # Thinking marker suppresses stuck


def test_classify_stuck_not_triggered_without_hash():
    """No hash → stuck-at-prompt never fires (falls through to quiet/stalled)."""
    from juggle_watchdog import classify_pane_state
    state, _ = classify_pane_state(
        content="unchanged",
        prev_content="unchanged",
        stalled_for=120.0,
        threshold=300.0,
        last_send_task_pane_hash=None,
    )
    assert state == "quiet"
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd ~/github/juggle && python -m pytest tests/test_watchdog.py::test_classify_stuck_at_prompt tests/test_watchdog.py::test_classify_stuck_not_triggered_within_grace -v
```

Expected: failures (`_hash_tail` not found, "stuck" state not returned).

- [ ] **Step 3: Add helpers and update `classify_pane_state` in `src/juggle_watchdog.py`**

After `_COLD_START_DEFAULTS`, add:

```python
import hashlib as _hashlib

_EXECUTION_MARKERS = ("Thinking", "Running", "→", "↓", "Tool call", "✓", "⚡")


def _hash_tail(content: str, lines: int = 10) -> str:
    """SHA-256 of the last `lines` of pane content, truncated to 16 hex chars."""
    tail = "\n".join(content.splitlines()[-lines:])
    return _hashlib.sha256(tail.encode()).hexdigest()[:16]


def _has_execution_markers(tail: str) -> bool:
    return any(m in tail for m in _EXECUTION_MARKERS)
```

Update `classify_pane_state` signature to accept `last_send_task_pane_hash`:

```python
def classify_pane_state(
    content: str | None,
    prev_content: str | None,
    stalled_for: float,
    threshold: float,
    *,
    last_send_task_pane_hash: str | None = None,
) -> tuple[str, str | None]:
```

Inside the function, after the allowlist check and after the bare-shell-prompt check, before `if content != prev_content`, add:

```python
    # Content unchanged from here — check stuck-at-prompt before quiet/stalled
    if content != prev_content:
        return "working", None

    # Stuck-at-prompt: all 4 conditions must hold
    if (
        last_send_task_pane_hash is not None
        and stalled_for >= 60
        and not _has_execution_markers(tail)
        and _hash_tail(content) == last_send_task_pane_hash
    ):
        return "stuck", None
```

Remove the original `if content != prev_content: return "working", None` line (it moved up).

Final order of checks in the function:
1. `if content is None` → crashed
2. Compute tail (last 15 lines)
3. Allowlist check → prompt
4. Bare shell prompt → crashed
5. `if content != prev_content` → working
6. Stuck-at-prompt (4 conditions) → stuck
7. `if "Thinking" in tail or stalled_for < 60` → quiet
8. `if stalled_for >= threshold` → stalled
9. Default → quiet

- [ ] **Step 4: Run classifier tests**

```bash
cd ~/github/juggle && python -m pytest tests/test_watchdog.py -v
```

Expected: all tests pass, including the 4 new stuck-at-prompt tests.

- [ ] **Step 5: Update daemon `scripts/juggle-agent-watchdog` to handle "stuck"**

Add a module-level dict for Enter retry tracking (in-memory, resets on restart — that's acceptable since we still have stalled-silent as a fallback):

```python
_enter_sent: dict[str, int] = {}  # agent_id → number of Enter keys sent so far
```

In `_poll_once`, update the `classify_pane_state` call to pass `last_send_task_pane_hash`:

```python
        state, key = classify_pane_state(
            content=content,
            prev_content=prev,
            stalled_for=stalled_for,
            threshold=threshold,
            last_send_task_pane_hash=agent.get("last_send_task_pane_hash"),
        )
```

Add the "stuck" handler after the `elif state == "prompt"` block:

```python
        elif state == "stuck":
            enter_count = _enter_sent.get(agent_id, 0)
            if enter_count < 2:
                mgr._run_tmux("send-keys", "-t", pane_id, "Enter")
                _enter_sent[agent_id] = enter_count + 1
                session_id = get_session_id(db)
                db.add_notification_v2(
                    thread_id=agent.get("assigned_thread"),
                    message=f"[Watchdog] agent {agent_id[:8]} stuck-at-prompt — sent Enter (attempt {enter_count + 1}/2)",
                    session_id=session_id,
                )
                _log.info("Watchdog: stuck-at-prompt Enter #%d sent to agent %s", enter_count + 1, agent_id[:8])
            else:
                _log.warning(
                    "Watchdog: agent %s stuck after 2 Enters — escalating to recovery",
                    agent_id[:8],
                )
                execute_recovery(
                    db, mgr, agent, content or "",
                    recovery_dir=recovery_dir,
                    session_id=session_id,
                )
                _enter_sent.pop(agent_id, None)
```

Also reset `_enter_sent` when an agent recovers or is no longer stuck. In the `state == "working"` handler, add:

```python
            _enter_sent.pop(agent_id, None)
```

- [ ] **Step 6: Run all watchdog tests**

```bash
cd ~/github/juggle && python -m pytest tests/test_watchdog.py tests/test_watchdog_integration.py -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
cd ~/github/juggle
git add src/juggle_watchdog.py scripts/juggle-agent-watchdog tests/test_watchdog.py
git commit -m "feat(watchdog): stuck-at-prompt classifier + Enter retry (2x) before escalation"
```

---

## Task 13: Orphaned thread detection (Loop 2)

**Files:**
- Modify: `src/juggle_watchdog.py` (add `check_orphaned_threads` pure function)
- Modify: `scripts/juggle-agent-watchdog` (call `check_orphaned_threads` in `_poll_once` as Loop 2)
- Test: `tests/test_watchdog_integration.py` (extend)

After the per-agent Loop 1, scan all background threads with no active agent. File a high-priority action item for any thread orphaned longer than `JUGGLE_ORPHAN_THRESHOLD` (default 5 min). Dedup via `watchdog_events` to avoid re-filing within 24 h.

- [ ] **Step 1: Write failing test**

Add to `tests/test_watchdog_integration.py`:

```python
def test_orphaned_thread_files_action_item(db, tmp_path):
    """Background thread with no agent and old last_active_at gets an action item."""
    from datetime import datetime, timezone, timedelta
    from juggle_watchdog import check_orphaned_threads

    thread_id = db.create_thread("orphan test", session_id="")
    # Set thread to background with last_active_at 10 min ago
    db.set_thread_status(thread_id, "background")
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with db._connect() as conn:
        conn.execute(
            "UPDATE threads SET last_active_at=? WHERE id=?", (past, thread_id)
        )
        conn.commit()

    orphaned = check_orphaned_threads(db, orphan_threshold=300.0)
    assert thread_id in orphaned

    items = db.get_open_action_items()
    assert any("orphaned" in it["message"].lower() for it in items)
    priorities = {it["priority"] for it in items}
    assert "high" in priorities


def test_orphaned_thread_dedup(db, tmp_path):
    """A second call within 24h does NOT file a duplicate action item."""
    from datetime import datetime, timezone, timedelta
    from juggle_watchdog import check_orphaned_threads

    thread_id = db.create_thread("dedup test", session_id="")
    db.set_thread_status(thread_id, "background")
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with db._connect() as conn:
        conn.execute(
            "UPDATE threads SET last_active_at=? WHERE id=?", (past, thread_id)
        )
        conn.commit()

    check_orphaned_threads(db, orphan_threshold=300.0)
    check_orphaned_threads(db, orphan_threshold=300.0)  # second call

    items = db.get_open_action_items()
    orphan_items = [it for it in items if "orphaned" in it["message"].lower()]
    assert len(orphan_items) == 1  # only one, not two


def test_active_thread_not_orphaned(db, tmp_path):
    """Thread with an active busy agent is NOT flagged as orphaned."""
    from datetime import datetime, timezone, timedelta
    from juggle_watchdog import check_orphaned_threads

    thread_id = db.create_thread("active test", session_id="")
    db.set_thread_status(thread_id, "background")
    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id)
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with db._connect() as conn:
        conn.execute(
            "UPDATE threads SET last_active_at=? WHERE id=?", (past, thread_id)
        )
        conn.commit()

    orphaned = check_orphaned_threads(db, orphan_threshold=300.0)
    assert thread_id not in orphaned
    assert db.get_open_action_items() == []
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd ~/github/juggle && python -m pytest tests/test_watchdog_integration.py::test_orphaned_thread_files_action_item tests/test_watchdog_integration.py::test_orphaned_thread_dedup -v
```

Expected: `ImportError: cannot import name 'check_orphaned_threads'`.

- [ ] **Step 3: Add `check_orphaned_threads` to `src/juggle_watchdog.py`**

Add after `execute_recovery`:

```python
# ---------------------------------------------------------------------------
# Orphaned thread detection (Loop 2)
# ---------------------------------------------------------------------------

def check_orphaned_threads(
    db: Any,
    *,
    orphan_threshold: float = 300.0,
    dedup_window_hours: float = 24.0,
) -> list[str]:
    """Scan background threads with no active agent; file action items for orphans.

    Returns list of orphaned thread_ids detected this cycle.
    Dedup guard: skips threads that already have an 'orphaned' watchdog_event
    within the last `dedup_window_hours`.
    """
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    dedup_cutoff = (now - timedelta(hours=dedup_window_hours)).isoformat()

    with db._connect() as conn:
        thread_rows = conn.execute(
            "SELECT * FROM threads WHERE status='background'"
        ).fetchall()
        threads = [dict(r) for r in thread_rows]

        busy_rows = conn.execute(
            "SELECT assigned_thread FROM agents WHERE status='busy' AND assigned_thread IS NOT NULL"
        ).fetchall()
        busy_thread_ids = {r["assigned_thread"] for r in busy_rows}

    orphaned: list[str] = []

    for thread in threads:
        thread_id = thread["id"]
        if thread_id in busy_thread_ids:
            continue

        last_active_at = thread.get("last_active_at")
        if not last_active_at:
            continue

        try:
            last_dt = datetime.fromisoformat(last_active_at)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            orphaned_for = (now - last_dt).total_seconds()
        except (ValueError, TypeError):
            continue

        if orphaned_for < orphan_threshold:
            continue

        # Dedup: skip if already filed within dedup window
        with db._connect() as conn:
            recent = conn.execute(
                "SELECT id FROM watchdog_events "
                "WHERE thread_id=? AND event_type='orphaned' AND created_at > ?",
                (thread_id, dedup_cutoff),
            ).fetchone()
        if recent:
            continue

        label = thread.get("user_label") or thread.get("label") or thread_id[:8]
        mins = int(orphaned_for // 60)
        last_task = thread.get("last_dispatched_task")
        task_snippet = f"\n  Last task: {last_task[:80]}..." if last_task else ""

        db.add_action_item(
            thread_id=thread_id,
            message=(
                f"🔴 [{label}] orphaned — background thread with no agent for {mins} min"
                f"{task_snippet}\n"
                f"  State: orphaned\n"
                f"  Last activity: {mins} min ago\n"
                f"  Recovery attempted: none (auto-recovery OOS v1)\n"
                f"  Next step: re-dispatch manually"
            ),
            type_="failure",
            priority="high",
        )
        db.add_watchdog_event(
            agent_id="",
            thread_id=thread_id,
            event_type="orphaned",
            snapshot_path=None,
        )
        orphaned.append(thread_id)
        _log.warning(
            "Watchdog: orphaned thread %s (%s, %d min no agent)", thread_id[:8], label, mins
        )

    return orphaned
```

- [ ] **Step 4: Wire Loop 2 into daemon `scripts/juggle-agent-watchdog`**

Add `check_orphaned_threads` to the imports at the top of the daemon script:

```python
from juggle_watchdog import (
    check_orphaned_threads,
    classify_pane_state,
    execute_recovery,
    get_session_id,
    get_threshold_seconds,
    handle_prompt,
    read_snapshot,
    write_snapshot,
)
```

At the end of `_poll_once`, after the Loop 1 `for agent in agents:` block, add:

```python
    # Loop 2: orphaned thread detection
    _orphan_threshold = float(os.environ.get("JUGGLE_ORPHAN_THRESHOLD", "300"))
    check_orphaned_threads(db, orphan_threshold=_orphan_threshold)
```

- [ ] **Step 5: Run all tests**

```bash
cd ~/github/juggle && python -m pytest tests/ -v --tb=short 2>&1 | tail -40
```

Expected: all tests pass, no regressions.

- [ ] **Step 6: Commit**

```bash
cd ~/github/juggle
git add src/juggle_watchdog.py scripts/juggle-agent-watchdog tests/test_watchdog_integration.py
git commit -m "feat(watchdog): orphaned thread detection (Loop 2) — action item after 5 min, 24h dedup"
```

---

## Self-Review Against Spec

Checklist run against `docs/superpowers/specs/2026-05-17-juggle-agent-watchdog.md`:

| Spec requirement | Task covering it |
|---|---|
| New DB columns: watchdog_retried, watchdog_threshold_minutes, model, last_task, busy_since | Task 1 |
| agent_completions table + index | Task 1 |
| watchdog_events table | Task 1 |
| get-agent sets busy_since, model | Task 2 |
| send-task stores last_task | Task 2 |
| complete-agent inserts agent_completions row | Task 2 |
| State classifier (working/quiet/prompt/stalled/crashed) | Task 3 |
| Snapshot helpers (read/write/recovery/prune) | Task 3 |
| Adaptive threshold (2× median, cold-start defaults) | Task 3 |
| Recovery: decommission + spawn + re-send + retry guard | Task 4 |
| Guard: watchdog_retried==1 → stop | Task 4 |
| Guard: last_task==None → manual action item | Task 4 |
| Daemon poll loop (30s, SIGTERM) | Task 5 |
| PID file management | Task 5 + 6 |
| cmd_start launches watchdog | Task 6 |
| cmd_stop kills watchdog | Task 6 |
| set-watchdog CLI | Task 7 |
| allowlist auto-responses (permission/plan-mode/Enter) | Task 3 + 4 |
| watchdog_events telemetry | Task 1 + 4 |
| Snapshot retention (last 100) | Task 3 |
| watchdog_events retention (30d cleanup) | Task 1 + 5 |
| Action items: high for crash/stall, normal for re-dispatch | Task 4 |
| Cockpit notification for prompt auto-resolve | Task 4 |
| New agents columns: last_send_task_pane_hash, last_send_task_at, last_activity_at | Task 10 |
| New threads columns: last_dispatched_task/role/model | Task 10 |
| Daemon uses last_activity_at from DB (not in-memory dict) | Task 10 |
| execute_recovery copies dispatch payload to threads before decommission | Task 10 |
| send-task captures post-paste-pre-Enter pane hash | Task 11 |
| send-task stores last_send_task_pane_hash + last_send_task_at in DB | Task 11 |
| Stuck-at-prompt state (4-condition classifier, "stuck" return value) | Task 12 |
| 2× Enter retry before escalation to aggressive recovery | Task 12 |
| Orphaned thread detection as Loop 2 in daemon | Task 13 |
| check_orphaned_threads pure function with 24h dedup guard | Task 13 |
| Structured action item format for orphaned threads | Task 13 |
| pre-PR gate | Task 9 |
