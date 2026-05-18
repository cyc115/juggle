# Juggle Agent Watchdog — Design Spec

**Status:** Draft  
**Date:** 2026-05-17

---

## Motivation

Agents stall silently when network interruptions break Claude's mid-stream output. The Monitor tool sees nothing because nothing is emitted — no `complete-agent`, no heartbeat. In the incident that prompted this spec, agents IF and IH burned 108k and 30k tokens respectively and never completed; the user had to diagnose via DB inspection and recover manually.

A second failure mode: Claude Code permission prompts block agents until manually answered. These are recoverable without re-dispatch but currently require user intervention.

The watchdog closes both gaps: auto-resolves permission prompts and auto-recovers stalled or crashed agents.

---

## Architecture

A standalone Python daemon (`scripts/juggle-agent-watchdog`) launched by `cmd_start` as a background process (PID file at `~/.juggle/watchdog.pid`). It polls every 30 s, inspects all `status='busy'` agents in `juggle.db`, diffs their tmux pane output against the previous snapshot, classifies state, and takes action. `cmd_stop` sends SIGTERM and cleans the PID file.

The watchdog imports from `src/` (same as other scripts) — no new process boundary, no IPC, direct DB reads.

```
cmd_start
  └─ subprocess.Popen(juggle-agent-watchdog, start_new_session=True)
       └─ writes ~/.juggle/watchdog.pid
            └─ poll loop (30 s)
                 ├─ for each busy agent: capture pane → classify
                 ├─ allowlist prompt → send key, log
                 ├─ stalled / crashed → aggressive recovery
                 └─ writes events to watchdog_events table (telemetry)

cmd_stop
  └─ reads ~/.juggle/watchdog.pid → SIGTERM → remove PID file
```

---

## State Machine

Each poll cycle iterates every agent with `status='busy'`. An in-memory dict tracks per-agent grace state across cycles.

| State | Detection | Action |
|---|---|---|
| **Working** | Pane content differs from previous snapshot | Save snapshot; reset grace counter |
| **Working-but-quiet** | Unchanged AND pane shows `Thinking…` OR last change < 60 s ago | Increment grace counter; no action if grace ≤ 1 cycle |
| **Recoverable prompt** | Pane tail matches allowlist pattern (see below) | Auto-send safe key; log to `watchdog.log`; add `notifications_v2` row (no action item) |
| **Stalled-silent** | Unchanged ≥ threshold, no recoverable prompt, grace expired | Save 500-line recovery snapshot; file `high` action item; execute aggressive recovery |
| **Crashed** | Pane no longer exists (`verify_pane` returns False) OR pane ends with bare shell prompt (`$`, `%`, `>` on last non-empty line) — Claude process exited | Mark thread `failed`; file `high` action item; execute aggressive recovery |

Grace counter resets on any state change. An agent triggers recovery only after spending ≥ 2 consecutive cycles in stalled state (i.e., threshold elapsed AND one more poll with no change).

### Allowlist of safe auto-responses

| Pattern (pane tail contains) | Key sent |
|---|---|
| `1. Yes / 2. Yes, allow always / 3. No` | `2\n` (allow always) |
| `1. Yes, auto-accept / 2. Yes, manually approve / 3. No` (plan mode) | `2\n` (manually approve — safer) |
| `Press Enter to continue` | `\n` |
| Boxed dialog `╭─ … ─╮` with single default action | `\n` |

Patterns matched against last 15 lines of `tmux capture-pane -pt <pane_id>`.

---

## Aggressive Recovery

Triggered on Stalled-silent or Crashed. Runs once per agent lifetime (guarded by `agents.watchdog_retried`).

```
1. Capture pane (last 500 lines) → ~/.juggle/watchdog/recovery/<agent_id>-<unix_ts>.txt
2. Read agent.assigned_thread, agent.last_task, agent.role, agent.model from DB
3. Decommission stuck agent: kill_pane(agent.pane_id) + delete_agent(agent.id)
4. DB: update threads SET status='failed' WHERE id=<assigned_thread>
5. DB: add_action_item type='failure' priority='high':
       "🚨 [LABEL] agent stalled/crashed — snapshot at <path>, auto-retrying"
6. IF agent.watchdog_retried == 1:
       DB: add_action_item type='failure' priority='high':
           "🛑 [LABEL] agent stalled AGAIN after watchdog retry — manual intervention required"
       STOP — do not re-dispatch.
7. IF agent.last_task is NULL:
       DB: add_action_item type='failure' priority='high':
           "🚨 [LABEL] agent stalled — no task content to replay; re-dispatch manually"
       STOP.
8. spawn_agent(db, role=agent.role, model=agent.model) → new_agent
9. DB: update agents SET watchdog_retried=1, last_task=<task_content> WHERE id=<new_agent.id>
10. DB: update threads SET status='background' WHERE id=<assigned_thread>
11. DB: update agents SET assigned_thread=<assigned_thread>, status='busy', busy_since=now WHERE id=<new_agent.id>
12. send_task(new_agent.pane_id, agent.last_task)
13. DB: add_action_item type='manual_step' priority='normal':
        "⚠️ [LABEL] agent auto-re-dispatched after stall — verify result when complete"
14. Insert into watchdog_events: agent_id, thread_id, event_type, snapshot_path, created_at
```

The recovery path operates directly on the DB (no CLI subprocess calls) to avoid the generic "agent released without completing" action item that `release-agent --force` would file. The thread is re-opened to `background` at step 10 only after the new agent is ready (step 8).

**Max 1 retry per agent chain.** The new agent spawned at step 10 gets `watchdog_retried=1`. If it stalls, step 8 blocks a second retry and escalates.

---

## Adaptive Threshold

### Cold-start (< 10 samples for role in last 30 days)

| Role | Default |
|---|---|
| coder | 5 min |
| planner | 3 min |
| researcher | 2 min |

### Steady-state (≥ 10 samples)

`threshold = 2 × median(duration_secs)` for the role, computed from `agent_completions` over the last 30 days.

Computed lazily once per watchdog poll (single SQL query per role, cheap).

### Per-agent override

`juggle set-watchdog <agent_id> <minutes>` — writes to `agents.watchdog_threshold_minutes`.  
`juggle set-watchdog <agent_id> off` — sets `agents.watchdog_threshold_minutes = -1` (watchdog skips this agent).

---

## Schema Changes

### Modified: `agents` table (new columns via migration)

```sql
ALTER TABLE agents ADD COLUMN watchdog_retried    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN watchdog_threshold_minutes INTEGER;  -- NULL = adaptive
ALTER TABLE agents ADD COLUMN model               TEXT;            -- claude model for re-dispatch
ALTER TABLE agents ADD COLUMN last_task           TEXT;            -- task content, set by send-task
ALTER TABLE agents ADD COLUMN busy_since          TEXT;            -- UTC ISO, set by get-agent
```

### New: `agent_completions` table (for adaptive threshold)

```sql
CREATE TABLE IF NOT EXISTS agent_completions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  role          TEXT NOT NULL,
  duration_secs REAL NOT NULL,
  completed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_completions_role_date
  ON agent_completions(role, completed_at);
```

### New: `watchdog_events` table (telemetry — implement in v1)

```sql
CREATE TABLE IF NOT EXISTS watchdog_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id      TEXT NOT NULL,
  thread_id     TEXT,
  event_type    TEXT NOT NULL,  -- 'prompt_resolved', 'stalled', 'crashed', 'recovered', 'retry_blocked'
  snapshot_path TEXT,
  created_at    TEXT NOT NULL
);
```

Retention: watchdog cleans `watchdog_events` older than 30 days on startup. Recovery snapshot files: keep last 100 per agent (prune oldest on write).

---

## CLI Changes

### Modified commands

| Command | Change |
|---|---|
| `get-agent` | Set `agents.busy_since = now`, `agents.model = args.model` (if provided) |
| `send-task` | After paste: write prompt content to `agents.last_task` |
| `complete-agent` | Insert row into `agent_completions` with `duration_secs = now - agent.busy_since` |

### New commands

| Command | Signature | Notes |
|---|---|---|
| `set-watchdog` | `<agent_id> <minutes\|off>` | Per-agent threshold override |
| `stop-watchdog` | — | SIGTERM the watchdog daemon; equivalent to the teardown step in `cmd_stop` |

### Updated: `cmd_start` / `cmd_stop`

`cmd_start`: after `db.init_db()`, check PID file — if watchdog not already running, `subprocess.Popen([sys.executable, watchdog_script], start_new_session=True, stdout=log_file, stderr=log_file)`. Write PID to `~/.juggle/watchdog.pid`.

`cmd_stop`: read `~/.juggle/watchdog.pid`, `os.kill(pid, signal.SIGTERM)`, remove PID file.

---

## Surfaces

| Event | Action item? | Cockpit notification? | Log? |
|---|---|---|---|
| Allowlist prompt auto-resolved | No | Yes (`notifications_v2` row) | Yes (`watchdog.log`) |
| First stall/crash + auto-retry | Yes (high priority) | No | Yes |
| Successful re-dispatch | Yes (normal priority) | No | Yes |
| Retry blocked (second stall) | Yes (high priority) | No | Yes |

---

## File Layout

```
~/github/juggle/
  scripts/
    juggle-agent-watchdog        # new daemon script
  src/
    juggle_db.py                 # schema migration + agent_completions + watchdog_events
    juggle_cmd_agents.py         # get-agent / send-task / complete-agent modifications
                                 # + cmd_set_watchdog + cmd_stop_watchdog
    juggle_cmd_threads.py        # cmd_start / cmd_stop: PID file lifecycle
    juggle_cli.py                # wire new subcommands
  docs/superpowers/specs/
    2026-05-17-juggle-agent-watchdog.md   # this file
```

Snapshots and logs:
```
~/.juggle/
  watchdog.pid
  watchdog.log
  watchdog/
    snapshots/<agent_id>.txt             # rolling current snapshot (1 per agent)
    recovery/<agent_id>-<unix_ts>.txt    # 500-line recovery snapshot on stall/crash
```

---

## Out of Scope (v1)

- Distinguishing legitimate slow operations (large web search, big grep) beyond the content-diff + grace-window heuristic.
- Pre-emptive kill of busy agents without a stall trigger.
- Watching non-Juggle Claude sessions (only `juggle.db` agents).
- UI for browsing/replaying recovery snapshots.
- Watchdog for the cockpit or monitor processes themselves.

---

## Devil's Advocate

### 1. Race condition: user sends a new task while watchdog is mid-recovery

**Problem:** User sees an agent stall, manually sends a new task via `send-task` at the same moment watchdog is running `kill-pane` + re-dispatch. The new task lands in a dead pane or on the wrong agent.

**Mitigation:** Watchdog checks `agent.status` immediately before each recovery action. If it's no longer `busy` at the point of kill, abort the recovery (the orchestrator already released or completed it). This is a TOCTOU window, but the 30 s poll cycle makes concurrent manual intervention + watchdog action within the same cycle rare. For v1, the window is accepted; v2 can add a `recovering` status with a DB advisory lock.

### 2. False positive cost: re-dispatching an agent that was actually working

**Problem:** A coder doing a long `docker build` or web search may not emit pane output for > 5 min (the cold-start coder default). Watchdog re-dispatches, duplicating work.

**Mitigations:**
- `Thinking…` grace period: one extra cycle before classifying as stalled.
- Adaptive threshold kicks in after 10 samples — once the role's median is calibrated, 2× median handles slow operations correctly.
- Per-agent override: user can `juggle set-watchdog <agent_id> 15` for known-slow tasks.
- Cold-start coder default (5 min) is intentionally conservative. It won't catch a 10-minute build. Accepted trade-off — false negatives (missing real stalls) are cheaper than false positives on day 1.

### 3. Snapshot storage: unbounded growth

**Problem:** Recovery snapshots accumulate indefinitely in `~/.juggle/watchdog/recovery/`.

**Mitigation:** On each write, prune files for that agent so at most 100 exist total across all agents. Implement in the snapshot-write helper. `watchdog_events` rows older than 30 days are deleted on watchdog startup.

### 4. Cold-start over-triggering: fixed defaults too short for long coder tasks

**Problem:** On day 1 before any completions are recorded, a coder writing a large feature has only 5 min before the watchdog fires. In practice, the first re-dispatch will often be wrong.

**Trade-off accepted.** The 5 min cold-start is calibrated for the common case (quick coders). Users with known-long tasks should set a per-agent override. After ≥ 10 completions, the adaptive threshold takes over. The retry guard (max 1) bounds damage — worst case is one wasted re-dispatch per role until calibration kicks in.

### 5. Pane ID reuse: tmux may recycle pane IDs

**Problem:** Agent A's pane is killed and recycled to agent B. Watchdog still has a snapshot keyed to agent A's pane_id, or `verify_pane` returns true for the wrong process.

**Mitigation:** Snapshots are keyed by `agent_id` (UUID), not `pane_id`. The DB `pane_id` is what we pass to `capture-pane`, but the snapshot file is `snapshots/<agent_id>.txt`. If pane is reused, the new content will differ from the old snapshot — the agent would be classified Working, not stalled. The bigger risk is watchdog detecting a new process's output as valid agent output. Mitigated by the existing `_pane_has_juggle_agent_env` check in `reap_stale_agents` — the watchdog can call this before classifying any pane as active.

### 6. `last_task` not set if agent was dispatched before this version

**Problem:** Existing busy agents at the time of deployment have `last_task = NULL`. Watchdog can't re-dispatch them.

**Behavior:** If `agent.last_task` is NULL, recovery flow stops at step 9 (after kill and action item). File a high-priority action item: `"🚨 [LABEL] agent stalled — no task content to replay; re-dispatch manually"`. This is the safe fallback.

### 7. `complete-agent` modifies `agents.status` before computing `busy_since` duration

**Problem:** `complete-agent` calls `update_agent(status='idle', last_active=now)`, which overwrites `last_active`. Then when we try to compute `now - agent.busy_since`, `busy_since` is the right field but we need to read it before the update happens.

**Mitigation:** `complete-agent` reads `agent.busy_since` FIRST, inserts into `agent_completions`, THEN calls `update_agent`. The new schema adds `busy_since` as a separate column — it is not updated by `complete-agent`, only by `get-agent`.
