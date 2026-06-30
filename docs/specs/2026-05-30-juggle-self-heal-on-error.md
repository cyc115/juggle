# Juggle Self-Heal on Error

**Status:** Approved — pending implementation  
**Date:** 2026-05-30  
**Scope:** `src/juggle_selfheal.py` (new), `src/juggle_db.py` (migration 24), `src/juggle_hooks.py` (Stop + SessionStart), `src/juggle_cli.py` (main()), `src/juggle_watchdog.py`, `scripts/juggle-selfheal-monitor` (new), `commands/start.md` (first dogfood fix)

---

## 1. Overview

When Juggle itself causes an error — either in its own Python code or by giving the orchestrator bad instructions — the system captures the failure, deduplicates it, and surfaces root-cause + proposed patch as a **gated action item**. No code lands without user approval.

### Two Error Classes

| Class | Source | Detection point | Fix target |
|-------|--------|-----------------|------------|
| **A — Juggle-Python** | Unhandled exception in Juggle's process (CLI, hooks, watchdog, cockpit) | `record_error()` called from each entrypoint's top-level `except` | Code patch (diff) |
| **B — Orchestration-layer** | Tool error caused by Juggle's instructions to the orchestrator | Stop-hook transcript scan with causal-attribution filter | Code guard if a defensible surface exists; else instruction/doc patch to culprit command/skill markdown |

### Shared pipeline

```
Error detected
    │
    ▼
juggle_selfheal.record_*(...)
    │  signature → dedup check
    ├─ duplicate (open/awaiting_approval) → increment count, update last_seen, suppress dispatch
    └─ new signature → INSERT error_events status=open
                            │
                            ▼
                  selfheal-monitor polls DB
                            │
                  status=open row found → emit line to stdout
                  orchestrator reads Monitor output
                            │
                  cap check: any row status='diagnosing'? → skip (remain open)
                  else: UPDATE status='diagnosing' (atomic, rowcount=1 guard)
                            │
                  dispatch diagnosis agent (researcher)
                            │
                  agent: reads traceback/tool-error + source code
                  proposes diff (A) or guard/doc-patch (B)
                  calls request-action --type decision
                  UPDATEs status=awaiting_approval
                            │
                  user approves action item
                            │
                  coder applies on branch cyc_juggle-selfheal-<sig8>
                  verifies, user approves → merge to main
                  UPDATE status=resolved, ack action item
```

---

## 2. `error_events` Schema (exact DDL)

```sql
CREATE TABLE IF NOT EXISTS error_events (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  signature_hash   TEXT    NOT NULL,
  error_class      TEXT    NOT NULL CHECK(error_class IN ('A', 'B')),
  exc_type         TEXT,                    -- Class A: exception class name; Class B: NULL
  traceback        TEXT,                    -- Class A: full traceback string; Class B: tool error text
  entrypoint       TEXT,                    -- Class A: e.g. 'juggle_cli.main'; Class B: tool name
  surface          TEXT,                    -- Class B: culprit command/skill path; Class A: NULL
  command_args     TEXT,                    -- Class A: sys.argv as JSON; Class B: tool_input as JSON
  juggle_ref       TEXT,                    -- Class B: matched juggle path/command that triggered it
  count            INTEGER NOT NULL DEFAULT 1,
  first_seen       TEXT    NOT NULL,
  last_seen        TEXT    NOT NULL,
  status           TEXT    NOT NULL DEFAULT 'open'
                           CHECK(status IN ('open','diagnosing','awaiting_approval','resolved')),
  action_item_id   INTEGER REFERENCES action_items(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_error_events_sig
  ON error_events(signature_hash);

CREATE INDEX IF NOT EXISTS idx_error_events_status
  ON error_events(status);
```

### Migration 24

Add to `juggle_db.py` `_migrate()` after Migration 23, using the existing `ALTER TABLE` / `CREATE TABLE` pattern:

```python
# Migration 24: error_events for self-heal
try:
    conn.execute(CREATE_ERROR_EVENTS)   # constant defined at module top
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

`CREATE_ERROR_EVENTS` constant goes in `juggle_db.py` alongside the other `CREATE_*` constants.

**Migration path:** `juggle_db.py init_db()` already calls `_migrate()` on every startup, so the table is created automatically when any Juggle process starts after the code ships. `cmd_doctor` (`juggle_cmd_doctor.py`) calls `db.init_db()` — no separate migration command needed.

---

## 3. Signature & Normalization Algorithm

### Class A (Python exception)

**Input to hash:**

```
class_A:<exc_type>:<normalized_frame_1>|<normalized_frame_2>|...
```

**Normalize each frame:**

1. Strip absolute paths → basename only: `"/Users/x/github/juggle/src/juggle_cli.py"` → `"juggle_cli.py"`
2. Keep `file:lineno:func`: `"juggle_cli.py:701:main"`
3. Take the **last 5 frames** of the traceback (innermost → most specific).
4. Exclude frames from Python stdlib (file path contains `site-packages` or starts with Python install prefix).

**Hash:** `hashlib.sha256(input_str.encode()).hexdigest()[:16]`

**Example:**

```
exc_type   = "KeyError"
frames     = ["juggle_cli.py:701:main", "juggle_cmd_agents.py:312:cmd_complete_agent"]
hash_input = "class_A:KeyError:juggle_cli.py:701:main|juggle_cmd_agents.py:312:cmd_complete_agent"
sig        = sha256(hash_input)[:16]  → e.g. "a3f1b2c9d4e5f678"
```

### Class B (orchestration tool error)

**Input to hash:**

```
class_B:<tool_name>:<normalized_error_fragment>:<juggle_ref_basename>
```

**Normalize error fragment:** first 120 chars of the error message, lowercased, with digits stripped (removes line numbers that vary), whitespace collapsed.

**Example:**

```
tool_name      = "Monitor"
error_msg      = "InputValidationError: 'command' is a required property"
juggle_ref     = "commands/start.md"
normalized_err = "inputvalidationerror: 'command' is a required property"
hash_input     = "class_B:Monitor:inputvalidationerror: 'command' is a required property:start.md"
sig            = sha256(hash_input)[:16]
```

### Dedup rule

- Same `signature_hash` + `status` in `('open', 'diagnosing', 'awaiting_approval')` → **suppress**: `UPDATE error_events SET count = count+1, last_seen = ? WHERE signature_hash = ?`. Do not dispatch another diagnosis agent.
- `status = 'resolved'` → treat as new: INSERT a fresh row (resolved bugs can regress).

---

## 4. Capture Wiring

### 4a. Class A — Python exceptions

**Module:** `src/juggle_selfheal.py`

```python
import hashlib, json, logging, os, sys, traceback
from pathlib import Path

_log = logging.getLogger(__name__)
_SELFHEAL_ENV = "JUGGLE_SELFHEAL_OP"  # set around own DB calls → skip re-capture

_ALLOWLIST = (
    SystemExit,
    KeyboardInterrupt,
)
_ALLOWLIST_PATTERNS = (
    # argparse exits (ValueError/SystemExit from argparse)
    # sqlite locked (transient infra error, not a Juggle bug)
)

def _is_allowlisted(exc: BaseException) -> bool:
    if isinstance(exc, _ALLOWLIST):
        return True
    if isinstance(exc, (ValueError, SystemExit)) and _from_argparse(exc):
        return True
    if isinstance(exc, Exception):
        msg = str(exc).lower()
        if "sqlite" in msg and "database is locked" in msg:
            return True
    return False

def _from_argparse(exc) -> bool:
    # argparse raises SystemExit(2) and ValueError for bad args; check stack
    import traceback as tb
    frames = tb.extract_stack()
    return any("argparse" in str(f.filename) for f in frames)

def record_error(exc: BaseException, entrypoint: str, context: dict | None = None) -> None:
    """Capture a Class A exception. Never re-raises. Self-protecting."""
    if os.environ.get(_SELFHEAL_ENV):
        return
    try:
        if _is_allowlisted(exc):
            return
        _capture_class_a(exc, entrypoint, context or {})
    except Exception as inner:
        _log.error("selfheal.record_error itself failed: %s", inner)

def record_orchestration_error(
    tool: str, tool_input: dict, error_text: str, juggle_ref: str
) -> None:
    """Capture a Class B tool error. Never re-raises. Self-protecting."""
    if os.environ.get(_SELFHEAL_ENV):
        return
    try:
        _capture_class_b(tool, tool_input, error_text, juggle_ref)
    except Exception as inner:
        _log.error("selfheal.record_orchestration_error itself failed: %s", inner)
```

**Insertion points (Class A):**

| File | Location | Change |
|------|----------|--------|
| `src/juggle_cli.py` | `main()` line ~701, existing `except Exception as e:` block | Add `from juggle_selfheal import record_error; record_error(e, "juggle_cli.main", {"argv": sys.argv})` before `print(f"Error: {e}")` |
| `src/juggle_hooks.py` | `handle_user_prompt_submit()` line ~238, existing `except Exception as exc:` | Add `record_error(exc, "juggle_hooks.UserPromptSubmit")` |
| `src/juggle_hooks.py` | `handle_stop()` line ~300, existing `except Exception as exc:` | Add `record_error(exc, "juggle_hooks.Stop")` |
| `src/juggle_hooks.py` | `handle_session_start()` line ~331, existing `except Exception as exc:` | Add `record_error(exc, "juggle_hooks.SessionStart")` |
| `src/juggle_hooks.py` | `handle_pre_tool_use()` line ~457, existing `except Exception as exc:` | Add `record_error(exc, "juggle_hooks.PreToolUse")` |
| `src/juggle_hooks.py` | `handle_post_tool_use()` line ~562, existing `except Exception as exc:` | Add `record_error(exc, "juggle_hooks.PostToolUse")` |
| `src/juggle_hooks.py` | `main()` wrapper (add outer try/except around `handler(data)` at line 600) | Wrap `handler(data)` in try/except; call `record_error(exc, f"juggle_hooks.{event_name}")` |
| `src/juggle_watchdog.py` | Top-level daemon loop (wherever exceptions are caught) | Add `record_error(exc, "juggle_watchdog")` |
| `src/juggle_cockpit.py` | App.run() wrapper if present | Add `record_error(exc, "juggle_cockpit")` |

**Self-protection rule:** All `_capture_class_a` / `_capture_class_b` internal operations set `os.environ[_SELFHEAL_ENV] = "1"` before touching the DB and `del os.environ[_SELFHEAL_ENV]` in a `finally` block. `record_error()` is a no-op when this env var is set, preventing recursive capture.

### 4b. Class B — Stop-hook transcript scan

**Location:** `src/juggle_hooks.py` `handle_stop()`, after the existing `last_msg` capture block.

**Algorithm:**

```python
def _scan_transcript_for_class_b(data: dict) -> None:
    """Read current-turn tool calls from transcript; record Class B errors."""
    transcript_path = data.get("transcript_path")
    if not transcript_path:
        return  # transcript unavailable; skip silently
    try:
        _do_class_b_scan(Path(transcript_path))
    except Exception as exc:
        logging.warning("Class B transcript scan failed: %s", exc)

def _do_class_b_scan(transcript_path: Path) -> None:
    import json as _json
    from juggle_selfheal import record_orchestration_error

    # Read the transcript JSONL. Each line is one event.
    # Collect events from current turn (events after the last "human" turn start).
    lines = transcript_path.read_text(errors="replace").splitlines()
    events = []
    for line in lines:
        try:
            events.append(_json.loads(line))
        except _json.JSONDecodeError:
            continue

    # Find the last user/human message boundary → current turn starts there.
    current_turn_start = 0
    for i, ev in enumerate(events):
        if ev.get("role") == "human" or ev.get("type") == "user":
            current_turn_start = i

    current_turn = events[current_turn_start:]
    _attribute_tool_errors(current_turn)

_JUGGLE_PATHS = (
    "juggle_cli.py",
    "juggle_hooks.py",
    "juggle_selfheal.py",
    "scripts/juggle-",
    "commands/",       # juggle skill/command markdown
    "juggle:",         # /juggle: skill invocations
)

def _is_juggle_ref(text: str) -> bool:
    return any(p in text for p in _JUGGLE_PATHS)

def _attribute_tool_errors(turn_events: list[dict]) -> None:
    from juggle_selfheal import record_orchestration_error

    # N = 10: look at up to 10 tool calls in this turn for juggle references
    N = 10
    tool_calls = [e for e in turn_events if e.get("type") in ("tool_use", "tool_call")]
    tool_results = [e for e in turn_events if e.get("type") in ("tool_result", "tool_error")]

    # Build a window of recent tool inputs for juggle-ref detection
    recent_inputs = []
    for tc in tool_calls[-N:]:
        inp = tc.get("input") or tc.get("tool_input") or {}
        recent_inputs.append(json.dumps(inp))
    recent_inputs_str = " ".join(recent_inputs)

    juggle_ref = None
    for path in _JUGGLE_PATHS:
        if path in recent_inputs_str:
            juggle_ref = path
            break

    if juggle_ref is None:
        return  # no juggle involvement in this turn

    # Find tool errors
    for tr in tool_results:
        if tr.get("is_error") or "error" in str(tr.get("content", "")).lower():
            error_text = str(tr.get("content", ""))
            tool_name = tr.get("tool_use_id", "unknown")
            # Match to tool_use to get the tool name
            for tc in tool_calls:
                if tc.get("id") == tr.get("tool_use_id"):
                    tool_name = tc.get("name", tool_name)
                    tool_input = tc.get("input") or {}
                    record_orchestration_error(tool_name, tool_input, error_text, juggle_ref)
                    break
```

**Call site:** add `_scan_transcript_for_class_b(data)` at the end of `handle_stop()` (inside the `try` block, before `sys.exit(0)`).

---

## 5. Causal Attribution for Class B

**Resolved value:** `N = 10` tool calls, **same-turn constraint enforced**.

**Rationale:**

- **Same-turn only:** The Stop hook fires at the end of a single response turn. All tool calls in `current_turn` (events after the last human message) happened in that turn. Cross-turn attribution introduces false positives from coincidental prior juggle activity (e.g., the user ran `/juggle:start` two turns ago but the current tool error is from unrelated user code).
- **N = 10:** Within a turn, Juggle-triggered sequences rarely span more than 5–6 tool calls. 10 is generous enough to handle multi-step orchestration (e.g., `Skill` → `Bash` → `Monitor`) while remaining bounded.
- **Attribution test:** At least one tool call input in the last N must contain a string matching `_JUGGLE_PATHS` (see above). The `juggle_ref` recorded is the first matched path.

**Dogfood case:** `/juggle:start` → `Skill(juggle:start)` → `Monitor(...)` (no schema loaded) → `InputValidationError`. All in same turn. `Skill` input contains `"juggle:start"` → juggle_ref = `"juggle:"`. Tool error on `Monitor`. Class B captured, fix target = `commands/start.md` (add `ToolSearch select:Monitor` before arming Monitor).

---

## 6. Diagnosis Concurrency Cap = 1

**Mechanism:** DB-enforced atomic UPDATE.

```python
def _try_claim_diagnosis_slot(db, error_event_id: int) -> bool:
    """Attempt to claim the diagnosis slot for this error_event. Returns True if claimed."""
    with db._connect() as conn:
        # Abort if any other row is already in 'diagnosing' state
        in_flight = conn.execute(
            "SELECT COUNT(*) FROM error_events WHERE status = 'diagnosing'"
        ).fetchone()[0]
        if in_flight > 0:
            return False
        # Atomically claim this row
        cur = conn.execute(
            "UPDATE error_events SET status = 'diagnosing' "
            "WHERE id = ? AND status = 'open'",
            (error_event_id,)
        )
        conn.commit()
        return cur.rowcount == 1
```

**Enforcement:**

1. `juggle-selfheal-monitor` emits a line for each new `status=open` row.
2. The orchestrator receives the Monitor line and calls the dispatch helper.
3. The dispatch helper calls `_try_claim_diagnosis_slot()`. If it returns `False`, the orchestrator is notified "Diagnosis already in flight; [sig] queued." and does not dispatch.
4. Only when the current diagnosis reaches `awaiting_approval` or `resolved` does the next `status=open` row become eligible (the next Monitor poll will emit it again only if it remains `open` — but since the monitor deduplicates by ID, the orchestrator must re-query `selfheal list` after each resolution to pick up queued `open` rows).

**Starvation mitigation:** The `juggle-selfheal-monitor` script always re-emits any `status=open` row that has not been claimed within 60 seconds of first emission (re-poll at each tick, emit if not in emitted set AND a diagnosis slot is free). See §7.

**This is code-enforced, not prompt-enforced:** The `rowcount=1` guard on the UPDATE means two concurrent callers can't both claim the slot; SQLite's WAL mode ensures serialization.

---

## 7. Monitor Script

**Path:** `scripts/juggle-selfheal-monitor`

**Structure:** mirror `scripts/juggle-agent-monitor` (uv script header, same DB-poll loop pattern).

```python
#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = []
# ///
"""juggle-selfheal-monitor — streams self-heal events to stdout for Monitor tool.

Each line is one open/newly-emitted error event.
Format:
  Class A: [SELFHEAL-A] <exc_type> in <entrypoint>: <truncated message> (count=N)
  Class B: [SELFHEAL-B] <tool_name> error via <juggle_ref_basename>: <truncated error> (count=N)
"""
```

**Poll loop:**

- Poll `error_events WHERE status = 'open'` every 2 seconds.
- Track emitted IDs + last-emitted-at timestamp per ID.
- On first encounter: emit immediately.
- On re-encounter (still `open` after 60s): re-emit to prompt the orchestrator to retry cap check.
- On encounter with `status != 'open'`: remove from emitted set (claimed or resolved).
- Suppress: `status IN ('diagnosing', 'awaiting_approval', 'resolved')`.

**Emitted line format:**

```
[SELFHEAL-A] KeyError in juggle_cli.main: 'thread_id' key missing (count=3)
[SELFHEAL-B] Monitor error via start.md: inputvalidationerror 'command' required (count=1)
```

**Orchestrator reaction (specify in dispatch prompt / start.md section):**

> When you see a `[SELFHEAL-A]` or `[SELFHEAL-B]` line from the self-heal monitor:
> 1. Call `juggle selfheal-status` (or query `selfheal list`) to see the full error detail.
> 2. Attempt `_try_claim_diagnosis_slot`. If cap is occupied, note "queued" inline.
> 3. If claimed: dispatch a diagnosis researcher agent using the Class A or Class B prompt (§8) for that `error_event_id`.

*(A `selfheal-status` / `selfheal list` CLI sub-command should be added as a thin wrapper over `SELECT * FROM error_events WHERE status != 'resolved'`.)*

---

## 8. Diagnosis Agent Prompts

### Class A Prompt (full text)

```
[JUGGLE_THREAD:<thread_id>]
## Self-Heal Diagnosis — Class A (Juggle Python exception)

error_event_id: <id>
signature:      <signature_hash>
exc_type:       <exc_type>
entrypoint:     <entrypoint>
count:          <count> occurrence(s), first: <first_seen>, last: <last_seen>

### Traceback
```
<full traceback text>
```

### Task

You are a researcher. Diagnose this exception and propose a minimal code patch.

1. Read the source file(s) named in the traceback using semble MCP or Read tool.
2. Identify the root cause (missing guard, wrong assumption, off-by-one, etc.).
3. Produce a minimal unified diff of the fix (no refactoring, no style changes).
4. Assess confidence: HIGH / MEDIUM / LOW. Note any assumptions.

### Output format (for the action item)

```
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
```

### Completion

After diagnosis:
1. Call `request-action <thread_id> "Self-heal A: <exc_type> in <entrypoint> — <one-line root cause>" --type decision --priority high`
2. Note the returned action_item_id.
3. Call the selfheal DB helper to set status=awaiting_approval and store action_item_id:
   `uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py selfheal-set-status <error_event_id> awaiting_approval --action-item-id <action_item_id>`
4. `agent complete <thread_id> "Diagnosis complete for error_event <id>. Action item #<action_item_id> filed." --retain "Self-heal A sig=<sig8>: <root cause in 10 words>"`

NEVER auto-apply the patch. The user must approve the action item first.
```

---

### Class B Prompt (full text)

```
[JUGGLE_THREAD:<thread_id>]
## Self-Heal Diagnosis — Class B (Orchestration tool error)

error_event_id: <id>
signature:      <signature_hash>
tool:           <entrypoint>  (the tool that errored)
juggle_ref:     <juggle_ref>  (the Juggle path that triggered it)
count:          <count> occurrence(s), first: <first_seen>, last: <last_seen>

### Tool error
```
<traceback / error_text>
```

### Tool input that caused the error
```json
<command_args JSON>
```

### Task

You are a researcher. Diagnose why Juggle's instructions caused this tool error.

**Decision tree:**
- If a defensible code surface exists (e.g., a preflight check, a schema-load guard before arming the tool): propose a **code guard** (minimal diff to the relevant .py file).
- If no defensible code surface exists (e.g., the fix is purely in how instructions are worded): propose an **instruction patch** to the culprit command/skill markdown at `<juggle_ref>`.

**Steps:**
1. Read `<juggle_ref>` (the command/skill markdown) using Read tool.
2. Read the relevant source file if a code guard is feasible (use semble MCP).
3. Identify exactly which instruction led the orchestrator to call `<tool>` incorrectly.
4. Produce the minimal fix:
   - **Code guard:** unified diff.
   - **Instruction patch:** exact replacement lines for the culprit section of the markdown.

### Output format

```
ROOT CAUSE: <one sentence — which instruction / missing guard>
FIX TYPE: code_guard | instruction_patch
FIX:
<unified diff OR markdown diff with --- / +++ lines>
CONFIDENCE: HIGH|MEDIUM|LOW
CAVEATS: <if any>
```

### Completion

After diagnosis:
1. Call `request-action <thread_id> "Self-heal B: <tool> error via <juggle_ref_basename> — <one-line root cause>" --type decision --priority high`
2. Note the returned action_item_id.
3. `uv run ${CLAUDE_PLUGIN_ROOT}/src/juggle_cli.py selfheal-set-status <error_event_id> awaiting_approval --action-item-id <action_item_id>`
4. `agent complete <thread_id> "Diagnosis complete for error_event <id>. Action item #<action_item_id> filed." --retain "Self-heal B sig=<sig8>: <root cause in 10 words>"`

NEVER auto-apply the patch.
```

---

## 9. Gate + Apply Flow (State Machine)

```
       ┌──────────┐
       │   open   │◄──── new/regressed signature
       └────┬─────┘
            │  cap free → _try_claim_diagnosis_slot() → rowcount=1
            ▼
      ┌───────────┐
      │ diagnosing│  ← diagnosis agent running
      └─────┬─────┘
            │  agent calls selfheal-set-status → awaiting_approval
            ▼
  ┌──────────────────┐
  │ awaiting_approval│  ← action item visible in cockpit
  └────────┬─────────┘
           │  user approves action item
           ▼
   [coder dispatched on branch cyc_juggle-selfheal-<sig8>]
           │
           │  coder: apply patch, run tests, agent complete
           ▼
   [user verifies + approves merge]
           │
           │  merge to main (Juggle commit-to-main policy)
           │  juggle selfheal-set-status <id> resolved
           │  juggle ack-action <action_item_id>
           ▼
        ┌──────────┐
        │ resolved │
        └──────────┘
```

**Branch naming:** `cyc_juggle-selfheal-<sig8>` where `sig8` = first 8 chars of `signature_hash`.

**Coder prompt additions (over standard coder template):**
- Include the diagnosis output (diff or instruction patch) verbatim.
- Add: "Apply ONLY the proposed fix. No scope expansion. Run existing tests. If tests fail for unrelated reasons, document in --retain and proceed."
- Add: "After applying, call `juggle selfheal-set-status <error_event_id> awaiting_approval` (already set; do not change status — the orchestrator manages this)."

**Merge to main:** follows Juggle commit-to-main policy. After user signs off: `git checkout main && git merge --no-ff cyc_juggle-selfheal-<sig8> && git push`. Then `selfheal-set-status <id> resolved` + `ack-action <action_item_id>`.

**Regressed resolved bug:** If the same `signature_hash` appears after `status=resolved`, a new row is inserted (treated as fresh). The old resolved row remains for audit.

---

## 10. Offline / SessionStart Surfacing

### Offline (no live session)

`record_error()` and `record_orchestration_error()` write directly to the DB regardless of session state. No live session needed — errors accumulate durably.

### SessionStart hook

In `juggle_hooks.py` `handle_session_start()`, after the existing `build_startup_output()` call, append a self-heal summary line to `additional_context`:

```python
# Self-heal pending errors
pending = _get_pending_selfheal_count(db)
if pending > 0:
    additional_context += f"\n⚠️ {pending} pending self-heal error(s) — run `selfheal list` to review."
```

```python
def _get_pending_selfheal_count(db) -> int:
    try:
        with db._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM error_events WHERE status != 'resolved'"
            ).fetchone()
            return row[0] if row else 0
    except Exception:
        return 0
```

This means even if the error occurred during an offline/background run, the user sees it on next session resume.

---

## 11. Devil's Advocate

| Risk | Failure mode | Mitigation |
|------|-------------|------------|
| **Recursive self-capture** | `record_error()` itself throws → tries to record that → infinite loop | `JUGGLE_SELFHEAL_OP` env var gates all re-entry; inner exceptions are caught and logged only, never re-raised |
| **False positives (Class B)** | Unrelated tool error attributed to Juggle because a juggle path appeared in unrelated prior tool call input | Same-turn constraint + N=10 window limits scope; `_JUGGLE_PATHS` strings are specific enough (no single-char matches) |
| **False positives (Class A)** | Transient infra error (DB lock, network timeout) generates action item | Allowlist covers `sqlite ... database is locked`; additional transient patterns can be added to allowlist without schema change |
| **Self-modifying running code** | Coder applies patch to files that are currently running (watchdog, hooks) | Hooks and watchdog are re-`exec`'d on each invocation (not long-running in-process); CLI is a subprocess. Patch takes effect on next invocation. No runtime reload needed. |
| **Stop-hook cost/fragility** | Transcript read on every Stop event adds latency; `transcript_path` may be absent or large | Check `transcript_path` first; skip if absent (no error). Read only the tail (last 200 lines) for large transcripts. Scan is O(N) over tool calls, not O(transcript). |
| **Why PostToolUse was rejected** | PostToolUse fires after EVERY tool call — high frequency, and the error hasn't fully propagated yet at that point; the Stop hook fires once per turn after all errors are visible; Stop is also where `last_assistant_message` already tells us the orchestrator completed processing |
| **Concurrency cap starvation** | Multiple rapid errors → first fills cap → rest sit `open` indefinitely if diagnosis agent hangs | Watchdog already monitors agent health. If diagnosis agent is stuck, watchdog nudges/alerts. The re-emit after 60s (§7) keeps open errors visible. `selfheal-set-status` can be called manually to reset a stuck `diagnosing` row back to `open`. |
| **Diagnosis agent proposes wrong fix** | Researcher misreads traceback → bad patch in action item | User reviews action item before coder dispatches — the gate absorbs this. CONFIDENCE field helps the user calibrate trust. |
| **action_items FK** | `action_items.id` is deleted when thread is closed/archived | `ON DELETE SET NULL` on the FK — `error_events.action_item_id` becomes NULL. Self-heal status is still tracked; the action item is gone but `status=awaiting_approval` remains queryable. The orchestrator should surface this as "action item lost — re-request". |

---

## 12. Test Plan

### Unit tests (`tests/test_juggle_selfheal.py`)

| Test | What it verifies |
|------|-----------------|
| `test_signature_dedup_class_a` | Two identical exceptions → same `signature_hash`; second call increments `count`, does not insert new row |
| `test_signature_different_lineno` | Exception at different line in same function → different signature (lineno is in the frame key) |
| `test_allowlist_systemexit` | `SystemExit` → `record_error()` returns without writing to DB |
| `test_allowlist_sqlite_locked` | `OperationalError("sqlite database is locked")` → skipped |
| `test_allowlist_keyboardinterrupt` | `KeyboardInterrupt` → skipped |
| `test_class_b_attribution_positive` | Turn with juggle path in tool input + tool error → `record_orchestration_error` called |
| `test_class_b_attribution_negative` | Turn with tool error but no juggle path in any tool input → NOT recorded |
| `test_class_b_same_turn_constraint` | Error in prior turn, juggle ref only in current turn → NOT attributed (prior turn events excluded) |
| `test_concurrency_cap_single` | Two concurrent `_try_claim_diagnosis_slot()` calls on same row → exactly one returns True |
| `test_concurrency_cap_in_flight` | `status='diagnosing'` row exists → new `_try_claim_diagnosis_slot()` returns False |
| `test_resolved_regression` | Same signature after `status=resolved` → new INSERT, not dedup |
| `test_self_protection` | `record_error()` called while `JUGGLE_SELFHEAL_OP=1` → no-op |
| `test_session_start_count` | 2 `open` + 1 `diagnosing` rows → `_get_pending_selfheal_count` returns 3 |

### Integration tests

**Class A end-to-end:**

1. Patch `juggle_cli.main()` to raise a test exception when passed `--test-selfheal-class-a`.
2. Run `uv run src/juggle_cli.py --test-selfheal-class-a`.
3. Assert `error_events` has one row, `status='open'`, `error_class='A'`, correct `exc_type`.
4. Call `_try_claim_diagnosis_slot()` → returns True, row is `status='diagnosing'`.
5. Simulate approval: `selfheal-set-status <id> awaiting_approval`.
6. Simulate resolution: `selfheal-set-status <id> resolved`.
7. Re-run step 2 → new row inserted (regression path).

**Class B end-to-end (Monitor-style):**

1. Construct a synthetic transcript JSONL with:
   - A human message.
   - A `tool_use` event: `{"type":"tool_use","name":"Skill","input":{"skill":"juggle:start"}}`.
   - A `tool_result` event: `{"type":"tool_result","is_error":true,"content":"InputValidationError: 'command' is a required property"}`.
2. Write to a temp file; call `_do_class_b_scan(Path(tmp))`.
3. Assert `error_events` has one row, `error_class='B'`, `entrypoint='Skill'` (or `Monitor`), `juggle_ref` contains `"juggle:"`.
4. Assert signature is stable across two identical calls (dedup).

---

## 13. YAGNI / Non-Goals

| Non-goal | Reason |
|----------|--------|
| **Auto-merge without approval** | Safety gate is the entire point of this feature. No exceptions. |
| **Batched diagnosis mode** (multiple errors → one agent) | Complexity not justified; diagnosis quality is better per-error; cap=1 prevents overload anyway |
| **Curated tool allowlist for Class B** (e.g., only flag Monitor/Bash errors) | Over-engineered; causal attribution filter already limits false positives without maintaining a list |
| **Cap > 1** | The risk of two concurrent diagnosis agents producing conflicting patches outweighs the throughput benefit; starvation is mitigated by re-emit |
| **Automatic error triage / severity scoring** | User reviews action item; CONFIDENCE field is sufficient |
| **Cross-turn Class B attribution** | See §5 — false-positive risk is too high |

---

## First Dogfood Case

**Bug:** `/juggle:start` tells the orchestrator to arm Monitor immediately (`Monitor: ${CLAUDE_PLUGIN_ROOT}/scripts/juggle-agent-monitor`) before ToolSearch has loaded the Monitor schema. The orchestrator calls Monitor with the required `command` argument absent → `InputValidationError: 'command' is a required property`.

**Class B detection:**
- Same-turn tool calls include `Skill(juggle:start)` (juggle_ref = `"juggle:"` / `"commands/start.md"`) and the erroring `Monitor(...)` call.
- `_attribute_tool_errors` records `error_class='B'`, `entrypoint='Monitor'`, `juggle_ref='commands/start.md'`.

**Proposed fix (instruction patch to `commands/start.md`):**

```markdown
-Arm monitor immediately:
-```
-Monitor: ${CLAUDE_PLUGIN_ROOT}/scripts/juggle-agent-monitor
-```
+Before arming Monitor, load its schema:
+```
+ToolSearch: select:Monitor
+```
+Then arm monitor:
+```
+Monitor: ${CLAUDE_PLUGIN_ROOT}/scripts/juggle-agent-monitor
+```
```

This fix is applied via the Class B flow: instruction patch → coder edits `commands/start.md` on `cyc_juggle-selfheal-<sig8>` → merge to main.
