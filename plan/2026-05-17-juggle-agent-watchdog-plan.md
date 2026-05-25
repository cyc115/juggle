# Juggle Agent Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a background daemon that detects stalled/crashed Juggle agents (via tmux pane snapshot diffs), auto-resolves permission prompts, and re-dispatches agents that go silent past an adaptive threshold.

**Architecture:** A new Python module `src/juggle_watchdog.py` holds all pure logic (classifier, threshold, snapshot helpers, recovery, orphaned detection). A thin daemon script `scripts/juggle-agent-watchdog` runs the 30-second poll loop. `cmd_start` launches it as a background process (PID file at `~/.juggle/watchdog.pid`); `cmd_stop` sends SIGTERM. New DB schema tracks `busy_since`, `model`, `last_task`, `watchdog_retried`, `last_send_task_pane_hash`, `last_send_task_at`, `last_activity_at` on `agents`, plus `last_dispatched_task/role/model` on `threads`, plus `agent_completions` and `watchdog_events` tables.

**Tech Stack:** Python 3.11+, SQLite (via existing `JuggleDB`), tmux subprocess, pytest. All code in `~/github/juggle/`. Commit directly to `main`.

**Spec:** `docs/superpowers/specs/2026-05-17-juggle-agent-watchdog.md` — read it before implementing.

---

## File Map

| Action | File | Purpose |
|---|---|---|
| Modify | `src/juggle_db.py` | Schema migrations 20–22; new DB methods |
| Modify | `src/juggle_cmd_agents.py` | Track busy_since/model on get-agent; last_task + pane hash on send-task; completion on complete-agent; dispatch payload copy on release-agent; cmd_set_watchdog + cmd_stop_watchdog |
| Modify | `src/juggle_tmux.py` | Capture post-paste pane hash in send_task (returns str instead of None) |
| Modify | `src/juggle_cmd_threads.py` | cmd_start launches watchdog daemon; cmd_stop kills it |
| Modify | `src/juggle_cli.py` | Wire set-watchdog + stop-watchdog subcommands |
| **Create** | `src/juggle_watchdog.py` | All pure watchdog logic: snapshot helpers, state classifier (incl. stuck-at-prompt), threshold, recovery, orphaned detection |
| **Create** | `scripts/juggle-agent-watchdog` | Thin daemon entry point: poll loop + signal handler + Loop 2 |
| **Create** | `tests/test_watchdog.py` | Unit tests for juggle_watchdog.py |
| **Create** | `tests/test_db_watchdog.py` | Tests for new DB schema + methods |
| **Create** | `tests/test_watchdog_integration.py` | Integration smoke tests |

---

## Devil's Advocate (Plan)

_DA pass on plan execution (not the spec — spec was already DA'd twice). Findings below must be resolved before the coder starts._

### DA-1: Hidden ordering dependency (resolved by merge)
Tasks 10-13 introduced dependencies on files already authored by earlier tasks: Task 11 needed Task 10's migration columns to exist before populating them; Task 12 changed Task 3's `classify_pane_state` signature, breaking Task 3's tests when run in isolation. **Resolution:** Merge 10→1, 11→2, 12→3, 13→4 so each file is authored once with full final content.

### DA-2 (MISSING TASK): `cmd_release_agent` must copy dispatch payload
The spec (Schema Changes section) states: "The copy [of last_task/role/model to threads] happens in: (a) the watchdog's recovery flow (step 3, before delete_agent), and (b) `cmd_release_agent` before deleting the agent row." No task implemented (b). Without it, if an agent is manually released, orphaned detection cannot surface the last task content. **Resolution:** Added as Step 3b in Task 2.

### DA-3 (MISSING): `stop-watchdog` CLI command
The spec CLI Changes table lists `stop-watchdog` as a new command. No task implemented it. Task 6 wires `_stop_watchdog()` into `cmd_stop`, but `juggle stop-watchdog` as a standalone command is absent. **Resolution:** Added to Task 7.

### DA-4 (SPEC DISCREPANCY): Snapshot pruning is per-agent, not global
Task 3's `write_recovery_snapshot` globs `recovery_dir.glob("*.txt")` and prunes to the last 100 total across all agents. The spec says "keep last 100 per agent." **Resolution:** Change glob to `recovery_dir.glob(f"{agent_id}-*.txt")` in Task 3.

### DA-5 (TEST GAP): Retry-blocked test doesn't assert thread status
`test_execute_recovery_second_stall_blocked` asserts `mgr.spawn_agent.assert_not_called()` but doesn't verify the thread ends up `status='failed'`. Recovery step 3 marks the thread failed before the retry guard fires. **Resolution:** Add `assert db.get_thread(thread_id)["status"] == "failed"` to that test in Task 4.

### DA-6 (RACE CONDITION): `execute_recovery` doesn't recheck `agent.status`
Spec DA item 1 says "Watchdog checks `agent.status` immediately before each recovery action." The plan's `execute_recovery` takes a snapshot dict and proceeds without a DB recheck. A manual `release-agent` between the poll cycle and recovery could cause double-decommission. **Resolution:** Add a DB recheck at the start of `execute_recovery` — if `db.get_agent(agent_id)` returns None or status != 'busy', abort.

### DA-7: Orphaned event `agent_id=""` is semantically wrong
`check_orphaned_threads` calls `db.add_watchdog_event(agent_id="", ...)`. The column is `NOT NULL` but an empty string is misleading. **Resolution:** Use `agent_id="orphan_detector"` as sentinel.

### DA-8: `send_task` return-type change breaks callers
`JuggleTmuxManager.send_task` currently returns `None`; after Task 2 it returns `str`. Any caller that currently ignores the return value is fine, but callers in test mocks may set `return_value=None`. **Resolution:** Audit all callers of `mgr.send_task` in tests and update mock return values to a 16-char hex string (e.g. `"deadbeef00000000"`).

### DA-9: Task 9 references `pyproject.toml` which doesn't exist
Juggle has no `pyproject.toml` or `VERSION` file. Version is tracked in commit messages only. **Resolution:** Task 9 Step 2 updated to reflect this.

### DA-10: `_poll_once` daemon logic is untested directly
Unit tests cover classifier + recovery functions; integration tests cover component chains. The daemon's routing logic in `_poll_once` (snapshot write on "working", `_enter_sent` tracking on "stuck", Loop 2 dispatch) is not covered by automated tests. **Accepted risk** — `_poll_once` has tmux side effects that require extensive mocking. The test harness baseline suite (already merged to main) provides observational coverage.

---

## Task 1: Schema migrations (juggle_db.py)

Highest existing migration is **19**. Add migrations 20, 21, 22 covering ALL watchdog schema columns — including those that were formerly in bolt-on Task 10. No migration 23.

**Files:**
- Modify: `src/juggle_db.py`
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


# --- Migration 20: agents columns ---

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

def test_agents_has_last_send_task_pane_hash(db):
    assert "last_send_task_pane_hash" in _col_names(db, "agents")

def test_agents_has_last_send_task_at(db):
    assert "last_send_task_at" in _col_names(db, "agents")

def test_agents_has_last_activity_at(db):
    assert "last_activity_at" in _col_names(db, "agents")

# --- Migration 22: threads columns ---

def test_threads_has_last_dispatched_columns(db):
    cols = _col_names(db, "threads")
    assert "last_dispatched_task" in cols
    assert "last_dispatched_role" in cols
    assert "last_dispatched_model" in cols

# --- Tables ---

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

# --- DB methods ---

def test_insert_agent_completion(db):
    db.insert_agent_completion(role="coder", duration_secs=120.5)
    with db._connect() as conn:
        row = conn.execute("SELECT * FROM agent_completions").fetchone()
    assert row["role"] == "coder"
    assert abs(row["duration_secs"] - 120.5) < 0.01

def test_get_median_coldstart(db):
    db.insert_agent_completion(role="coder", duration_secs=100.0)
    assert db.get_median_duration_secs("coder") is None  # < 10 samples

def test_get_median_adaptive(db):
    for i in range(10):
        db.insert_agent_completion(role="coder", duration_secs=float(100 + i * 10))
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

After `CREATE_SETTINGS`, add:

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

After migration 19, add:

```python
        # Migration 20: all watchdog columns on agents (includes pane hash + activity tracking)
        agents_cols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()}
        try:
            for col, defn in [
                ("watchdog_retried",           "INTEGER NOT NULL DEFAULT 0"),
                ("watchdog_threshold_minutes", "INTEGER"),
                ("model",                      "TEXT"),
                ("last_task",                  "TEXT"),
                ("busy_since",                 "TEXT"),
                ("last_send_task_pane_hash",   "TEXT"),
                ("last_send_task_at",          "TEXT"),
                ("last_activity_at",           "TEXT"),
            ]:
                if col not in agents_cols:
                    conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {defn}")
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

        # Migration 22: watchdog_events table + threads dispatch payload columns
        try:
            conn.execute(CREATE_WATCHDOG_EVENTS)
            threads_cols = {r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()}
            for col in ("last_dispatched_task", "last_dispatched_role", "last_dispatched_model"):
                if col not in threads_cols:
                    conn.execute(f"ALTER TABLE threads ADD COLUMN {col} TEXT")
            conn.commit()
            _log.info("Migration 22: watchdog_events + threads dispatch payload columns created")
        except Exception as e:
            _log.warning("Migration 22 (watchdog_events + threads) skipped: %s", e)
```

Also update `CREATE_AGENTS` constant to include all new columns (fresh DBs skip migrations):

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

Also update `CREATE_THREADS` to add the three `last_dispatched_*` columns at the end:

```sql
  last_dispatched_task  TEXT,
  last_dispatched_role  TEXT,
  last_dispatched_model TEXT
```

- [ ] **Step 5: Add new DB methods to juggle_db.py**

Add to the `JuggleDB` class after `get_agent_by_thread`:

```python
    def insert_agent_completion(self, role: str, duration_secs: float) -> None:
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
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO watchdog_events (agent_id, thread_id, event_type, snapshot_path, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent_id, thread_id, event_type, snapshot_path, now),
            )
            conn.commit()

    def cleanup_watchdog_events(self, days: int = 30) -> int:
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
git commit -m "feat(watchdog): schema migrations 20-22 — all watchdog cols, agent_completions, watchdog_events, threads dispatch payload"
```

---

## Task 2: Agent metadata tracking (get-agent, send-task, complete-agent, release-agent)

**Files:**
- Modify: `src/juggle_cmd_agents.py`
- Modify: `src/juggle_tmux.py` (split send_task to capture pane hash pre-Enter)
- Test: `tests/test_db_watchdog.py` (extend)

Covers what was formerly Tasks 2 and 11. All send-task changes happen once. Also adds the missing `cmd_release_agent` dispatch payload copy (DA-2).

**⚠ Before modifying `send_task` in juggle_tmux.py:** Run `grep -n "send_task" src/juggle_tmux.py src/juggle_cmd_agents.py tests/` to find all callers. Any test mock with `return_value=None` for `send_task` must be updated to return a 16-char hex string (e.g. `"deadbeef00000000"`) — DA-8.

- [ ] **Step 1: Write failing tests for metadata tracking**

Add to `tests/test_db_watchdog.py`:

```python
def test_get_agent_sets_busy_since(tmp_path, monkeypatch):
    import os, subprocess, sys
    monkeypatch.setenv("JUGGLE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("JUGGLE_TMUX_MOCK_PANE", "%5")

    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    thread_id = d.create_thread("test topic", session_id="")
    agent_id_raw = d.create_agent(role="coder", pane_id="%5")
    d.update_agent(agent_id_raw, status="idle")

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


def test_send_task_stores_last_task_and_pane_hash(tmp_path, monkeypatch):
    """send-task writes last_task, last_send_task_pane_hash (16 hex chars), last_send_task_at."""
    import os, subprocess, sys
    task_file = tmp_path / "task.txt"
    task_file.write_text("do something useful")

    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    agent_id = d.create_agent(role="coder", pane_id="%5")
    d.update_agent(agent_id, status="busy")

    subprocess.run(
        [sys.executable, "src/juggle_cli.py", "send-task", agent_id, str(task_file)],
        cwd=str(Path(__file__).parent.parent),
        capture_output=True, text=True,
        env={**os.environ, "JUGGLE_DATA_DIR": str(tmp_path),
             "JUGGLE_TMUX_MOCK_SEND": "1", "JUGGLE_TMUX_MOCK_PANE": "%5"},
    )
    agent = d.get_agent(agent_id)
    assert agent["last_task"] == "do something useful"
    assert agent["last_send_task_pane_hash"] is not None
    assert len(agent["last_send_task_pane_hash"]) == 16
    assert agent["last_send_task_at"] is not None


def test_complete_agent_inserts_completion(tmp_path, monkeypatch):
    import os, subprocess, sys
    from datetime import datetime, timezone, timedelta

    d = JuggleDB(str(tmp_path / "test.db"))
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
    with d._connect() as conn:
        rows = conn.execute("SELECT * FROM agent_completions").fetchall()
    assert len(rows) == 1
    assert rows[0]["role"] == "coder"
    assert rows[0]["duration_secs"] >= 100


def test_release_agent_copies_dispatch_payload(tmp_path):
    """release-agent copies last_task/role/model to thread before decommissioning agent."""
    import argparse
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    import juggle_cli_common as common
    import juggle_cmd_agents

    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()

    import importlib
    monkeypatch_db = lambda: d  # noqa: E731
    original_get_db = juggle_cmd_agents.get_db
    juggle_cmd_agents.get_db = monkeypatch_db
    common.get_db = monkeypatch_db

    thread_id = d.create_thread("payload test", session_id="")
    d.update_thread(thread_id, status="background")
    agent_id = d.create_agent(role="coder", pane_id="%5")
    d.update_agent(agent_id, status="busy", assigned_thread=thread_id,
                   last_task="do the thing", model="claude-sonnet-4-6")

    from juggle_cmd_agents import cmd_release_agent
    args = argparse.Namespace(agent_id=agent_id, force=True)
    cmd_release_agent(args)

    thread = d.get_thread(thread_id)
    assert thread["last_dispatched_task"] == "do the thing"
    assert thread["last_dispatched_role"] == "coder"
    assert thread["last_dispatched_model"] == "claude-sonnet-4-6"

    juggle_cmd_agents.get_db = original_get_db
    common.get_db = original_get_db
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd ~/github/juggle && python -m pytest tests/test_db_watchdog.py::test_get_agent_sets_busy_since tests/test_db_watchdog.py::test_send_task_stores_last_task_and_pane_hash tests/test_db_watchdog.py::test_complete_agent_inserts_completion tests/test_db_watchdog.py::test_release_agent_copies_dispatch_payload -v
```

Expected: 4 failures.

- [ ] **Step 3: Update `cmd_get_agent` in juggle_cmd_agents.py**

Find (around line 450):
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

- [ ] **Step 4: Modify `JuggleTmuxManager.send_task` in juggle_tmux.py**

Read the existing `send_task` body first:
```bash
grep -n "def send_task" src/juggle_tmux.py
```

Split the Enter-send to capture hash pre-Enter. Change return type to `str`:

```python
def send_task(self, pane_id: str, content: str, *, is_new: bool = False) -> str:
    """Paste task content; returns 16-hex post-paste-pre-Enter pane tail hash."""
    import hashlib
    import time as _time

    # --- existing paste logic (keep ALL paste-buffer code exactly as-is) ---
    # ... (DO NOT change the paste section) ...
    # --- end paste logic ---

    # Capture pane tail BEFORE sending Enter
    _time.sleep(0.15)
    cap = self._run_tmux("capture-pane", "-pt", pane_id, "-S", "-10")
    tail = (cap.stdout or "") if hasattr(cap, "stdout") else ""
    pane_hash = hashlib.sha256(tail.encode()).hexdigest()[:16]

    # Now send Enter
    self._run_tmux("send-keys", "-t", pane_id, "Enter")
    return pane_hash
```

**Important:** Update all test mocks that patch `mgr.send_task` to return `"deadbeef00000000"` instead of the default `MagicMock()`.

- [ ] **Step 5: Update `cmd_send_task` in juggle_cmd_agents.py**

Find:
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
    pane_hash = mgr.send_task(pane_id, full_prompt, is_new=is_new)
    now_iso = datetime.now(timezone.utc).isoformat()
    db.update_agent(
        args.agent_id,
        last_task=full_prompt,
        last_send_task_pane_hash=pane_hash,
        last_send_task_at=now_iso,
    )
    print(f"Task sent to agent {args.agent_id[:8]} (pane {pane_id}).")
```

- [ ] **Step 6: Update `cmd_complete_agent` in juggle_cmd_agents.py**

Find:
```python
    agent = db.get_agent_by_thread(thread_uuid)
    if agent:
        db.update_agent(agent["id"], status="idle", assigned_thread=None)
```

Replace with:
```python
    agent = db.get_agent_by_thread(thread_uuid)
    if agent:
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

- [ ] **Step 7: Update `cmd_release_agent` to copy dispatch payload (DA-2)**

In `cmd_release_agent`, find the block that checks if the assigned thread is still "background" and files the failure action item. BEFORE calling `db.update_agent(agent_id, status="idle", ...)`, add:

```python
    # Copy dispatch payload to thread before clearing agent record (spec requirement)
    if assigned:
        agent_snap = db.get_agent(agent_id)
        if agent_snap:
            with db._connect() as conn:
                conn.execute(
                    "UPDATE threads SET last_dispatched_task=?, last_dispatched_role=?, "
                    "last_dispatched_model=? WHERE id=?",
                    (agent_snap.get("last_task"), agent_snap.get("role"),
                     agent_snap.get("model"), assigned),
                )
                conn.commit()
```

- [ ] **Step 8: Run tests to verify they pass**

```bash
cd ~/github/juggle && python -m pytest tests/test_db_watchdog.py -v
```

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
cd ~/github/juggle
git add src/juggle_cmd_agents.py src/juggle_tmux.py tests/test_db_watchdog.py
git commit -m "feat(watchdog): track busy_since/model/last_task/pane_hash on agents; insert agent_completions; copy dispatch payload on release"
```

---

## Task 3: Core watchdog module — snapshot helpers, classifier, threshold, stuck-at-prompt

**Files:**
- Create: `src/juggle_watchdog.py`
- Test: `tests/test_watchdog.py`

Covers what was formerly Tasks 3 and 12. The full final `juggle_watchdog.py` is created here with all pure functions including stuck-at-prompt. The daemon script (Task 5) imports from here.

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
        content=None, prev_content="some previous",
        stalled_for=0.0, threshold=60.0,
    )
    assert state == "crashed"


def test_classify_crashed_shell_prompt():
    from juggle_watchdog import classify_pane_state
    state, key = classify_pane_state(
        content="some output\nmikechen@host:~$ ",
        prev_content="some output\nmikechen@host:~$ ",
        stalled_for=200.0, threshold=60.0,
    )
    assert state == "crashed"


def test_classify_prompt_permission():
    from juggle_watchdog import classify_pane_state
    content = "Claude wants to run a command\n1. Yes / 2. Yes, allow always / 3. No"
    state, key = classify_pane_state(
        content=content, prev_content=content,
        stalled_for=300.0, threshold=60.0,
    )
    assert state == "prompt"
    assert key == "2"


def test_classify_prompt_plan_mode():
    from juggle_watchdog import classify_pane_state
    content = "Review the plan\n1. Yes, auto-accept / 2. Yes, manually approve / 3. No"
    state, key = classify_pane_state(
        content=content, prev_content=content,
        stalled_for=300.0, threshold=60.0,
    )
    assert state == "prompt"
    assert key == "2"


def test_classify_prompt_press_enter():
    from juggle_watchdog import classify_pane_state
    content = "long output\nPress Enter to continue"
    state, key = classify_pane_state(
        content=content, prev_content=content,
        stalled_for=300.0, threshold=60.0,
    )
    assert state == "prompt"
    assert key == ""


def test_classify_quiet_thinking():
    from juggle_watchdog import classify_pane_state
    content = "doing stuff\nThinking…"
    state, key = classify_pane_state(
        content=content, prev_content=content,
        stalled_for=300.0, threshold=60.0,
    )
    assert state == "quiet"


def test_classify_quiet_within_threshold():
    from juggle_watchdog import classify_pane_state
    state, key = classify_pane_state(
        content="unchanged", prev_content="unchanged",
        stalled_for=30.0, threshold=120.0,
    )
    assert state == "quiet"


def test_classify_stalled():
    from juggle_watchdog import classify_pane_state
    state, key = classify_pane_state(
        content="unchanged", prev_content="unchanged",
        stalled_for=400.0, threshold=120.0,
    )
    assert state == "stalled"


# --- Stuck-at-prompt classifier (formerly Task 12) ---

def test_classify_stuck_at_prompt():
    from juggle_watchdog import classify_pane_state, _hash_tail
    content = "╭─────────────────────╮\n│ do something useful │\n╰─────────────────────╯"
    pane_hash = _hash_tail(content)
    state, key = classify_pane_state(
        content=content, prev_content=content,
        stalled_for=90.0, threshold=300.0,
        last_send_task_pane_hash=pane_hash,
    )
    assert state == "stuck"
    assert key is None


def test_classify_stuck_not_triggered_within_grace():
    from juggle_watchdog import classify_pane_state, _hash_tail
    content = "╭───╮\n│ x │\n╰───╯"
    pane_hash = _hash_tail(content)
    state, _ = classify_pane_state(
        content=content, prev_content=content,
        stalled_for=30.0, threshold=300.0,
        last_send_task_pane_hash=pane_hash,
    )
    assert state == "quiet"


def test_classify_stuck_not_triggered_with_execution_markers():
    from juggle_watchdog import classify_pane_state, _hash_tail
    content = "╭───╮\n│ x │\n╰───╯\n✻ Thinking…"
    pane_hash = _hash_tail(content)
    state, _ = classify_pane_state(
        content=content, prev_content=content,
        stalled_for=120.0, threshold=300.0,
        last_send_task_pane_hash=pane_hash,
    )
    assert state == "quiet"


def test_classify_stuck_not_triggered_without_hash():
    from juggle_watchdog import classify_pane_state
    state, _ = classify_pane_state(
        content="unchanged", prev_content="unchanged",
        stalled_for=120.0, threshold=300.0,
        last_send_task_pane_hash=None,
    )
    assert state == "quiet"


# --- Threshold ---

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
    assert get_threshold_seconds(db, agent) == 300.0


def test_get_threshold_coldstart_planner():
    from juggle_watchdog import get_threshold_seconds
    db = MagicMock()
    db.get_median_duration_secs.return_value = None
    agent = {"watchdog_threshold_minutes": None, "role": "planner"}
    assert get_threshold_seconds(db, agent) == 180.0


def test_get_threshold_adaptive():
    from juggle_watchdog import get_threshold_seconds
    db = MagicMock()
    db.get_median_duration_secs.return_value = 90.0
    agent = {"watchdog_threshold_minutes": None, "role": "coder"}
    assert get_threshold_seconds(db, agent) == 180.0


# --- Snapshot helpers ---

def test_snapshot_roundtrip(tmp_path):
    from juggle_watchdog import read_snapshot, write_snapshot
    write_snapshot("agent-123", "hello world", snapshot_dir=tmp_path)
    result = read_snapshot("agent-123", snapshot_dir=tmp_path)
    assert result == "hello world"


def test_read_snapshot_missing(tmp_path):
    from juggle_watchdog import read_snapshot
    assert read_snapshot("no-such-agent", snapshot_dir=tmp_path) is None


def test_recovery_snapshot_prune_per_agent(tmp_path):
    """write_recovery_snapshot prunes to 100 files per agent, not globally."""
    from juggle_watchdog import write_recovery_snapshot
    import time
    recovery_dir = tmp_path / "recovery"
    for i in range(105):
        write_recovery_snapshot("agent-A", f"content-{i}", recovery_dir)
        time.sleep(0.001)
    # Only 100 files for agent-A
    a_files = list(recovery_dir.glob("agent-A-*.txt"))
    assert len(a_files) == 100
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd ~/github/juggle && python -m pytest tests/test_watchdog.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError: No module named 'juggle_watchdog'`.

- [ ] **Step 3: Create `src/juggle_watchdog.py`**

```python
"""Juggle agent watchdog — pure functions for the watchdog daemon."""
from __future__ import annotations

import hashlib as _hashlib
import logging
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

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
_EXECUTION_MARKERS = ("Thinking", "Running", "→", "↓", "Tool call", "✓", "⚡")


def _hash_tail(content: str, lines: int = 10) -> str:
    tail = "\n".join(content.splitlines()[-lines:])
    return _hashlib.sha256(tail.encode()).hexdigest()[:16]


def _has_execution_markers(tail: str) -> bool:
    return any(m in tail for m in _EXECUTION_MARKERS)


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
    """Write a 500-line recovery snapshot; prune to last 100 per agent (DA-4 fix)."""
    recovery_dir.mkdir(parents=True, exist_ok=True)
    import time
    ts = int(time.time())
    path = recovery_dir / f"{agent_id}-{ts}.txt"
    path.write_text(content)
    # Prune per-agent (not global) — glob only this agent's files
    agent_snaps = sorted(recovery_dir.glob(f"{agent_id}-*.txt"),
                         key=lambda p: p.stat().st_mtime)
    for old in agent_snaps[:-100]:
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
    *,
    last_send_task_pane_hash: str | None = None,
) -> tuple[str, str | None]:
    """Classify agent pane state.

    Returns (state, key_to_send):
      ("working", None) | ("crashed", None) | ("prompt", key)
      | ("stuck", None) | ("quiet", None) | ("stalled", None)

    Classification order (most specific first):
    1. content is None → crashed
    2. Allowlist match → prompt
    3. Bare shell prompt → crashed
    4. Content changed → working
    5. Stuck-at-prompt (4 conditions) → stuck
    6. Thinking grace or within 60s → quiet
    7. Past threshold → stalled
    8. Default → quiet
    """
    if content is None:
        return "crashed", None

    tail = "\n".join(content.splitlines()[-15:])

    for pattern, key in _ALLOWLIST:
        if pattern in tail:
            return "prompt", key

    last_nonempty = next(
        (line for line in reversed(content.splitlines()) if line.strip()), ""
    )
    if any(last_nonempty.endswith(suffix) for suffix in _SHELL_SUFFIXES):
        return "crashed", None

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

    if "Thinking" in tail or stalled_for < 60:
        return "quiet", None

    if stalled_for >= threshold:
        return "stalled", None
    return "quiet", None


# ---------------------------------------------------------------------------
# Threshold
# ---------------------------------------------------------------------------

def get_threshold_seconds(db: Any, agent: dict) -> float:
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
    _log.info("Watchdog: prompt resolved for agent %s key=%r", agent["id"][:8], key)


# ---------------------------------------------------------------------------
# Recovery (execute_recovery written in Task 4)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Orphaned thread detection — Loop 2 (written in Task 4)
# ---------------------------------------------------------------------------
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
git commit -m "feat(watchdog): core module — snapshot helpers, classifier (incl. stuck-at-prompt), threshold"
```

---

## Task 4: Recovery logic + orphaned thread detection

**Files:**
- Modify: `src/juggle_watchdog.py` (add `execute_recovery`, `check_orphaned_threads`)
- Test: `tests/test_watchdog.py` (recovery tests)
- Test: `tests/test_watchdog_integration.py` (orphaned detection tests)

Covers what was formerly Tasks 4 and 13.

- [ ] **Step 1: Write failing tests for recovery**

Add to `tests/test_watchdog.py`:

```python
def test_execute_recovery_aborts_if_agent_gone(tmp_path):
    """Recovery aborts if DB recheck shows agent already released (DA-6)."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from juggle_watchdog import execute_recovery
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    thread_id = db.create_thread("test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id,
                    last_task="do work", watchdog_retried=0)

    # Simulate: agent released by orchestrator BEFORE recovery runs
    db.update_agent(agent_id, status="idle", assigned_thread=None)

    mgr = MagicMock()
    recovery_dir = tmp_path / "recovery"
    execute_recovery(db, mgr, db.get_agent(agent_id), "pane content",
                     recovery_dir=recovery_dir, session_id="")

    # decommission_agent must NOT be called — agent is already idle
    mgr.decommission_agent.assert_not_called()


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
    recovery_dir = tmp_path / "recovery"
    execute_recovery(db, mgr, db.get_agent(agent_id), "pane content",
                     recovery_dir=recovery_dir, session_id="")

    items = db.get_open_action_items()
    assert any("no task content" in it["message"] for it in items)
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
    recovery_dir = tmp_path / "recovery"
    execute_recovery(db, mgr, db.get_agent(agent_id), "pane content",
                     recovery_dir=recovery_dir, session_id="")

    items = db.get_open_action_items()
    assert any("stalled AGAIN" in it["message"] for it in items)
    mgr.spawn_agent.assert_not_called()
    # DA-5: thread must be 'failed' even in retry-blocked case
    assert db.get_thread(thread_id)["status"] == "failed"


def test_execute_recovery_full_flow(tmp_path):
    """Successful recovery: decommissions old, spawns new, re-sends task."""
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

    new_agent_id = db.create_agent(role="coder", pane_id="%6")
    new_agent = db.get_agent(new_agent_id)

    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    mgr.spawn_agent.return_value = new_agent
    recovery_dir = tmp_path / "recovery"

    execute_recovery(db, mgr, db.get_agent(agent_id), "pane content",
                     recovery_dir=recovery_dir, session_id="")

    assert db.get_agent(agent_id) is None
    assert db.get_thread(thread_id)["status"] == "background"
    updated_new = db.get_agent(new_agent_id)
    assert updated_new["watchdog_retried"] == 1
    assert updated_new["status"] == "busy"
    mgr.send_task.assert_called_once_with("%6", "do the work")
    items = db.get_open_action_items()
    assert "high" in {it["priority"] for it in items}
    assert any("auto-re-dispatched" in it["message"] for it in items)
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd ~/github/juggle && python -m pytest tests/test_watchdog.py::test_execute_recovery_no_last_task tests/test_watchdog.py::test_execute_recovery_second_stall_blocked tests/test_watchdog.py::test_execute_recovery_full_flow -v
```

Expected: 3 failures (`execute_recovery` not found).

- [ ] **Step 3: Add `execute_recovery` to `src/juggle_watchdog.py`**

Replace the `# Recovery (execute_recovery written in Task 4)` placeholder:

```python
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
    """Decommission a stalled/crashed agent and (if eligible) re-dispatch it."""
    agent_id = agent["id"]

    # DA-6: Recheck agent status from DB to guard against TOCTOU race
    live = db.get_agent(agent_id)
    if live is None or live.get("status") != "busy":
        _log.info("Watchdog: recovery aborted for %s — agent no longer busy", agent_id[:8])
        return

    pane_id = agent["pane_id"]
    thread_id = agent.get("assigned_thread")
    role = agent.get("role", "researcher")
    model = agent.get("model")
    last_task = agent.get("last_task")
    label = _get_thread_label(db, thread_id) if thread_id else agent_id[:8]

    # 1. Save recovery snapshot
    snap_path = write_recovery_snapshot(agent_id, pane_content, recovery_dir)
    _log.info("Watchdog: recovery snapshot saved to %s", snap_path)

    # 2. Copy dispatch payload to thread before deleting agent record
    if thread_id:
        with db._connect() as conn:
            conn.execute(
                "UPDATE threads SET last_dispatched_task=?, last_dispatched_role=?, "
                "last_dispatched_model=? WHERE id=?",
                (last_task, role, model, thread_id),
            )
            conn.commit()

    # 3. Decommission stuck agent
    mgr.decommission_agent(db, agent_id)

    # 4. Mark thread failed
    if thread_id:
        db.update_thread(thread_id, status="failed")

    # 5. Guard: already retried once
    if agent.get("watchdog_retried", 0) == 1:
        if thread_id:
            db.add_action_item(
                thread_id=thread_id,
                message=f"🛑 [{label}] agent stalled AGAIN after watchdog retry — manual intervention required. Snapshot: {snap_path}",
                type_="failure", priority="high",
            )
        db.add_watchdog_event(agent_id=agent_id, thread_id=thread_id,
                              event_type="retry_blocked", snapshot_path=str(snap_path))
        return

    # 6. Guard: no task to replay
    if not last_task:
        if thread_id:
            db.add_action_item(
                thread_id=thread_id,
                message=f"🚨 [{label}] agent stalled — no task content to replay; re-dispatch manually. Snapshot: {snap_path}",
                type_="failure", priority="high",
            )
        db.add_watchdog_event(agent_id=agent_id, thread_id=thread_id,
                              event_type="stalled", snapshot_path=str(snap_path))
        return

    # File stall action item
    if thread_id:
        db.add_action_item(
            thread_id=thread_id,
            message=f"🚨 [{label}] agent stalled/crashed — snapshot at {snap_path}, auto-retrying",
            type_="failure", priority="high",
        )

    # 7. Spawn new agent
    new_agent = mgr.spawn_agent(db, role=role, model=model)
    new_agent_id = new_agent["id"]
    new_pane_id = new_agent["pane_id"]

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    db.update_agent(new_agent_id, status="busy", assigned_thread=thread_id,
                    last_active=now, busy_since=now, watchdog_retried=1, last_task=last_task)
    if thread_id:
        db.update_thread(thread_id, status="background")

    mgr.send_task(new_pane_id, last_task)

    if thread_id:
        db.add_action_item(
            thread_id=thread_id,
            message=f"⚠️ [{label}] agent auto-re-dispatched after stall — verify result when complete",
            type_="manual_step", priority="normal",
        )

    db.add_watchdog_event(agent_id=agent_id, thread_id=thread_id,
                          event_type="recovered", snapshot_path=str(snap_path))
    _log.info("Watchdog: re-dispatched %s → %s for thread %s",
              agent_id[:8], new_agent_id[:8], (thread_id or "")[:8])
```

- [ ] **Step 4: Run recovery tests**

```bash
cd ~/github/juggle && python -m pytest tests/test_watchdog.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Write failing tests for orphaned detection**

Create `tests/test_watchdog_integration.py`:

```python
"""Integration smoke + orphaned detection tests."""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


def test_orphaned_thread_files_action_item(db, tmp_path):
    from datetime import datetime, timezone, timedelta
    from juggle_watchdog import check_orphaned_threads

    thread_id = db.create_thread("orphan test", session_id="")
    db.set_thread_status(thread_id, "background")
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with db._connect() as conn:
        conn.execute("UPDATE threads SET last_active_at=? WHERE id=?", (past, thread_id))
        conn.commit()

    orphaned = check_orphaned_threads(db, orphan_threshold=300.0)
    assert thread_id in orphaned

    items = db.get_open_action_items()
    assert any("orphaned" in it["message"].lower() for it in items)
    assert "high" in {it["priority"] for it in items}


def test_orphaned_thread_dedup(db, tmp_path):
    from datetime import datetime, timezone, timedelta
    from juggle_watchdog import check_orphaned_threads

    thread_id = db.create_thread("dedup test", session_id="")
    db.set_thread_status(thread_id, "background")
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with db._connect() as conn:
        conn.execute("UPDATE threads SET last_active_at=? WHERE id=?", (past, thread_id))
        conn.commit()

    check_orphaned_threads(db, orphan_threshold=300.0)
    check_orphaned_threads(db, orphan_threshold=300.0)

    items = db.get_open_action_items()
    orphan_items = [it for it in items if "orphaned" in it["message"].lower()]
    assert len(orphan_items) == 1


def test_active_thread_not_orphaned(db, tmp_path):
    from datetime import datetime, timezone, timedelta
    from juggle_watchdog import check_orphaned_threads

    thread_id = db.create_thread("active test", session_id="")
    db.set_thread_status(thread_id, "background")
    agent_id = db.create_agent(role="coder", pane_id="%5")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id)
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with db._connect() as conn:
        conn.execute("UPDATE threads SET last_active_at=? WHERE id=?", (past, thread_id))
        conn.commit()

    orphaned = check_orphaned_threads(db, orphan_threshold=300.0)
    assert thread_id not in orphaned
    assert db.get_open_action_items() == []
```

- [ ] **Step 6: Run to verify orphaned tests fail**

```bash
cd ~/github/juggle && python -m pytest tests/test_watchdog_integration.py -v
```

Expected: `ImportError: cannot import name 'check_orphaned_threads'`.

- [ ] **Step 7: Add `check_orphaned_threads` to `src/juggle_watchdog.py`**

Replace the `# Orphaned thread detection — Loop 2 (written in Task 4)` placeholder:

```python
# ---------------------------------------------------------------------------
# Orphaned thread detection — Loop 2
# ---------------------------------------------------------------------------

def check_orphaned_threads(
    db: Any,
    *,
    orphan_threshold: float = 300.0,
    dedup_window_hours: float = 24.0,
) -> list[str]:
    """Scan background threads with no active agent; file action items for orphans.

    Returns list of orphaned thread_ids detected this cycle. Uses 24h dedup guard.
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
            type_="failure", priority="high",
        )
        # DA-7: use sentinel agent_id, not empty string
        db.add_watchdog_event(
            agent_id="orphan_detector",
            thread_id=thread_id,
            event_type="orphaned",
            snapshot_path=None,
        )
        orphaned.append(thread_id)
        _log.warning("Watchdog: orphaned thread %s (%s, %d min no agent)",
                     thread_id[:8], label, mins)

    return orphaned
```

- [ ] **Step 8: Run all watchdog tests**

```bash
cd ~/github/juggle && python -m pytest tests/test_watchdog.py tests/test_watchdog_integration.py -v
```

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
cd ~/github/juggle
git add src/juggle_watchdog.py tests/test_watchdog.py tests/test_watchdog_integration.py
git commit -m "feat(watchdog): recovery logic + orphaned thread detection (Loop 2) — decommission, re-dispatch, retry guard, 24h dedup"
```

---

## Task 5: Daemon script (juggle-agent-watchdog)

**Files:**
- Create: `scripts/juggle-agent-watchdog`

The thin daemon entry point. Includes: poll loop, signal handler, Loop 1 (all states including stuck-at-prompt Enter retry), Loop 2 (orphaned), and `last_activity_at` DB tracking (formerly Task 10's daemon refactor). Written once with full final content — no later tasks touch this file.

- [ ] **Step 1: Create `scripts/juggle-agent-watchdog`**

```python
#!/usr/bin/env python3
"""juggle-agent-watchdog — polls for stalled/crashed agents every 30s."""
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
    check_orphaned_threads,
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
# In-memory Enter retry count; resets on restart (stalled-silent is fallback)
_enter_sent: dict[str, int] = {}


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


def _capture_pane(mgr: JuggleTmuxManager, pane_id: str, lines: int = 80) -> str | None:
    if not mgr.verify_pane(pane_id):
        return None
    result = mgr._run_tmux("capture-pane", "-pt", pane_id, "-S", f"-{lines}")
    if result.returncode != 0:
        return None
    return result.stdout or ""


def _poll_once(db: JuggleDB, mgr: JuggleTmuxManager) -> None:
    snapshot_dir, recovery_dir = _get_dirs()
    now_ts = time.time()
    session_id = get_session_id(db)
    agents = [a for a in db.get_all_agents() if a["status"] == "busy"]

    for agent in agents:
        agent_id = agent["id"]
        pane_id = agent["pane_id"]

        prev = read_snapshot(agent_id, snapshot_dir)
        content = _capture_pane(mgr, pane_id)

        # Compute stalled_for from last_activity_at in DB (survives restarts)
        last_activity_at_str = agent.get("last_activity_at")
        if last_activity_at_str:
            try:
                from datetime import datetime, timezone
                last_dt = datetime.fromisoformat(last_activity_at_str)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                stalled_for = now_ts - last_dt.timestamp()
            except (ValueError, TypeError):
                stalled_for = 0.0
        else:
            stalled_for = 0.0  # first observation — treat as just changed

        threshold = get_threshold_seconds(db, agent)

        state, key = classify_pane_state(
            content=content,
            prev_content=prev,
            stalled_for=stalled_for,
            threshold=threshold,
            last_send_task_pane_hash=agent.get("last_send_task_pane_hash"),
        )

        if state == "working":
            write_snapshot(agent_id, content, snapshot_dir)
            from datetime import datetime, timezone
            db.update_agent(agent_id, last_activity_at=datetime.now(timezone.utc).isoformat())
            _enter_sent.pop(agent_id, None)

        elif state == "prompt":
            handle_prompt(db, mgr, agent, pane_id, key or "")
            write_snapshot(agent_id, content, snapshot_dir)
            from datetime import datetime, timezone
            db.update_agent(agent_id, last_activity_at=datetime.now(timezone.utc).isoformat())
            _enter_sent.pop(agent_id, None)

        elif state == "stuck":
            enter_count = _enter_sent.get(agent_id, 0)
            if enter_count < 2:
                mgr._run_tmux("send-keys", "-t", pane_id, "Enter")
                _enter_sent[agent_id] = enter_count + 1
                db.add_notification_v2(
                    thread_id=agent.get("assigned_thread"),
                    message=f"[Watchdog] agent {agent_id[:8]} stuck-at-prompt — sent Enter (attempt {enter_count + 1}/2)",
                    session_id=session_id,
                )
                _log.info("Watchdog: stuck-at-prompt Enter #%d sent to %s",
                          enter_count + 1, agent_id[:8])
            else:
                _log.warning("Watchdog: agent %s stuck after 2 Enters — escalating to recovery",
                             agent_id[:8])
                execute_recovery(db, mgr, agent, content or "",
                                 recovery_dir=recovery_dir, session_id=session_id)
                _enter_sent.pop(agent_id, None)

        elif state in ("stalled", "crashed"):
            _log.warning("Watchdog: agent %s is %s (stalled_for=%.0fs threshold=%.0fs)",
                         agent_id[:8], state, stalled_for, threshold)
            execute_recovery(db, mgr, agent, content or "",
                             recovery_dir=recovery_dir, session_id=session_id)

        # "quiet" — no action

    # Loop 2: orphaned thread detection
    _orphan_threshold = float(os.environ.get("JUGGLE_ORPHAN_THRESHOLD", "300"))
    check_orphaned_threads(db, orphan_threshold=_orphan_threshold)


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
cd ~/github/juggle && python -c "
import sys; sys.path.insert(0, 'src')
from juggle_watchdog import (classify_pane_state, execute_recovery, get_threshold_seconds,
    check_orphaned_threads, _hash_tail)
from juggle_db import JuggleDB
print('imports OK')
"
```

Expected: `imports OK`.

- [ ] **Step 4: Commit**

```bash
cd ~/github/juggle
git add scripts/juggle-agent-watchdog
git commit -m "feat(watchdog): daemon script — poll loop, signal handling, last_activity_at DB tracking, stuck-at-prompt retry, Loop 2"
```

---

## Task 6: Daemon lifecycle (cmd_start and cmd_stop)

**Files:**
- Modify: `src/juggle_cmd_threads.py`

- [ ] **Step 1: Add watchdog lifecycle helpers in juggle_cmd_threads.py**

Add after the imports (add `import os`, `import signal`, `import subprocess` if not present; add `_log = logging.getLogger(__name__)` if not present):

```python
from pathlib import Path as _Path


def _watchdog_script() -> _Path:
    return _Path(__file__).parent.parent / "scripts" / "juggle-agent-watchdog"


def _watchdog_pid_file() -> _Path:
    from juggle_settings import get_settings
    return _Path(get_settings()["paths"]["config_dir"]) / "watchdog.pid"


def _start_watchdog() -> None:
    pid_file = _watchdog_pid_file()
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return  # already running
        except (OSError, ValueError):
            pid_file.unlink(missing_ok=True)

    log_path = pid_file.parent / "watchdog.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    script = _watchdog_script()
    if not script.exists():
        _log.warning("Watchdog script not found at %s — skipping", script)
        return

    with open(log_path, "a") as log_fh:
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            stdout=log_fh, stderr=log_fh,
            start_new_session=True,
        )
    import time
    time.sleep(1)
    _log.info("Watchdog started (PID=%d)", proc.pid)


def _stop_watchdog() -> None:
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

- [ ] **Step 2: Call `_start_watchdog()` in `cmd_start`**

In `cmd_start`, after `db.set_active(True)`, add:

```python
    _start_watchdog()
```

- [ ] **Step 3: Call `_stop_watchdog()` in `cmd_stop`**

In `cmd_stop`, before the final print, add:

```python
    _stop_watchdog()
```

- [ ] **Step 4: Verify start/stop manually**

```bash
cd ~/github/juggle && python src/juggle_cli.py start
cat ~/.juggle/watchdog.pid
ps aux | grep juggle-agent-watchdog | grep -v grep
python src/juggle_cli.py stop
ls ~/.juggle/watchdog.pid 2>/dev/null && echo "FAIL: PID file still exists" || echo "PASS: PID file removed"
```

- [ ] **Step 5: Commit**

```bash
cd ~/github/juggle
git add src/juggle_cmd_threads.py
git commit -m "feat(watchdog): wire daemon lifecycle into juggle start/stop"
```

---

## Task 7: set-watchdog and stop-watchdog CLI commands

**Files:**
- Modify: `src/juggle_cmd_agents.py` (add `cmd_set_watchdog`, `cmd_stop_watchdog`)
- Modify: `src/juggle_cli.py` (wire both subcommands)
- Test: `tests/test_db_watchdog.py` (extend)

Covers `set-watchdog` plus the missing `stop-watchdog` command (DA-3).

- [ ] **Step 1: Write failing tests**

Add to `tests/test_db_watchdog.py`:

```python
def test_set_watchdog_minutes(tmp_path):
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
    assert d.get_agent(agent_id)["watchdog_threshold_minutes"] == 15


def test_set_watchdog_off(tmp_path):
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
    assert d.get_agent(agent_id)["watchdog_threshold_minutes"] == -1
```

- [ ] **Step 2: Run to verify tests fail**

```bash
cd ~/github/juggle && python -m pytest tests/test_db_watchdog.py::test_set_watchdog_minutes tests/test_db_watchdog.py::test_set_watchdog_off -v
```

Expected: 2 failures.

- [ ] **Step 3: Add `cmd_set_watchdog` and `cmd_stop_watchdog` to juggle_cmd_agents.py**

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


def cmd_stop_watchdog(_args):
    """Send SIGTERM to the watchdog daemon if running."""
    import os, signal
    from pathlib import Path
    from juggle_settings import get_settings
    pid_file = Path(get_settings()["paths"]["config_dir"]) / "watchdog.pid"
    if not pid_file.exists():
        print("Watchdog is not running.")
        return
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        pid_file.unlink(missing_ok=True)
        print(f"Watchdog stopped (PID={pid}).")
    except (OSError, ValueError, ProcessLookupError) as e:
        print(f"Error stopping watchdog: {e}")
        pid_file.unlink(missing_ok=True)
```

- [ ] **Step 4: Wire both commands in juggle_cli.py**

Add to the subparser section:

```python
    # set-watchdog
    p_set_watchdog = subparsers.add_parser(
        "set-watchdog", help="Set per-agent watchdog threshold or disable it"
    )
    p_set_watchdog.add_argument("agent_id")
    p_set_watchdog.add_argument("value", help="Minutes (int) or 'off'")
    p_set_watchdog.set_defaults(func=cmd_set_watchdog)

    # stop-watchdog
    p_stop_watchdog = subparsers.add_parser(
        "stop-watchdog", help="Send SIGTERM to the watchdog daemon"
    )
    p_stop_watchdog.set_defaults(func=cmd_stop_watchdog)
```

Update the import from `juggle_cmd_agents`:

```python
from juggle_cmd_agents import (
    # ... existing ...
    cmd_set_watchdog,
    cmd_stop_watchdog,
)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd ~/github/juggle && python -m pytest tests/test_db_watchdog.py -v
```

- [ ] **Step 6: Commit**

```bash
cd ~/github/juggle
git add src/juggle_cmd_agents.py src/juggle_cli.py tests/test_db_watchdog.py
git commit -m "feat(watchdog): set-watchdog + stop-watchdog CLI commands"
```

---

## Task 8: Integration smoke test

**Files:**
- Modify: `tests/test_watchdog_integration.py` (extend)

- [ ] **Step 1: Add integration tests**

Add to `tests/test_watchdog_integration.py`:

```python
def test_full_stall_recovery_cycle(db, tmp_path):
    """Simulate: agent busy → same pane content × threshold → recovery fires."""
    from juggle_watchdog import (classify_pane_state, execute_recovery,
                                  get_threshold_seconds, write_snapshot)

    thread_id = db.create_thread("integration test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%9")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id,
                    last_task="do the work", watchdog_retried=0,
                    watchdog_threshold_minutes=1)

    snapshot_dir = tmp_path / "snapshots"
    recovery_dir = tmp_path / "recovery"
    pane_content = "Working on stuff\nstill here"
    write_snapshot(agent_id, pane_content, snapshot_dir)

    agent = db.get_agent(agent_id)
    threshold = get_threshold_seconds(db, agent)
    assert threshold == 60.0

    state, key = classify_pane_state(
        content=pane_content, prev_content=pane_content,
        stalled_for=70.0, threshold=threshold,
    )
    assert state == "stalled"

    new_agent_id = db.create_agent(role="coder", pane_id="%10")
    new_agent = db.get_agent(new_agent_id)

    mgr = MagicMock()
    mgr.verify_pane.return_value = True
    mgr.spawn_agent.return_value = new_agent

    execute_recovery(db, mgr, agent, pane_content,
                     recovery_dir=recovery_dir, session_id="")

    assert db.get_agent(agent_id) is None
    assert db.get_thread(thread_id)["status"] == "background"
    new = db.get_agent(new_agent_id)
    assert new["watchdog_retried"] == 1
    assert new["status"] == "busy"
    snaps = list(recovery_dir.glob(f"{agent_id}-*.txt"))
    assert len(snaps) == 1
    items = db.get_open_action_items()
    assert len(items) == 2
    assert {it["priority"] for it in items} == {"high", "normal"}


def test_allowlist_resolution_no_recovery(db, tmp_path):
    """Permission prompt auto-resolved — no recovery, no action item."""
    from juggle_watchdog import classify_pane_state

    thread_id = db.create_thread("permission test", session_id="")
    agent_id = db.create_agent(role="coder", pane_id="%7")
    db.update_agent(agent_id, status="busy", assigned_thread=thread_id)

    content = "Claude wants to run a bash command\n1. Yes / 2. Yes, allow always / 3. No"
    state, key = classify_pane_state(
        content=content, prev_content=content,
        stalled_for=300.0, threshold=60.0,
    )
    assert state == "prompt"
    assert key == "2"
    assert db.get_open_action_items() == []
```

- [ ] **Step 2: Run all tests**

```bash
cd ~/github/juggle && python -m pytest tests/test_watchdog_integration.py tests/test_watchdog.py tests/test_db_watchdog.py -v --tb=short
```

- [ ] **Step 3: Run full test suite**

```bash
cd ~/github/juggle && python -m pytest tests/ -v --tb=short 2>&1 | tail -30
```

Expected: all new watchdog tests pass; no regressions in existing tests.

- [ ] **Step 4: Commit**

```bash
cd ~/github/juggle
git add tests/test_watchdog_integration.py
git commit -m "test(watchdog): integration smoke — stall→recovery cycle, allowlist no-op, orphaned detection"
```

---

## Task 9: Pre-PR quality gate

- [ ] **Step 1: Invoke mike:pre-pr skill**

Fix all issues surfaced. Do NOT open a PR.

- [ ] **Step 2: Final commit with version bump**

Juggle has no `pyproject.toml` or `VERSION` file — version is tracked in commit messages only. Bump is minor (new feature, non-breaking). Target: v1.22.0.

```bash
cd ~/github/juggle
git commit --allow-empty -m "chore: bump version to v1.22.0 for watchdog feature"
```

---

## Self-Review Against Spec

| Spec requirement | Task |
|---|---|
| watchdog_retried, watchdog_threshold_minutes, model, last_task, busy_since | Task 1 |
| last_send_task_pane_hash, last_send_task_at, last_activity_at | Task 1 (migration 20) |
| last_dispatched_task/role/model on threads | Task 1 (migration 22) |
| agent_completions table + index | Task 1 (migration 21) |
| watchdog_events table | Task 1 (migration 22) |
| get-agent sets busy_since, model | Task 2 |
| send-task stores last_task + pane hash + timestamp | Task 2 |
| complete-agent inserts agent_completions | Task 2 |
| release-agent copies dispatch payload to thread | Task 2 |
| State classifier (working/quiet/prompt/stalled/crashed) | Task 3 |
| Stuck-at-prompt state (4-condition, "stuck" return value) | Task 3 |
| Snapshot helpers (read/write/recovery/prune per-agent) | Task 3 |
| Adaptive threshold (2× median, cold-start defaults) | Task 3 |
| Recovery: decommission + spawn + re-send + retry guard | Task 4 |
| Guard: watchdog_retried==1 → stop | Task 4 |
| Guard: last_task==None → manual action item | Task 4 |
| execute_recovery status recheck (DA-6) | Task 4 |
| Orphaned thread detection as Loop 2 with 24h dedup | Task 4 |
| Daemon poll loop (30s, SIGTERM) | Task 5 |
| last_activity_at DB tracking (not in-memory dict) | Task 5 |
| 2× Enter retry before escalation to aggressive recovery | Task 5 |
| Loop 2 wired into daemon | Task 5 |
| PID file management | Task 5 + 6 |
| cmd_start launches watchdog | Task 6 |
| cmd_stop kills watchdog | Task 6 |
| set-watchdog CLI | Task 7 |
| stop-watchdog CLI (DA-3) | Task 7 |
| watchdog_events telemetry | Task 1 + 4 |
| Snapshot retention (last 100 per agent, DA-4 fix) | Task 3 |
| watchdog_events retention (30d cleanup) | Task 1 + 5 |
| Action items: high for crash/stall, normal for re-dispatch | Task 4 |
| Cockpit notification for prompt auto-resolve | Task 3 |
| Cockpit notification for stuck-at-prompt Enter | Task 5 |
| pre-PR gate | Task 9 |

---

## Revision Log

| Version | Changes |
|---|---|
| v1 | Initial plan (13 tasks) |
| v2 | DA pass on plan execution; merged Tasks 10-13 into Tasks 1-4 (9 tasks). DA findings: missing cmd_release_agent payload copy (→ Task 2), missing stop-watchdog (→ Task 7), snapshot pruning per-agent fix (→ Task 3), execute_recovery status recheck (→ Task 4), retry-blocked test thread status assertion (→ Task 4), orphaned sentinel agent_id fix (→ Task 4), version file note (→ Task 9), send_task return-type caller audit (→ Task 2). |
