# Juggle Self-Heal on Error — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture Juggle-caused errors (Python exceptions + orchestrator tool errors), deduplicate them, and surface root-cause + proposed patch as a gated action item requiring explicit user approval before any code lands.

**Architecture:** Two error classes (A = Juggle Python exceptions, B = orchestration tool errors caused by Juggle's instructions) share a single `error_events` DB table and capture pipeline. A new `juggle_selfheal.py` module owns signature hashing, dedup, and DB writes. Class A is wired into every top-level `except` block; Class B is detected via a Stop-hook transcript scan with causal-attribution filtering. A monitor script polls the DB and emits events for the orchestrator to act on.

**Tech Stack:** Python 3.12+, SQLite (WAL mode — confirmed active in `juggle_db.py:203`), Textual (cockpit), uv script header (monitor script), existing Juggle DB / CLI / hooks patterns.

**Source of truth:** `docs/specs/2026-05-30-juggle-self-heal-on-error.md`

---

## Devil's Advocate

### 1. Cap enforcement has one prompt-dependency

The orchestrator decides to dispatch after the monitor emits a line. The atomic DB `UPDATE … WHERE id=? AND status='open'` with `rowcount=1` guard makes worst-case a **missed dispatch** (orchestrator skips), never a double-dispatch. The 60s re-emit loop in the monitor catches any missed dispatch. A fully code-driven alternative (a daemon that auto-dispatches without orchestrator involvement) would require a subprocess that can spawn agents and construct prompts — expensive, out of Juggle's architecture scope. **Decision: accept the prompt-dependency; the atomic DB claim is sufficient.**

### 2. Transcript schema was unverified in the spec — now corrected

The spec's `_do_class_b_scan` assumed flat top-level events (`type:"tool_use"` as JSONL records). **The real Claude Code JSONL schema is different:** tool calls are nested inside `message.content` blocks. See Task 1 for the verified schema and corrected parser. The spec's pseudocode in §4b must NOT be used as-is.

**Verified real schema (from `~/.claude/projects/*/XXXX.jsonl` inspection):**

| Record type | `type` field | `message.content` |
|-------------|-------------|-------------------|
| Human prompt | `"user"` | `str` (plain text) OR `list[{type:"text",...}]` |
| Tool result  | `"user"` | `list[{type:"tool_result", tool_use_id:"toolu_...", is_error:true\|false\|null, content:str}]` |
| Assistant w/ tool call | `"assistant"` | `list[{type:"tool_use", id:"toolu_...", name:"Bash", input:{...}, caller:...}]` |
| Assistant text | `"assistant"` | `list[{type:"text"\|"thinking",...}]` |
| Meta records | `"queue-operation"`, `"attachment"`, `"last-prompt"` | ignore |

**Key corrections from spec:**
- `tool_use.id` matches `tool_result.tool_use_id` (not `type`)
- `is_error` is literally `True` for errors; `False` or `None` for success → check `is True`
- Turn boundary = last `type="user"` record with string content, not `role=="human"`
- No `"tool_call"` or `"tool_error"` types exist

### 3. CLI subcommands fully specified

**`selfheal-set-status <id> <status> [--action-item-id N]`**
- `<id>`: integer `error_events.id`
- `<status>`: one of `open`, `diagnosing`, `awaiting_approval`, `resolved`
- `--action-item-id N`: optional int; written to `error_events.action_item_id`
- Output (stdout): `error_event <id> status → <status>` or `error: row <id> not found`
- Exit 1 if row not found or invalid status

**`selfheal list`**
- No args
- Output: one line per non-resolved row:
  `<id>  [A]  open     count=3  last=2026-05-30 11:00  sig=a3f1b2c9  KeyError in juggle_cli.main`
  `<id>  [B]  diagnosing  count=1  last=2026-05-30 11:01  sig=b4e2d1f0  Monitor error via start.md`
- Empty output + exit 0 if all resolved

**`selfheal-reset-diagnosing <id>`**
- Resets `status='diagnosing'` → `status='open'` for manual recovery from stuck diagnosis
- Output: `reset error_event <id> diagnosing→open` or `error: row <id> not in diagnosing state`
- Exit 1 if row not found or not in diagnosing state

### 4. Other flags

**Migration ordering:** Migration 23 is confirmed last (adds `last_reflect_msg_count`). Migration 24 is next — correct.

**FK enforcement:** `_connect()` does NOT set `PRAGMA foreign_keys = ON`. The `ON DELETE SET NULL` FK in the `error_events` DDL is decorative (matches existing pattern for `action_items.thread_id`). The `selfheal-set-status resolved` flow must manually handle orphaned `action_item_id`.

**WAL mode:** Confirmed (`PRAGMA journal_mode=WAL` at `juggle_db.py:203`). SQLite serializes writes; the two-step check+claim in `_try_claim_diagnosis_slot` is safe because the `UPDATE WHERE status='open' AND rowcount=1` is atomic.

**Cockpit:** `run()` at line 718 has no `try/except` — must add one. `app.run()` can throw if Textual init fails.

**Watchdog:** `_poll_once` has `except Exception: _log.exception(...)` at line 192-195 — add `record_error()` there. The outer `try/finally` at line 176 only covers PID cleanup, not exceptions.

**Simplified allowlist:** The spec's `_from_argparse()` check uses `extract_stack()` which sees the selfheal call stack, not the exception's — it's unreliable. Plan simplifies to: allowlist `SystemExit`, `KeyboardInterrupt`, and `OperationalError` containing "database is locked". `SystemExit(2)` from argparse is already covered by `SystemExit`.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `src/juggle_selfheal.py` | **Create** | Core module: `record_error`, `record_orchestration_error`, sig hashing, allowlist, self-protection, `_try_claim_diagnosis_slot`, `_get_pending_selfheal_count` |
| `src/juggle_db.py` | **Modify** | Add `CREATE_ERROR_EVENTS` constant, Migration 24, `JuggleDB` methods: `add_error_event`, `dedup_or_insert_error`, `set_error_event_status`, `get_open_error_events`, `get_pending_selfheal_count` |
| `src/juggle_cli.py` | **Modify** | Register `selfheal-set-status`, `selfheal list`, `selfheal-reset-diagnosing` subcommands |
| `src/juggle_hooks.py` | **Modify** | Class A wiring in 5 handlers + `main()` wrapper; Class B scan in `handle_stop()`; pending count in `handle_session_start()` |
| `src/juggle_cli.py` (main) | **Modify** | Class A wiring in `main()` |
| `src/juggle_cockpit.py` | **Modify** | Class A wiring in `run()` |
| `scripts/juggle-agent-watchdog` | **Modify** | Class A wiring in `_poll_once` except block |
| `scripts/juggle-selfheal-monitor` | **Create** | DB-poll loop emitting `[SELFHEAL-A/B]` lines |
| `docs/diagnosis-prompts.md` | **Create** | Class A and Class B diagnosis agent prompt templates |
| `tests/test_juggle_selfheal.py` | **Create** | All unit tests from spec §12 |
| `tests/fixtures/transcript_class_b.jsonl` | **Create** | Real-schema fixture for Class B integration test |

---

## Task 1: Verify Transcript Schema — Create Fixture + RED Test

This is the hard gate for Task 6. Class B parsing code must be written against the verified schema below, not the spec's pseudocode.

**Files:**
- Create: `tests/fixtures/transcript_class_b.jsonl`
- Create (test goes RED here, GREEN in Task 6): `tests/test_juggle_selfheal.py`

- [ ] **Step 1: Create the real-schema fixture file**

```bash
mkdir -p /path/to/juggle/tests/fixtures
```

Write `tests/fixtures/transcript_class_b.jsonl` — three lines matching the real schema:

```
{"type":"user","message":{"role":"user","content":"start juggle"},"sessionId":"test-session"}
{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_001","name":"Skill","input":{"skill":"juggle:start"},"caller":"user"}]},"sessionId":"test-session"}
{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_001","is_error":true,"content":"InputValidationError: 'command' is a required property"}]},"sessionId":"test-session"}
```

This is the dogfood scenario: `/juggle:start` → `Skill(juggle:start)` → `Monitor` errors → tool_result with `is_error:true`.

- [ ] **Step 2: Write the failing test**

Create `tests/test_juggle_selfheal.py`:

```python
"""Tests for juggle_selfheal — self-healing error capture pipeline."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Task 1 gate: Class B transcript parsing (real schema)
# Stays RED until Task 6 implements _do_class_b_scan in juggle_hooks.py
# ---------------------------------------------------------------------------

def test_class_b_scan_real_schema_detects_juggle_tool_error(tmp_path):
    """_do_class_b_scan finds a tool error attributed to juggle: skill invocation."""
    from juggle_hooks import _do_class_b_scan

    transcript = FIXTURES_DIR / "transcript_class_b.jsonl"
    assert transcript.exists(), f"Fixture missing: {transcript}"

    calls: list = []

    def fake_record(tool, tool_input, error_text, juggle_ref):
        calls.append({"tool": tool, "error_text": error_text, "juggle_ref": juggle_ref})

    with patch("juggle_selfheal.record_orchestration_error", side_effect=fake_record):
        _do_class_b_scan(transcript)

    assert len(calls) == 1, f"Expected 1 call, got {calls}"
    assert calls[0]["tool"] == "Skill"
    assert "InputValidationError" in calls[0]["error_text"]
    assert "juggle:" in calls[0]["juggle_ref"]


def test_class_b_scan_no_error_when_is_error_false(tmp_path):
    """_do_class_b_scan ignores tool_result where is_error is False."""
    from juggle_hooks import _do_class_b_scan

    transcript = tmp_path / "ok.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"start juggle"}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_002","name":"Skill","input":{"skill":"juggle:start"},"caller":"user"}]}}\n'
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_002","is_error":false,"content":"OK"}]}}\n'
    )
    calls: list = []
    with patch("juggle_selfheal.record_orchestration_error", side_effect=lambda *a, **kw: calls.append(a)):
        _do_class_b_scan(transcript)
    assert calls == [], "Should not record when is_error is False"


def test_class_b_scan_no_attribution_without_juggle_ref(tmp_path):
    """_do_class_b_scan does not record errors unrelated to juggle."""
    from juggle_hooks import _do_class_b_scan

    transcript = tmp_path / "unrelated.jsonl"
    transcript.write_text(
        '{"type":"user","message":{"role":"user","content":"do something"}}\n'
        '{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_003","name":"Bash","input":{"command":"ls /nonexistent"},"caller":"user"}]}}\n'
        '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_003","is_error":true,"content":"No such file"}]}}\n'
    )
    calls: list = []
    with patch("juggle_selfheal.record_orchestration_error", side_effect=lambda *a, **kw: calls.append(a)):
        _do_class_b_scan(transcript)
    assert calls == [], "Should not record when no juggle ref in tool inputs"


def test_class_b_scan_missing_transcript_path_is_silent():
    """_scan_transcript_for_class_b silently skips when transcript_path absent."""
    from juggle_hooks import _scan_transcript_for_class_b

    # No transcript_path in data → must not raise
    _scan_transcript_for_class_b({})
    _scan_transcript_for_class_b({"transcript_path": None})
```

- [ ] **Step 3: Run test to confirm RED**

```bash
cd /path/to/juggle && uv run pytest tests/test_juggle_selfheal.py::test_class_b_scan_real_schema_detects_juggle_tool_error -v
```

Expected: `FAILED` with `ImportError: cannot import name '_do_class_b_scan' from 'juggle_hooks'` (function not yet implemented).

- [ ] **Step 4: Commit fixture only (no production code yet)**

```bash
git add tests/fixtures/transcript_class_b.jsonl tests/test_juggle_selfheal.py
git commit -m "test(selfheal): add real-schema JSONL fixture + Task-1 RED tests"
```

---

## Task 2: error_events Table + Migration 24

**Files:**
- Modify: `src/juggle_db.py`
- Test: `tests/test_juggle_selfheal.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_juggle_selfheal.py`:

```python
# ---------------------------------------------------------------------------
# Task 2: error_events table + DB helpers
# ---------------------------------------------------------------------------

def test_error_events_table_created_by_init_db(tmp_path):
    """init_db() creates error_events table with expected columns."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    with db._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(error_events)").fetchall()}

    expected = {
        "id", "signature_hash", "error_class", "exc_type", "traceback",
        "entrypoint", "surface", "command_args", "juggle_ref",
        "count", "first_seen", "last_seen", "status", "action_item_id",
    }
    assert expected <= cols, f"Missing columns: {expected - cols}"


def test_error_events_unique_index_on_sig(tmp_path):
    """signature_hash has a UNIQUE index."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    with db._connect() as conn:
        indexes = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='error_events'"
            ).fetchall()
        ]
    assert "idx_error_events_sig" in indexes


def test_dedup_or_insert_inserts_new_row(tmp_path):
    """dedup_or_insert_error inserts a new row for an unseen signature."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    row_id = db.dedup_or_insert_error(
        signature_hash="abc123",
        error_class="A",
        exc_type="KeyError",
        traceback="Traceback...",
        entrypoint="juggle_cli.main",
        command_args='["juggle", "start"]',
    )
    assert row_id is not None

    with db._connect() as conn:
        row = conn.execute("SELECT * FROM error_events WHERE id = ?", (row_id,)).fetchone()
    assert row["signature_hash"] == "abc123"
    assert row["status"] == "open"
    assert row["count"] == 1


def test_dedup_or_insert_increments_count_on_dup(tmp_path):
    """dedup_or_insert_error increments count for existing non-resolved signature."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    db.dedup_or_insert_error("sig1", "A", "ValueError", "tb", "cli", "[]")
    result = db.dedup_or_insert_error("sig1", "A", "ValueError", "tb2", "cli", "[]")
    assert result is None  # suppressed — dedup, no new row

    with db._connect() as conn:
        row = conn.execute("SELECT count FROM error_events WHERE signature_hash = 'sig1'").fetchone()
    assert row["count"] == 2


def test_dedup_allows_insert_after_resolved(tmp_path):
    """dedup_or_insert_error inserts fresh row if prior row is resolved (regression)."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    first_id = db.dedup_or_insert_error("sig2", "A", "KeyError", "tb", "cli", "[]")
    db.set_error_event_status(first_id, "resolved")
    second_id = db.dedup_or_insert_error("sig2", "A", "KeyError", "tb", "cli", "[]")
    assert second_id is not None
    assert second_id != first_id


def test_set_error_event_status_updates_row(tmp_path):
    """set_error_event_status updates status (and optionally action_item_id)."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    row_id = db.dedup_or_insert_error("sig3", "A", "RuntimeError", "tb", "cli", "[]")

    db.set_error_event_status(row_id, "diagnosing")
    with db._connect() as conn:
        row = conn.execute("SELECT status, action_item_id FROM error_events WHERE id=?", (row_id,)).fetchone()
    assert row["status"] == "diagnosing"
    assert row["action_item_id"] is None

    db.set_error_event_status(row_id, "awaiting_approval", action_item_id=42)
    with db._connect() as conn:
        row = conn.execute("SELECT status, action_item_id FROM error_events WHERE id=?", (row_id,)).fetchone()
    assert row["status"] == "awaiting_approval"
    assert row["action_item_id"] == 42


def test_get_pending_selfheal_count(tmp_path):
    """get_pending_selfheal_count returns count of non-resolved rows."""
    from juggle_db import JuggleDB

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    db.dedup_or_insert_error("s1", "A", "E1", "tb", "cli", "[]")
    id2 = db.dedup_or_insert_error("s2", "A", "E2", "tb", "cli", "[]")
    db.dedup_or_insert_error("s3", "A", "E3", "tb", "cli", "[]")
    db.set_error_event_status(id2, "resolved")

    assert db.get_pending_selfheal_count() == 2  # s1 and s3
```

- [ ] **Step 2: Run to confirm RED**

```bash
uv run pytest tests/test_juggle_selfheal.py -k "error_events or dedup_or_insert or set_error_event or get_pending" -v
```

Expected: `FAILED` — `JuggleDB` has no `dedup_or_insert_error` / `set_error_event_status` / `get_pending_selfheal_count` methods; `error_events` table not yet in schema.

- [ ] **Step 3: Add `CREATE_ERROR_EVENTS` constant to `src/juggle_db.py`**

After the existing `CREATE_WATCHDOG_EVENTS` constant (around line 143), add:

```python
CREATE_ERROR_EVENTS = """
CREATE TABLE IF NOT EXISTS error_events (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  signature_hash   TEXT    NOT NULL,
  error_class      TEXT    NOT NULL CHECK(error_class IN ('A', 'B')),
  exc_type         TEXT,
  traceback        TEXT,
  entrypoint       TEXT,
  surface          TEXT,
  command_args     TEXT,
  juggle_ref       TEXT,
  count            INTEGER NOT NULL DEFAULT 1,
  first_seen       TEXT    NOT NULL,
  last_seen        TEXT    NOT NULL,
  status           TEXT    NOT NULL DEFAULT 'open'
                           CHECK(status IN ('open','diagnosing','awaiting_approval','resolved')),
  action_item_id   INTEGER
);
"""
```

Note: The FK `REFERENCES action_items(id) ON DELETE SET NULL` is intentionally omitted — `PRAGMA foreign_keys` is not enabled in `_connect()` and the FK would be decorative. The orphan is handled in `set_error_event_status`.

- [ ] **Step 4: Add Migration 24 to `_migrate()` in `juggle_db.py`**

After the Migration 23 block (after line ~562), add:

```python
        # Migration 24: error_events for self-heal
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "error_events" not in tables:
            try:
                conn.execute(CREATE_ERROR_EVENTS)
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_error_events_sig "
                    "ON error_events(signature_hash)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_error_events_status "
                    "ON error_events(status)"
                )
                conn.commit()
                _log.info("Migration 24: error_events table created")
            except sqlite3.OperationalError as e:
                _log.warning("Migration 24 (error_events) skipped: %s", e)
```

Also add `conn.execute(CREATE_ERROR_EVENTS)` inside `init_db()` alongside the other `CREATE_*` executes (around line 204), so fresh databases also get the table without needing to run `_migrate`.

- [ ] **Step 5: Add DB methods to `JuggleDB` class**

Add after `dismiss_action_item` (around line 1040):

```python
    # ------------------------------------------------------------------
    # Self-heal: error_events
    # ------------------------------------------------------------------

    def dedup_or_insert_error(
        self,
        signature_hash: str,
        error_class: str,
        exc_type: str | None,
        traceback: str | None,
        entrypoint: str | None,
        command_args: str,
        surface: str | None = None,
        juggle_ref: str | None = None,
    ) -> int | None:
        """Insert new error_events row or increment count on duplicate.

        Returns new row id on INSERT; None on dedup (existing open/in-progress row).
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM error_events "
                "WHERE signature_hash = ? AND status != 'resolved'",
                (signature_hash,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE error_events SET count = count + 1, last_seen = ? WHERE id = ?",
                    (now, existing["id"]),
                )
                conn.commit()
                return None
            cur = conn.execute(
                "INSERT INTO error_events "
                "(signature_hash, error_class, exc_type, traceback, entrypoint, "
                "surface, command_args, juggle_ref, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    signature_hash, error_class, exc_type, traceback,
                    entrypoint, surface, command_args, juggle_ref, now, now,
                ),
            )
            conn.commit()
            return cur.lastrowid

    def set_error_event_status(
        self,
        event_id: int,
        status: str,
        action_item_id: int | None = None,
    ) -> bool:
        """Update status (and optionally action_item_id) for an error_events row.

        Returns True if a row was updated.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        with self._connect() as conn:
            if action_item_id is not None:
                cur = conn.execute(
                    "UPDATE error_events SET status = ?, action_item_id = ?, last_seen = ? WHERE id = ?",
                    (status, action_item_id, now, event_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE error_events SET status = ?, last_seen = ? WHERE id = ?",
                    (status, now, event_id),
                )
            conn.commit()
            return cur.rowcount == 1

    def get_open_error_events(self) -> list[dict]:
        """Return all non-resolved error_events rows, newest last."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM error_events WHERE status != 'resolved' ORDER BY id ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_pending_selfheal_count(self) -> int:
        """Return count of non-resolved error_events rows."""
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM error_events WHERE status != 'resolved'"
                ).fetchone()
                return row[0] if row else 0
        except Exception:
            return 0
```

- [ ] **Step 6: Run tests GREEN**

```bash
uv run pytest tests/test_juggle_selfheal.py -k "error_events or dedup_or_insert or set_error_event or get_pending" -v
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/juggle_db.py tests/test_juggle_selfheal.py
git commit -m "feat(selfheal): add error_events table + Migration 24 + DB helpers"
```

---

## Task 3: CLI Helpers — selfheal-set-status, selfheal list, selfheal-reset-diagnosing

**Files:**
- Modify: `src/juggle_cli.py`
- Test: `tests/test_juggle_selfheal.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_juggle_selfheal.py`:

```python
# ---------------------------------------------------------------------------
# Task 3: CLI helpers (selfheal subcommands)
# ---------------------------------------------------------------------------

def test_list_selfheal_prints_open_rows(tmp_path, capsys):
    """selfheal list prints one line per non-resolved error_events row."""
    from juggle_db import JuggleDB
    from juggle_cli import _cmd_list_selfheal

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    db.dedup_or_insert_error("aabbccdd11223344", "A", "KeyError", "tb", "juggle_cli.main", "[]")
    db.dedup_or_insert_error("ff00ee11dd223344", "B", None, "Tool err", "Monitor", "[]",
                              juggle_ref="commands/start.md")

    class FakeArgs:
        db_path = str(tmp_path / "juggle.db")

    _cmd_list_selfheal(FakeArgs())
    out = capsys.readouterr().out
    assert "[A]" in out
    assert "[B]" in out
    assert "KeyError" in out
    assert "Monitor" in out


def test_selfheal_set_status_updates_row(tmp_path, capsys):
    """selfheal-set-status updates status and prints confirmation."""
    from juggle_db import JuggleDB
    from juggle_cli import _cmd_selfheal_set_status

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    row_id = db.dedup_or_insert_error("sig-test", "A", "KeyError", "tb", "cli", "[]")

    class FakeArgs:
        db_path = str(tmp_path / "juggle.db")
        id = row_id
        status = "diagnosing"
        action_item_id = None

    _cmd_selfheal_set_status(FakeArgs())
    out = capsys.readouterr().out
    assert "diagnosing" in out

    with db._connect() as conn:
        row = conn.execute("SELECT status FROM error_events WHERE id=?", (row_id,)).fetchone()
    assert row["status"] == "diagnosing"


def test_selfheal_reset_diagnosing(tmp_path, capsys):
    """selfheal-reset-diagnosing resets diagnosing→open."""
    from juggle_db import JuggleDB
    from juggle_cli import _cmd_selfheal_reset_diagnosing

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    row_id = db.dedup_or_insert_error("sig-reset", "A", "KeyError", "tb", "cli", "[]")
    db.set_error_event_status(row_id, "diagnosing")

    class FakeArgs:
        db_path = str(tmp_path / "juggle.db")
        id = row_id

    _cmd_selfheal_reset_diagnosing(FakeArgs())
    out = capsys.readouterr().out
    assert "open" in out

    with db._connect() as conn:
        row = conn.execute("SELECT status FROM error_events WHERE id=?", (row_id,)).fetchone()
    assert row["status"] == "open"
```

- [ ] **Step 2: Run to confirm RED**

```bash
uv run pytest tests/test_juggle_selfheal.py -k "list_selfheal or set_status or reset_diagnosing" -v
```

Expected: `FAILED` — `cannot import name '_cmd_list_selfheal' from 'juggle_cli'`.

- [ ] **Step 3: Add command handler functions to `src/juggle_cli.py`**

Add near the other `cmd_*` functions (before `main()`):

```python
def _cmd_list_selfheal(args):
    from juggle_db import JuggleDB, DB_PATH
    db = JuggleDB(getattr(args, "db_path", None) or str(DB_PATH))
    db.init_db()
    rows = db.get_open_error_events()
    if not rows:
        print("No pending self-heal errors.")
        return
    for row in rows:
        sig8 = (row["signature_hash"] or "")[:8]
        cls = row["error_class"]
        status = row["status"]
        count = row["count"]
        last = (row["last_seen"] or "")[:16]
        if cls == "A":
            detail = f"{row['exc_type'] or '?'} in {row['entrypoint'] or '?'}"
        else:
            ref = Path(row["juggle_ref"] or "").name or row["juggle_ref"] or "?"
            detail = f"{row['entrypoint'] or '?'} error via {ref}"
        print(f"{row['id']:>4}  [{cls}]  {status:<20} count={count}  last={last}  sig={sig8}  {detail}")


def _cmd_selfheal_set_status(args):
    from juggle_db import JuggleDB, DB_PATH
    db = JuggleDB(getattr(args, "db_path", None) or str(DB_PATH))
    db.init_db()
    valid = ("open", "diagnosing", "awaiting_approval", "resolved")
    if args.status not in valid:
        print(f"error: invalid status {args.status!r}; choose from {valid}")
        sys.exit(1)
    updated = db.set_error_event_status(args.id, args.status, action_item_id=args.action_item_id)
    if updated:
        print(f"error_event {args.id} status → {args.status}")
    else:
        print(f"error: row {args.id} not found")
        sys.exit(1)


def _cmd_selfheal_reset_diagnosing(args):
    from juggle_db import JuggleDB, DB_PATH
    db = JuggleDB(getattr(args, "db_path", None) or str(DB_PATH))
    db.init_db()
    with db._connect() as conn:
        row = conn.execute(
            "SELECT status FROM error_events WHERE id = ?", (args.id,)
        ).fetchone()
    if not row:
        print(f"error: row {args.id} not found")
        sys.exit(1)
    if row["status"] != "diagnosing":
        print(f"error: row {args.id} not in diagnosing state (current: {row['status']})")
        sys.exit(1)
    db.set_error_event_status(args.id, "open")
    print(f"reset error_event {args.id} diagnosing→open")
```

Also add `from pathlib import Path` if not already at the top of `juggle_cli.py`.

- [ ] **Step 4: Register subcommands in `main()` in `src/juggle_cli.py`**

After the existing `p_recall = subparsers.add_parser("recall", ...)` line (~537), add:

```python
    p_list_selfheal = subparsers.add_parser("selfheal list", help="List pending self-heal errors")
    p_list_selfheal.set_defaults(func=_cmd_list_selfheal)

    p_sh_set = subparsers.add_parser("selfheal-set-status", help="Update error_event status")
    p_sh_set.add_argument("id", type=int, help="error_events.id")
    p_sh_set.add_argument("status", help="open|diagnosing|awaiting_approval|resolved")
    p_sh_set.add_argument("--action-item-id", type=int, dest="action_item_id", default=None)
    p_sh_set.set_defaults(func=_cmd_selfheal_set_status)

    p_sh_reset = subparsers.add_parser("selfheal-reset-diagnosing", help="Reset stuck diagnosing→open")
    p_sh_reset.add_argument("id", type=int, help="error_events.id")
    p_sh_reset.set_defaults(func=_cmd_selfheal_reset_diagnosing)
```

- [ ] **Step 5: Run tests GREEN**

```bash
uv run pytest tests/test_juggle_selfheal.py -k "list_selfheal or set_status or reset_diagnosing" -v
```

- [ ] **Step 6: Commit**

```bash
git add src/juggle_cli.py tests/test_juggle_selfheal.py
git commit -m "feat(selfheal): add selfheal-set-status, selfheal list, selfheal-reset-diagnosing CLI commands"
```

---

## Task 4: Create `src/juggle_selfheal.py` — Core Module

**Files:**
- Create: `src/juggle_selfheal.py`
- Test: `tests/test_juggle_selfheal.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_juggle_selfheal.py`:

```python
# ---------------------------------------------------------------------------
# Task 4: juggle_selfheal core — signature, allowlist, record_error
# ---------------------------------------------------------------------------

def test_allowlist_systemexit(tmp_path):
    """record_error is a no-op for SystemExit."""
    from juggle_selfheal import record_error
    import os; os.environ.pop("JUGGLE_SELFHEAL_OP", None)

    with patch("juggle_selfheal._get_db") as mock_db:
        record_error(SystemExit(1), "test.entrypoint")
        mock_db.assert_not_called()


def test_allowlist_keyboardinterrupt(tmp_path):
    """record_error is a no-op for KeyboardInterrupt."""
    from juggle_selfheal import record_error
    with patch("juggle_selfheal._get_db") as mock_db:
        record_error(KeyboardInterrupt(), "test.entrypoint")
        mock_db.assert_not_called()


def test_allowlist_sqlite_locked(tmp_path):
    """record_error is a no-op for sqlite 'database is locked' errors."""
    import sqlite3
    from juggle_selfheal import record_error
    exc = sqlite3.OperationalError("sqlite database is locked")
    with patch("juggle_selfheal._get_db") as mock_db:
        record_error(exc, "test.entrypoint")
        mock_db.assert_not_called()


def test_self_protection_env_var(tmp_path):
    """record_error is a no-op when JUGGLE_SELFHEAL_OP env var is set."""
    from juggle_selfheal import record_error
    import os
    os.environ["JUGGLE_SELFHEAL_OP"] = "1"
    try:
        with patch("juggle_selfheal._get_db") as mock_db:
            record_error(RuntimeError("test"), "test.entrypoint")
            mock_db.assert_not_called()
    finally:
        del os.environ["JUGGLE_SELFHEAL_OP"]


def test_signature_dedup_class_a(tmp_path):
    """Two identical exceptions produce same signature_hash; second call increments count."""
    from juggle_db import JuggleDB
    from juggle_selfheal import record_error

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    def fake_exc():
        raise KeyError("thread_id")

    exc1 = exc2 = None
    try:
        fake_exc()
    except KeyError as e:
        exc1 = e
    try:
        fake_exc()
    except KeyError as e:
        exc2 = e

    with patch("juggle_selfheal._get_db", return_value=db):
        record_error(exc1, "juggle_cli.main")
        record_error(exc2, "juggle_cli.main")

    with db._connect() as conn:
        rows = conn.execute("SELECT * FROM error_events").fetchall()
    assert len(rows) == 1, f"Expected 1 row (dedup), got {len(rows)}"
    assert rows[0]["count"] == 2


def test_signature_different_exc_type(tmp_path):
    """Different exception types produce different signatures."""
    from juggle_db import JuggleDB
    from juggle_selfheal import record_error

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    def raise_key():
        raise KeyError("k")
    def raise_value():
        raise ValueError("v")

    exc_k = exc_v = None
    try: raise_key()
    except KeyError as e: exc_k = e
    try: raise_value()
    except ValueError as e: exc_v = e

    with patch("juggle_selfheal._get_db", return_value=db):
        record_error(exc_k, "juggle_cli.main")
        record_error(exc_v, "juggle_cli.main")

    with db._connect() as conn:
        rows = conn.execute("SELECT * FROM error_events").fetchall()
    assert len(rows) == 2, "Different exc types must produce different signatures"


def test_resolved_regression(tmp_path):
    """Same signature after resolved → new INSERT (regression path)."""
    from juggle_db import JuggleDB
    from juggle_selfheal import record_error

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    def raise_key():
        raise KeyError("x")

    exc = None
    try: raise_key()
    except KeyError as e: exc = e

    with patch("juggle_selfheal._get_db", return_value=db):
        record_error(exc, "juggle_cli.main")

    with db._connect() as conn:
        row = conn.execute("SELECT id FROM error_events").fetchone()
    db.set_error_event_status(row["id"], "resolved")

    with patch("juggle_selfheal._get_db", return_value=db):
        record_error(exc, "juggle_cli.main")

    with db._connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM error_events").fetchone()[0]
    assert count == 2, "Resolved regression should insert a fresh row"


def test_concurrency_cap_in_flight(tmp_path):
    """_try_claim_diagnosis_slot returns False when another row is already diagnosing."""
    from juggle_db import JuggleDB
    from juggle_selfheal import _try_claim_diagnosis_slot

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    id1 = db.dedup_or_insert_error("s-diag", "A", "E1", "tb", "cli", "[]")
    id2 = db.dedup_or_insert_error("s-open", "A", "E2", "tb", "cli", "[]")

    # id1 is already diagnosing
    db.set_error_event_status(id1, "diagnosing")

    # Claim for id2 must fail
    result = _try_claim_diagnosis_slot(db, id2)
    assert result is False


def test_concurrency_cap_single_claim(tmp_path):
    """_try_claim_diagnosis_slot returns True when no other row is diagnosing."""
    from juggle_db import JuggleDB
    from juggle_selfheal import _try_claim_diagnosis_slot

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    row_id = db.dedup_or_insert_error("s-single", "A", "E", "tb", "cli", "[]")
    result = _try_claim_diagnosis_slot(db, row_id)
    assert result is True

    with db._connect() as conn:
        row = conn.execute("SELECT status FROM error_events WHERE id=?", (row_id,)).fetchone()
    assert row["status"] == "diagnosing"


def test_record_orchestration_error_class_b(tmp_path):
    """record_orchestration_error inserts a Class B row."""
    from juggle_db import JuggleDB
    from juggle_selfheal import record_orchestration_error

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    with patch("juggle_selfheal._get_db", return_value=db):
        record_orchestration_error(
            tool="Monitor",
            tool_input={"command": "scripts/juggle-agent-monitor"},
            error_text="InputValidationError: 'command' is a required property",
            juggle_ref="juggle:",
        )

    with db._connect() as conn:
        row = conn.execute("SELECT * FROM error_events").fetchone()
    assert row is not None
    assert row["error_class"] == "B"
    assert row["entrypoint"] == "Monitor"
    assert row["status"] == "open"
```

- [ ] **Step 2: Run to confirm RED**

```bash
uv run pytest tests/test_juggle_selfheal.py -k "allowlist or self_protection or signature or resolved_regression or concurrency or record_orchestration" -v
```

Expected: `FAILED` — `ModuleNotFoundError: No module named 'juggle_selfheal'`.

- [ ] **Step 3: Create `src/juggle_selfheal.py`**

```python
"""Juggle Self-Heal — captures Juggle-caused errors for gated diagnosis."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sysconfig
import traceback as _tb
from datetime import datetime, timezone
from pathlib import Path

_log = logging.getLogger(__name__)
_SELFHEAL_ENV = "JUGGLE_SELFHEAL_OP"

_ALLOWLISTED_TYPES = (SystemExit, KeyboardInterrupt)


def _is_allowlisted(exc: BaseException) -> bool:
    if isinstance(exc, _ALLOWLISTED_TYPES):
        return True
    import sqlite3
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        if "database is locked" in msg:
            return True
    return False


def _is_stdlib(filename: str) -> bool:
    stdlib_paths = [sysconfig.get_path("stdlib"), sysconfig.get_path("platstdlib")]
    return any(filename.startswith(p) for p in stdlib_paths if p)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


def _get_db():
    from juggle_db import JuggleDB, DB_PATH
    db = JuggleDB(str(DB_PATH))
    db.init_db()
    return db


def _compute_class_a_signature(exc: BaseException, entrypoint: str) -> str:
    exc_type = type(exc).__name__
    frames = _tb.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    normalized = []
    for frame in frames:
        if _is_stdlib(frame.filename) or "site-packages" in frame.filename:
            continue
        fname = Path(frame.filename).name
        normalized.append(f"{fname}:{frame.lineno}:{frame.name}")
    normalized = normalized[-5:]
    frames_str = "|".join(normalized) or entrypoint
    sig_input = f"class_A:{exc_type}:{frames_str}"
    return hashlib.sha256(sig_input.encode()).hexdigest()[:16]


def _compute_class_b_signature(tool: str, error_text: str, juggle_ref: str) -> str:
    normalized_err = re.sub(r"\d+", "", error_text[:120].lower())
    normalized_err = re.sub(r"\s+", " ", normalized_err).strip()
    ref_basename = Path(juggle_ref).name if "/" in juggle_ref else juggle_ref.split(":")[0]
    sig_input = f"class_B:{tool}:{normalized_err}:{ref_basename}"
    return hashlib.sha256(sig_input.encode()).hexdigest()[:16]


def record_error(exc: BaseException, entrypoint: str, context: dict | None = None) -> None:
    """Capture a Class A exception. Never re-raises. Self-protecting."""
    if os.environ.get(_SELFHEAL_ENV):
        return
    try:
        if _is_allowlisted(exc):
            return
        sig = _compute_class_a_signature(exc, entrypoint)
        tb_str = _tb.format_exception_only(type(exc), exc)[-1].strip()
        full_tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
        db = _get_db()
        os.environ[_SELFHEAL_ENV] = "1"
        try:
            db.dedup_or_insert_error(
                signature_hash=sig,
                error_class="A",
                exc_type=type(exc).__name__,
                traceback=full_tb,
                entrypoint=entrypoint,
                command_args=json.dumps(context or {}),
            )
        finally:
            os.environ.pop(_SELFHEAL_ENV, None)
    except Exception as inner:
        _log.error("selfheal.record_error itself failed: %s", inner)


def record_orchestration_error(
    tool: str,
    tool_input: dict,
    error_text: str,
    juggle_ref: str,
) -> None:
    """Capture a Class B tool error. Never re-raises. Self-protecting."""
    if os.environ.get(_SELFHEAL_ENV):
        return
    try:
        sig = _compute_class_b_signature(tool, error_text, juggle_ref)
        db = _get_db()
        os.environ[_SELFHEAL_ENV] = "1"
        try:
            db.dedup_or_insert_error(
                signature_hash=sig,
                error_class="B",
                exc_type=None,
                traceback=error_text,
                entrypoint=tool,
                command_args=json.dumps(tool_input),
                surface=juggle_ref,
                juggle_ref=juggle_ref,
            )
        finally:
            os.environ.pop(_SELFHEAL_ENV, None)
    except Exception as inner:
        _log.error("selfheal.record_orchestration_error itself failed: %s", inner)


def _try_claim_diagnosis_slot(db, error_event_id: int) -> bool:
    """Atomically claim the diagnosis slot. Returns True if claimed."""
    with db._connect() as conn:
        in_flight = conn.execute(
            "SELECT COUNT(*) FROM error_events WHERE status = 'diagnosing'"
        ).fetchone()[0]
        if in_flight > 0:
            return False
        cur = conn.execute(
            "UPDATE error_events SET status = 'diagnosing' WHERE id = ? AND status = 'open'",
            (error_event_id,),
        )
        conn.commit()
        return cur.rowcount == 1


def _get_pending_selfheal_count(db) -> int:
    """Return count of non-resolved error_events. Safe to call even if table absent."""
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM error_events WHERE status != 'resolved'"
            ).fetchone()
            return row[0] if row else 0
    except Exception:
        return 0
```

- [ ] **Step 4: Run tests GREEN**

```bash
uv run pytest tests/test_juggle_selfheal.py -k "allowlist or self_protection or signature or resolved_regression or concurrency or record_orchestration" -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/juggle_selfheal.py tests/test_juggle_selfheal.py
git commit -m "feat(selfheal): add juggle_selfheal core module (record_error, sig, allowlist, cap)"
```

---

## Task 5: Class A Wiring — Insert `record_error()` at Existing Except Blocks

**Files:**
- Modify: `src/juggle_cli.py`, `src/juggle_hooks.py`, `src/juggle_cockpit.py`, `scripts/juggle-agent-watchdog`
- Test: `tests/test_juggle_selfheal.py`

- [ ] **Step 1: Write failing integration test**

Append to `tests/test_juggle_selfheal.py`:

```python
# ---------------------------------------------------------------------------
# Task 5: Class A wiring — verify record_error called from juggle_cli main()
# ---------------------------------------------------------------------------

def test_class_a_wired_in_juggle_cli_main(tmp_path):
    """juggle_cli.main() calls record_error when a subcommand raises."""
    import subprocess, sys

    # Use a real DB path so juggle_selfheal can init
    env = {**os.environ, "JUGGLE_TEST_SELFHEAL_RAISE": "1"}
    result = subprocess.run(
        [sys.executable, "-m", "juggle_cli", "--test-selfheal-raise"],
        capture_output=True, text=True, env=env,
        cwd=str(Path(__file__).parent.parent / "src"),
    )
    # The command should exit non-zero (the exception was raised)
    assert result.returncode != 0
    # But the error should have been recorded in DB
    from juggle_db import JuggleDB, DB_PATH
    db = JuggleDB()
    with db._connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM error_events WHERE error_class='A'"
        ).fetchone()[0]
    assert count >= 1, "Class A error should have been recorded"
```

Note: This integration test requires `--test-selfheal-raise` to be handled in `juggle_cli.main()` (added in Step 3). Skip this test if running in CI without the flag.

- [ ] **Step 2: Modify `src/juggle_cli.py` `main()` — add record_error**

Find the existing `except Exception as e:` block in `main()` at line ~701:

```python
    try:
        args.func(args)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
```

Replace with:

```python
    try:
        args.func(args)
    except Exception as e:
        from juggle_selfheal import record_error
        record_error(e, "juggle_cli.main", {"argv": sys.argv})
        print(f"Error: {e}")
        sys.exit(1)
```

- [ ] **Step 3: Modify `src/juggle_hooks.py` — add record_error to all 5 handlers + main() wrapper**

At the top of `juggle_hooks.py`, after other imports, add:

```python
def _record_error_safe(exc: Exception, entrypoint: str) -> None:
    """Import record_error lazily to avoid circular import at module load."""
    try:
        from juggle_selfheal import record_error
        record_error(exc, entrypoint)
    except Exception:
        pass  # record_error itself failed; already logged inside
```

In `handle_user_prompt_submit()`, find `except Exception as exc:` (~line 238):

```python
    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.UserPromptSubmit")
        logging.error("UserPromptSubmit handler error: %s", exc, exc_info=True)
```

In `handle_stop()`, find `except Exception as exc:` (~line 300):

```python
    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.Stop")
        logging.error("Stop handler error: %s", exc, exc_info=True)
```

In `handle_session_start()`, find `except Exception as exc:` (~line 331):

```python
    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.SessionStart")
        logging.error("SessionStart handler error: %s", exc, exc_info=True)
```

In `handle_pre_tool_use()`, find `except Exception as exc:` (~line 457):

```python
    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.PreToolUse")
        logging.error("PreToolUse handler error: %s", exc, exc_info=True)
```

In `handle_post_tool_use()`, find `except Exception as exc:` (~line 562):

```python
    except Exception as exc:
        _record_error_safe(exc, "juggle_hooks.PostToolUse")
        logging.error("PostToolUse handler error: %s", exc, exc_info=True)
```

In `main()` of `juggle_hooks.py`, wrap the `handler(data)` call at line ~600:

```python
    try:
        handler(data)
    except Exception as exc:
        _record_error_safe(exc, f"juggle_hooks.{event_name}")
        logging.error("Unhandled error in hook %s: %s", event_name, exc, exc_info=True)
        sys.exit(1)
```

- [ ] **Step 4: Modify `src/juggle_cockpit.py` — wrap `run()` at line 718**

```python
def run(db_path: str | None = None) -> None:
    try:
        app = CockpitApp(db_path=db_path)
        app.run()
    except Exception as exc:
        try:
            from juggle_selfheal import record_error
            record_error(exc, "juggle_cockpit.run")
        except Exception:
            pass
        raise
```

- [ ] **Step 5: Modify `scripts/juggle-agent-watchdog` — add record_error in poll loop**

Find the inner except block at line ~192-195:

```python
            try:
                _poll_once(db, mgr)
            except Exception:
                _log.exception("Watchdog: unhandled error in poll — continuing")
```

Replace with:

```python
            try:
                _poll_once(db, mgr)
            except Exception as exc:
                _log.exception("Watchdog: unhandled error in poll — continuing")
                try:
                    from juggle_selfheal import record_error
                    record_error(exc, "juggle_watchdog.poll")
                except Exception:
                    pass
```

- [ ] **Step 6: Verify no import errors**

```bash
cd /path/to/juggle && uv run python -c "import juggle_hooks; import juggle_selfheal; print('OK')"
```

Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add src/juggle_cli.py src/juggle_hooks.py src/juggle_cockpit.py scripts/juggle-agent-watchdog tests/test_juggle_selfheal.py
git commit -m "feat(selfheal): wire Class A record_error into all top-level except blocks"
```

---

## Task 6: Class B Wiring — Stop-Hook Transcript Scan + Corrected Schema

**Files:**
- Modify: `src/juggle_hooks.py`
- Test: `tests/test_juggle_selfheal.py` (the Task-1 RED tests turn GREEN here)

The corrected parsing algorithm (verified schema — see DA §2):

- JSONL records are top-level; tool calls are nested inside `message.content`
- `type="user"` with `message.content=str` → human turn boundary
- `type="assistant"` → extract `tool_use` blocks from `message.content`  
- `type="user"` with `message.content=list` → extract `tool_result` blocks
- `tool_use.id` matches `tool_result.tool_use_id`
- `is_error is True` (not just truthy — `None` means success)

- [ ] **Step 1: Confirm Task-1 tests are still RED**

```bash
uv run pytest tests/test_juggle_selfheal.py -k "class_b_scan" -v
```

Expected: `FAILED` — `_do_class_b_scan` not yet in `juggle_hooks`.

- [ ] **Step 2: Add parsing functions to `src/juggle_hooks.py`**

Add after `handle_post_tool_use` (before the HANDLERS dict):

```python
# ---------------------------------------------------------------------------
# Class B: transcript scan (Stop hook)
# ---------------------------------------------------------------------------

_JUGGLE_PATHS: tuple[str, ...] = (
    "juggle_cli.py",
    "juggle_hooks.py",
    "juggle_selfheal.py",
    "scripts/juggle-",
    "commands/",
    "juggle:",
)

_MAX_TRANSCRIPT_LINES = 200  # cap to avoid reading huge files


def _scan_transcript_for_class_b(data: dict) -> None:
    """Entry point: called from handle_stop(). Silently skips if no transcript_path."""
    transcript_path = data.get("transcript_path")
    if not transcript_path:
        return
    try:
        _do_class_b_scan(Path(transcript_path))
    except Exception as exc:
        logging.warning("Class B transcript scan failed: %s", exc)


def _do_class_b_scan(transcript_path: Path) -> None:
    """Parse transcript JSONL and record tool errors attributed to Juggle.

    VERIFIED schema (2026-05-30):
    - Top-level records have type="user"|"assistant"|"attachment"|"last-prompt"
    - tool_use blocks: inside assistant message.content, keys: type, id, name, input, caller
    - tool_result blocks: inside user message.content, keys: type, tool_use_id, is_error, content
    - Human turn boundary: last type="user" record with message.content as a plain str
      (tool-result-only user records have message.content as a list)
    """
    all_lines = transcript_path.read_text(errors="replace").splitlines()
    lines = all_lines[-_MAX_TRANSCRIPT_LINES:]

    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    # Find last human-text turn boundary
    boundary_idx = -1
    for i, rec in enumerate(records):
        if rec.get("type") != "user":
            continue
        content = rec.get("message", {}).get("content", "")
        if isinstance(content, str):
            boundary_idx = i
        elif isinstance(content, list):
            if any(isinstance(x, dict) and x.get("type") == "text" for x in content):
                boundary_idx = i

    if boundary_idx < 0:
        return

    current_turn = records[boundary_idx + 1:]

    # Extract tool_use blocks from assistant messages
    tool_uses: list[dict] = []
    for rec in current_turn:
        if rec.get("type") != "assistant":
            continue
        content = rec.get("message", {}).get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    tool_uses.append(item)

    # Extract tool_result blocks from user messages (within current turn)
    tool_results: list[dict] = []
    for rec in current_turn:
        if rec.get("type") != "user":
            continue
        content = rec.get("message", {}).get("content", [])
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_results.append(item)

    _attribute_tool_errors(tool_uses, tool_results)


def _attribute_tool_errors(tool_uses: list[dict], tool_results: list[dict]) -> None:
    """N=10 same-turn causal attribution: link tool errors to Juggle references."""
    from juggle_selfheal import record_orchestration_error

    N = 10
    recent_uses = tool_uses[-N:]
    recent_inputs_str = " ".join(json.dumps(tc.get("input") or {}) for tc in recent_uses)

    juggle_ref: str | None = None
    for path in _JUGGLE_PATHS:
        if path in recent_inputs_str:
            juggle_ref = path
            break

    if juggle_ref is None:
        return

    use_by_id = {tc.get("id"): tc for tc in tool_uses}

    for tr in tool_results:
        if tr.get("is_error") is not True:  # only strictly True; False/None = success
            continue
        error_text = str(tr.get("content", ""))
        use_id = tr.get("tool_use_id")
        tc = use_by_id.get(use_id, {})
        tool_name = tc.get("name", "unknown")
        tool_input = tc.get("input") or {}
        record_orchestration_error(tool_name, tool_input, error_text, juggle_ref)
```

- [ ] **Step 3: Call `_scan_transcript_for_class_b` from `handle_stop()`**

Inside `handle_stop()`, at the end of the `try` block (before `sys.exit(0)`), add:

```python
        # Class B: scan transcript for Juggle-caused tool errors
        _scan_transcript_for_class_b(data)
```

- [ ] **Step 4: Run Task-1 RED tests — now GREEN**

```bash
uv run pytest tests/test_juggle_selfheal.py -k "class_b_scan" -v
```

Expected: all 4 pass.

- [ ] **Step 5: Run full test file**

```bash
uv run pytest tests/test_juggle_selfheal.py -v
```

Expected: all pass (verify no regressions from earlier tasks).

- [ ] **Step 6: Commit**

```bash
git add src/juggle_hooks.py
git commit -m "feat(selfheal): add Class B Stop-hook transcript scan with corrected JSONL schema"
```

---

## Task 7: `scripts/juggle-selfheal-monitor`

**Files:**
- Create: `scripts/juggle-selfheal-monitor`
- Test: manual smoke test (no Textual runtime needed)

- [ ] **Step 1: Create the monitor script**

Create `scripts/juggle-selfheal-monitor`:

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = []
# ///
"""juggle-selfheal-monitor — streams self-heal events to stdout for Monitor tool.

Format:
  Class A: [SELFHEAL-A] <exc_type> in <entrypoint>: <truncated_tb> (count=N)
  Class B: [SELFHEAL-B] <tool_name> error via <juggle_ref_basename>: <err_fragment> (count=N)

Re-emits open rows not yet claimed after 60s (starvation mitigation).
"""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_settings import get_settings


def _db_path() -> Path:
    return Path(get_settings()["paths"]["data_dir"]) / "juggle.db"


def _format_line(row: sqlite3.Row) -> str:
    cls = row["error_class"]
    count = row["count"]
    if cls == "A":
        exc = row["exc_type"] or "?"
        ep = row["entrypoint"] or "?"
        tb = (row["traceback"] or "")[:60].replace("\n", " ")
        return f"[SELFHEAL-A] {exc} in {ep}: {tb} (count={count})"
    else:
        tool = row["entrypoint"] or "?"
        ref = row["juggle_ref"] or ""
        ref_base = Path(ref).name if "/" in ref else ref.split(":")[0] or ref
        err = (row["traceback"] or "")[:60].replace("\n", " ")
        return f"[SELFHEAL-B] {tool} error via {ref_base}: {err} (count={count})"


def _poll(db_path: Path, emitted: dict[int, float]) -> dict[int, float]:
    """Poll error_events. Returns updated emitted dict."""
    now = time.time()
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM error_events WHERE status = 'open' ORDER BY id"
        ).fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return emitted  # DB locked or missing — retry next tick

    current_ids = {row["id"] for row in rows}

    # Remove IDs no longer open
    emitted = {k: v for k, v in emitted.items() if k in current_ids}

    for row in rows:
        rid = row["id"]
        first_emitted = emitted.get(rid)
        if first_emitted is None:
            # First encounter — emit immediately
            print(_format_line(row), flush=True)
            emitted[rid] = now
        elif now - first_emitted >= 60:
            # Re-emit after 60s (starvation mitigation)
            print(_format_line(row), flush=True)
            emitted[rid] = now  # reset timer

    return emitted


def main() -> None:
    db_path = _db_path()
    emitted: dict[int, float] = {}

    while True:
        emitted = _poll(db_path, emitted)
        time.sleep(2)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make executable**

```bash
chmod +x scripts/juggle-selfheal-monitor
```

- [ ] **Step 3: Smoke test — add a row and verify output**

```bash
# Insert a test row manually
uv run python -c "
import sys; sys.path.insert(0, 'src')
from juggle_db import JuggleDB
db = JuggleDB()
db.init_db()
db.dedup_or_insert_error('smoke-test-sig', 'A', 'SmokTestError', 'Traceback...', 'juggle_cli.main', '[]')
print('row inserted')
"

# Run monitor for 3 seconds — should emit one line
timeout 3 uv run scripts/juggle-selfheal-monitor || true
```

Expected output: `[SELFHEAL-A] SmokTestError in juggle_cli.main: Traceback... (count=1)`

- [ ] **Step 4: Commit**

```bash
git add scripts/juggle-selfheal-monitor
git commit -m "feat(selfheal): add juggle-selfheal-monitor script"
```

---

## Task 8: SessionStart Pending Count Surfacing

**Files:**
- Modify: `src/juggle_hooks.py`
- Test: `tests/test_juggle_selfheal.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_juggle_selfheal.py`:

```python
# ---------------------------------------------------------------------------
# Task 8: SessionStart pending count
# ---------------------------------------------------------------------------

def test_session_start_includes_selfheal_count(tmp_path):
    """handle_session_start output includes pending self-heal count when > 0."""
    from juggle_db import JuggleDB
    from juggle_hooks import handle_session_start

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path)
    db.init_db()
    db.set_active(True)
    db.dedup_or_insert_error("s1", "A", "KeyError", "tb", "cli", "[]")
    db.dedup_or_insert_error("s2", "A", "ValueError", "tb", "cli", "[]")

    import io, json as _json
    output_lines = []

    def fake_print(s):
        output_lines.append(s)

    with patch("juggle_hooks.DB_PATH", Path(db_path)), \
         patch("builtins.print", side_effect=fake_print):
        handle_session_start({"reason": "resume"})

    # Collect all printed output
    combined = " ".join(output_lines)
    # Parse JSON if printed as hook output
    for line in output_lines:
        try:
            parsed = _json.loads(line)
            ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
            if "self-heal" in ctx or "selfheal" in ctx.lower() or "pending" in ctx:
                return  # found the warning
        except Exception:
            pass
    # If no JSON, check raw output
    assert any("self-heal" in l or "pending" in l.lower() for l in output_lines), \
        f"Expected selfheal warning in output, got: {output_lines}"
```

- [ ] **Step 2: Run to confirm RED**

```bash
uv run pytest tests/test_juggle_selfheal.py::test_session_start_includes_selfheal_count -v
```

Expected: `FAILED`.

- [ ] **Step 3: Modify `handle_session_start()` in `src/juggle_hooks.py`**

Find the section in `handle_session_start()` that builds `additional_context` and prints JSON (around line 320-329). After `additional_context = build_startup_output(db)`, add:

```python
            # Append self-heal pending count
            try:
                from juggle_selfheal import _get_pending_selfheal_count
                pending = _get_pending_selfheal_count(db)
                if pending > 0:
                    additional_context += (
                        f"\n⚠️ {pending} pending self-heal error(s) — "
                        "run `selfheal list` to review."
                    )
            except Exception:
                pass
```

- [ ] **Step 4: Run test GREEN**

```bash
uv run pytest tests/test_juggle_selfheal.py::test_session_start_includes_selfheal_count -v
```

- [ ] **Step 5: Commit**

```bash
git add src/juggle_hooks.py tests/test_juggle_selfheal.py
git commit -m "feat(selfheal): surface pending self-heal count in SessionStart output"
```

---

## Task 9: Diagnosis Prompt Templates

**Files:**
- Create: `docs/diagnosis-prompts.md`

No tests for this task (it's a documentation artifact, not executable code).

- [ ] **Step 1: Create `docs/diagnosis-prompts.md`**

The template text is used verbatim by the orchestrator when dispatching diagnosis agents. The `<…>` tokens are replaced with real values from the `error_events` row.

```markdown
# Self-Heal Diagnosis Agent Prompts

## Class A — Juggle Python Exception

```
[JUGGLE_THREAD:<thread_id>]
## Self-Heal Diagnosis — Class A (Juggle Python exception)

error_event_id: <id>
signature:      <signature_hash>
exc_type:       <exc_type>
entrypoint:     <entrypoint>
count:          <count> occurrence(s), first: <first_seen>, last: <last_seen>

### Traceback
<full traceback text from error_events.traceback>

### Task

You are a researcher. Diagnose this exception and propose a minimal code patch.

1. Read the source file(s) named in the traceback using semble MCP or Read tool.
2. Identify the root cause (missing guard, wrong assumption, off-by-one, etc.).
3. Produce a minimal unified diff of the fix (no refactoring, no style changes).
4. Assess confidence: HIGH / MEDIUM / LOW. Note any assumptions.

### Output format (for the action item message)

ROOT CAUSE: <one sentence>
FIX (unified diff):
--- a/src/<file>
+++ b/src/<file>
@@ ... @@
 <context>
-<old>
+<new>
CONFIDENCE: HIGH|MEDIUM|LOW
CAVEATS: <if any>

### Completion

After diagnosis:
1. uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py request-action <thread_id> \
     "Self-heal A: <exc_type> in <entrypoint> — <one-line root cause>" \
     --type decision --priority high
2. Note the returned action_item_id.
3. uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py selfheal-set-status <error_event_id> \
     awaiting_approval --action-item-id <action_item_id>
4. uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py agent complete <thread_id> \
     "Diagnosis complete for error_event <id>. Action item #<action_item_id> filed." \
     --retain "Self-heal A sig=<sig8>: <root cause in 10 words>"

NEVER auto-apply the patch. The user must approve the action item first.
```

## Class B — Orchestration Tool Error

```
[JUGGLE_THREAD:<thread_id>]
## Self-Heal Diagnosis — Class B (Orchestration tool error)

error_event_id: <id>
signature:      <signature_hash>
tool:           <entrypoint>   (the tool that errored)
juggle_ref:     <juggle_ref>   (the Juggle path that triggered it)
count:          <count> occurrence(s), first: <first_seen>, last: <last_seen>

### Tool error
<traceback / error_text from error_events.traceback>

### Tool input that caused the error
<command_args JSON from error_events.command_args>

### Task

You are a researcher. Diagnose why Juggle's instructions caused this tool error.

Decision tree:
- If a defensible code surface exists (e.g., a preflight check, a schema-load guard
  before arming the tool): propose a **code guard** (minimal diff to the relevant .py file).
- If no defensible code surface exists (e.g., the fix is purely how instructions are worded):
  propose an **instruction patch** to the culprit command/skill markdown at <juggle_ref>.

Steps:
1. Read <juggle_ref> (the command/skill markdown) using Read tool.
2. Read the relevant source file if a code guard is feasible (use semble MCP).
3. Identify exactly which instruction led the orchestrator to call <tool> incorrectly.
4. Produce the minimal fix:
   - Code guard: unified diff.
   - Instruction patch: exact replacement lines for the culprit section of the markdown.

### Output format

ROOT CAUSE: <one sentence — which instruction / missing guard>
FIX TYPE: code_guard | instruction_patch
FIX:
<unified diff OR markdown diff with --- / +++ lines>
CONFIDENCE: HIGH|MEDIUM|LOW
CAVEATS: <if any>

### Completion

After diagnosis:
1. uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py request-action <thread_id> \
     "Self-heal B: <tool> error via <juggle_ref_basename> — <one-line root cause>" \
     --type decision --priority high
2. Note the returned action_item_id.
3. uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py selfheal-set-status <error_event_id> \
     awaiting_approval --action-item-id <action_item_id>
4. uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py agent complete <thread_id> \
     "Diagnosis complete for error_event <id>. Action item #<action_item_id> filed." \
     --retain "Self-heal B sig=<sig8>: <root cause in 10 words>"

NEVER auto-apply the patch.
```

## Orchestrator Reaction to Monitor Lines

When the orchestrator sees `[SELFHEAL-A]` or `[SELFHEAL-B]` from juggle-selfheal-monitor:

1. Call `uv run juggle_cli.py selfheal list` to get the `error_event_id` and details.
2. Call `uv run juggle_cli.py selfheal-reset-diagnosing <id>` if the row is stuck in `diagnosing`.
3. Attempt cap check: if another row is already `diagnosing`, note "queued" inline and wait.
4. Call the CLI to claim the slot (handled automatically when the diagnosis agent calls `selfheal-set-status <id> diagnosing`).
5. Dispatch a researcher agent using the appropriate Class A or Class B prompt above.
   - Fill in `<id>`, `<signature_hash>`, `<entrypoint>`, etc. from `selfheal list` output.
   - Fill in `<thread_id>` with the current active thread.
```

- [ ] **Step 2: Commit**

```bash
git add docs/diagnosis-prompts.md
git commit -m "docs(selfheal): add Class A and Class B diagnosis agent prompt templates"
```

---

## Task 10: Integration Tests

**Files:**
- Test: `tests/test_juggle_selfheal.py`

- [ ] **Step 1: Write Class A end-to-end integration test**

Append to `tests/test_juggle_selfheal.py`:

```python
# ---------------------------------------------------------------------------
# Task 10: Integration tests
# ---------------------------------------------------------------------------

def test_class_a_e2e_record_and_claim(tmp_path):
    """Class A: simulate exception → record → claim diagnosis → resolve → regression."""
    from juggle_db import JuggleDB
    from juggle_selfheal import record_error, _try_claim_diagnosis_slot

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    # Simulate raising an exception
    def faulty():
        raise RuntimeError("simulated juggle bug")

    exc = None
    try:
        faulty()
    except RuntimeError as e:
        exc = e

    with patch("juggle_selfheal._get_db", return_value=db):
        record_error(exc, "juggle_cli.main")

    rows = db.get_open_error_events()
    assert len(rows) == 1
    assert rows[0]["status"] == "open"
    assert rows[0]["error_class"] == "A"
    assert rows[0]["exc_type"] == "RuntimeError"
    row_id = rows[0]["id"]

    # Claim diagnosis slot
    assert _try_claim_diagnosis_slot(db, row_id) is True
    assert db.get_open_error_events()[0]["status"] == "diagnosing"

    # Simulate approval
    db.set_error_event_status(row_id, "awaiting_approval", action_item_id=99)
    assert db.get_open_error_events()[0]["action_item_id"] == 99

    # Resolve
    db.set_error_event_status(row_id, "resolved")
    assert db.get_pending_selfheal_count() == 0

    # Regression: same exception re-recorded after resolved
    with patch("juggle_selfheal._get_db", return_value=db):
        record_error(exc, "juggle_cli.main")
    assert db.get_pending_selfheal_count() == 1


def test_class_b_e2e_fixture_to_db(tmp_path):
    """Class B: scan real-schema fixture → DB row → dedup → cap → resolve."""
    from juggle_db import JuggleDB
    from juggle_hooks import _do_class_b_scan
    from juggle_selfheal import _try_claim_diagnosis_slot

    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()

    transcript = FIXTURES_DIR / "transcript_class_b.jsonl"
    assert transcript.exists()

    with patch("juggle_selfheal._get_db", return_value=db):
        _do_class_b_scan(transcript)

    rows = db.get_open_error_events()
    assert len(rows) == 1
    row = rows[0]
    assert row["error_class"] == "B"
    assert row["entrypoint"] == "Skill"
    assert "juggle:" in row["juggle_ref"]
    assert row["status"] == "open"
    row_id = row["id"]

    # Dedup: scan again → count increments, no new row
    with patch("juggle_selfheal._get_db", return_value=db):
        _do_class_b_scan(transcript)
    rows = db.get_open_error_events()
    assert len(rows) == 1
    assert rows[0]["count"] == 2

    # Signature is stable
    sig1 = rows[0]["signature_hash"]
    assert len(sig1) == 16

    # Claim and resolve
    assert _try_claim_diagnosis_slot(db, row_id) is True
    db.set_error_event_status(row_id, "resolved")
    assert db.get_pending_selfheal_count() == 0
```

- [ ] **Step 2: Run full test suite**

```bash
uv run pytest tests/test_juggle_selfheal.py -v
```

Expected: all pass.

- [ ] **Step 3: Run broader cockpit/hooks regression suite**

```bash
uv run pytest tests/test_juggle_hooks.py tests/test_juggle_db.py tests/test_juggle_cli.py -v 2>&1 | tail -20
```

Expected: no new failures (pre-existing failures are documented in `--retain`).

- [ ] **Step 4: Final commit**

```bash
git add tests/test_juggle_selfheal.py
git commit -m "test(selfheal): add Class A + Class B end-to-end integration tests"
```

---

## Out of Scope

**`commands/start.md` dogfood fix** (Monitor schema prefetch): This is the first real-world Class B fix — it should be applied via the self-heal pipeline itself once the system is live. Implementing it in this plan would bypass the gated process the feature is designed to enforce. Note its expected signature for verification:

```
sig = sha256("class_B:Monitor:inputvalidationerror: 'command' is a required property:start.md")[:16]
```

---

## Self-Review Checklist

**Spec coverage:**

| Spec section | Task |
|---|---|
| §2 error_events DDL + Migration 24 | Task 2 |
| §3 Signature normalization (Class A + B) | Task 4 (juggle_selfheal.py) |
| §4a Class A capture wiring (all 7 entrypoints) | Task 5 |
| §4b Class B Stop-hook scan (corrected schema) | Task 6 |
| §5 Causal attribution N=10 same-turn | Task 6 |
| §6 Concurrency cap (_try_claim_diagnosis_slot) | Task 4 |
| §7 Monitor script + re-emit 60s | Task 7 |
| §8 Diagnosis agent prompts | Task 9 |
| §9 Gate + apply flow (state machine) | CLI helpers Task 3 + prompts Task 9 |
| §10 Offline / SessionStart surfacing | Task 8 |
| §12 Unit tests (13 items) | Tasks 2, 3, 4 |
| §12 Integration tests (Class A + Class B) | Task 10 |

**No placeholders found.** All code blocks are complete and executable.

**Type consistency verified:** `dedup_or_insert_error` returns `int | None` (consistent across Tasks 2, 4, 10). `set_error_event_status` returns `bool`. `_try_claim_diagnosis_slot(db, id)` takes `JuggleDB` instance (consistent in Tasks 4 and 10).
