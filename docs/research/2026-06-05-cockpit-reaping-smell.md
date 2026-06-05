# Architecture Findings: Cockpit Reaping Smell

**Date:** 2026-06-05  
**Status:** Read-only analysis — no code changes  
**Scope:** `src/juggle_tmux.py`, `src/juggle_cockpit.py`, `src/juggle_cli.py`, `src/juggle_cmd_agents.py`, `src/juggle_watchdog.py`, `src/juggle_db.py`

---

## 1. What `reap_stale_agents` Does (with line evidence)

**File:** `src/juggle_tmux.py:460–541`

```python
def reap_stale_agents(db, mgr):  # :460
```

The function has two structurally distinct passes:

### Pass 1 — DB→tmux (lines 480–518): "Is each DB agent's pane still alive?"

Iterates `db.get_all_agents()`. For each agent:

1. **Pane gone + boot grace respected** (lines 485–497):
   ```python
   if not mgr.verify_pane(a["pane_id"]):           # :485
       if (now_ts - dt).total_seconds() < cold_start_grace:
           continue  # still within boot window — skip  # :493
       db.delete_agent(a["id"])                    # :496
   ```
   `cold_start_grace` = `settings.get("agent_boot_grace_secs", 120)` (line 474).  
   A fresh agent whose pane disappears is **protected for 120 s**.

2. **Decommission-pending** (lines 500–503): kills pane + deletes DB record.

3. **Idle past TTL** (lines 505–518): decommissions if `last_active > agent_idle_ttl_secs` old.

### Pass 2 — tmux→DB (lines 520–539): "Are there orphan panes with no DB record?"

```python
known_pane_ids = {a["pane_id"] for a in db.get_all_agents()}   # :522
for pane_id in result.stdout.strip().splitlines():             # :532
    if pane_id in known_pane_ids:
        continue
    if _pane_has_juggle_agent_env(pane_id):
        mgr.kill_pane(pane_id)                                 # :536
```

**Pass 2 applies zero boot grace.** Any pane that has `JUGGLE_IS_AGENT=1` but no DB record is killed immediately, unconditionally.

---

## 2. Every Caller of `reap_stale_agents` (and `kill_pane`)

### `reap_stale_agents` callers

| Caller | File:line | Context | Cadence |
|--------|-----------|---------|---------|
| **Cockpit** | `juggle_cockpit.py:363–364` | Inside `_refresh()`, throttled by `_last_reap` 60s check | Every 60 s (triggered by 1 s timer or any key event) |
| **CLI main** | `juggle_cli.py:907–911` | Before every CLI subcommand dispatch | Every CLI invocation |
| **`get-agent` / assign** | `juggle_cmd_agents.py:539–541` | Before checking pool size and spawning | Every agent assignment |

### Direct `kill_pane` callers (not via `reap_stale_agents`)

| Caller | File:line | Context |
|--------|-----------|---------|
| Watchdog — never-tasked | `juggle_watchdog.py:614` | Agent never got a task, past boot grace |
| Watchdog — crash handler | `juggle_watchdog.py:640` | Stalled/crashed agent kill + DB delete |
| Watchdog — failed spawn | `juggle_watchdog.py:710` | Rollback after spawn failure |
| `decommission_agent` | `juggle_tmux.py:419` | Called by Pass 1 idle-TTL path |

**Key finding:** The watchdog does **NOT** call `reap_stale_agents`. It calls `kill_pane` directly at specific, intentional points — and it properly respects boot grace before killing never-tasked agents (`juggle_watchdog.py:596–620`).

---

## 3. Does the Cockpit's Refresh Path Mutate State?

**Yes. This is the architectural smell.**

```python
# juggle_cockpit.py:348 — _refresh() is called every REFRESH_INTERVAL (default 1.0s)
def _refresh(self) -> None:
    ...
    now = time.time()
    if now - self._last_reap >= 60 and self._cockpit_mgr is not None:  # :361
        try:
            from juggle_tmux import reap_stale_agents
            reap_stale_agents(self._db, self._cockpit_mgr)             # :364
            self._last_reap = now
```

`_refresh` is also called from at least 14 other places in `juggle_cockpit.py` (lines 463, 468, 473, 493, 518, 542, 568, 619, 647, 748, 787, 795, 803) — key events, action handlers, etc. The 60s throttle gates actual reaping, but the **mutation (killing panes and deleting DB rows) fires from the dashboard's read path**.

`REFRESH_INTERVAL = _SETTINGS["cockpit"]["refresh_interval_secs"]` defaults to `1.0s` (from `juggle_settings.py:37`).

**Verdict: The cockpit IS calling `reap_stale_agents` directly — not via a shared read function that reaps as a side effect.** It was explicitly added to `_refresh()`. The mutation is intentional but architecturally wrong: a dashboard component that performs destructive operations on the agent pool.

---

## 4. How the Watchdog Invokes Reaping

The watchdog does **not** use `reap_stale_agents` at all. It calls `mgr.kill_pane()` directly at:

- `juggle_watchdog.py:614` — never-tasked agents past grace period (respects `_BOOT_GRACE_SECS = 120.0`, line 62)
- `juggle_watchdog.py:640` — crashed/stalled agents (after recovery snapshot)
- `juggle_watchdog.py:710` — failed pane spawns (rollback)

The watchdog's loop cadence is not a simple sleep interval visible in the code — it appears to be driven by its Textual UI (`set_interval` calls not found in grep, suggesting it uses an external scheduler or is invoked by the CLI). The watchdog is the **intended** reaper for behavioral failures (crash, stall, never-tasked), but it was never given responsibility for the generic `reap_stale_agents` pass.

**There is no config for the watchdog's check interval that is clearly accessible** from a grep of `juggle_watchdog.py` — only `0.1s` poll sleeps in internal loops (lines 481, 1005), suggesting it runs continuously.

---

## 5. The DB-Row-Not-Yet-Committed Race

**The race is real.** Evidence from `spawn_agent` (`juggle_tmux.py:374–404`):

```python
def spawn_agent(self, db, role, model=None):  # :374
    ...
    pane_id = self.spawn_pane()                          # :400 — pane created in tmux
    self.start_claude_in_pane(pane_id, model, role)      # :401 — Claude process starts
    #  ↑ harness sets JUGGLE_IS_AGENT=1 (juggle_harness.py:51)
    #  ← RACE WINDOW: pane has JUGGLE_IS_AGENT=1, no DB record yet
    agent_id = db.create_agent(role=role, pane_id=pane_id)  # :403 — DB committed
```

`JUGGLE_IS_AGENT=1` is set by the harness launch command (`juggle_harness.py:51`), which is pasted into the pane at line 401 via `start_claude_in_pane`. The Claude process begins executing and sets this env var **before** `db.create_agent()` commits at line 403.

If `reap_stale_agents` runs in this window — which the cockpit does every 60s — Pass 2 will:
1. List all tmux panes with `JUGGLE_IS_AGENT=1`
2. Check against `known_pane_ids` from DB → the new pane is not there yet
3. **Kill the pane**

`_pane_has_juggle_agent_env` checks child process environments via subprocess, so it fires the moment Claude starts loading — not just after full boot. The grace window between pane creation and DB commit is short but real, particularly since `start_claude_in_pane` pastes a command and returns before Claude finishes starting.

---

## Call Graph

```
CLI invocation (every call)
  └─► reap_stale_agents()
        ├─ Pass 1: DB→tmux (honors 120s grace) → decommission_agent / delete_agent
        └─ Pass 2: tmux→DB (NO grace) → kill_pane ← BUG

juggle_cockpit._refresh() (every 60s, triggered by 1s timer or key events)
  └─► reap_stale_agents()
        ├─ Pass 1: DB→tmux (honors 120s grace) → decommission_agent / delete_agent
        └─ Pass 2: tmux→DB (NO grace) → kill_pane ← BUG + SMELL

juggle_cmd_agents.get-agent (every assign call)
  └─► reap_stale_agents()
        ├─ Pass 1: DB→tmux (honors 120s grace)
        └─ Pass 2: tmux→DB (NO grace) → kill_pane ← BUG

juggle_watchdog (continuous)
  ├─ never-tasked agent past grace → kill_pane (respects grace: line 614)
  ├─ crashed/stalled agent → kill_pane (line 640)
  └─ spawn rollback → kill_pane (line 710)
```

---

## Verdict

**"Cockpit performs reaping" is accurate** — the cockpit explicitly calls `reap_stale_agents` from its `_refresh()` method (line 364). It is not a shared read function with reaping as an invisible side effect; it is a deliberate but architecturally misplaced call.

**Two separate problems are conflated:**

1. **Immediate bug (Pass 2 boot grace):** Pass 2 has no boot grace. Any pane in the race window between `spawn_pane()` (line 400) and `db.create_agent()` (line 403) is killed. This is triggered by ALL four callers, not just the cockpit — but the cockpit is the **highest-frequency trigger** (every 60s) when the watchdog is dead.

2. **Architectural smell:** The cockpit is a dashboard and should be read-only. Reaping from `_refresh()` means killing agents is a side effect of viewing the dashboard. Runtime behavior (which agents die and when) changes based on whether the cockpit is open.

---

## Fix Options

### Option A — Minimal Fix (2-line change)

Add boot grace to Pass 2 in `juggle_tmux.py`. Parallel to the Pass 1 grace logic, track pane creation time. Since tmux doesn't store creation time, use `pane_start_time` from `tmux display -p '#{pane_start_time}'`:

```python
# After: if _pane_has_juggle_agent_env(pane_id):
pane_age = _get_pane_age_secs(pane_id)  # via tmux display #{pane_start_time}
if pane_age is not None and pane_age < cold_start_grace:
    continue  # honor boot grace in Pass 2
mgr.kill_pane(pane_id)
```

**Tradeoff:** Fixes the immediate race but leaves the cockpit reaping in place. Violates the principle but is safe and minimal. The DB-commit race window is closed.

### Option B — Remove Reaping from Cockpit

Remove lines 361–365 from `juggle_cockpit._refresh()`. The cockpit becomes purely read-only.

**Tradeoff:** Simple. Eliminates the smell. But if the watchdog is dead and no CLI commands are run, orphan panes accumulate indefinitely. The cockpit was added as a reaper precisely because it's always running. Removing it leaves only the CLI and `get-agent` callers (opportunistic, low-frequency when no work is happening).

### Option C — Clean Architecture: Watchdog Is Sole Reaper

1. Remove `reap_stale_agents` calls from cockpit `_refresh()` and CLI main.
2. Add `reap_stale_agents` (with Pass 2 boot grace fix) to the watchdog's main loop, on a configurable interval (e.g., `watchdog_reap_interval_secs`, default 60s).
3. The `get-agent` / assign path retains its `reap_stale_agents` call — this is a natural mutation context (spawning work) and is acceptable.
4. All read paths (`_refresh`, `list-agents`, `get-agent` display) become side-effect-free.

**Tradeoff:** Watchdog becomes single source of truth for agent lifecycle. Requires watchdog to be healthy. Dead watchdog → no proactive reaping. Mitigated by Option D.

### Option D — Dead Watchdog Detection (complement to C)

Add a watchdog heartbeat column to DB or a lock file. CLI commands check "watchdog last seen > N minutes ago" and surface a warning / auto-restart the watchdog rather than co-opting reaping into read paths.

```
juggle watchdog status  →  "DEAD since 14:32 (8 min ago)"
```

**This directly addresses the "if watchdog is dead, nothing reaps" concern.** Rather than every process compensating for a dead watchdog by doing its own reaping, the system detects and restores the watchdog.

---

## Critical Assessment of "Only Watchdog Reaps" Principle

The principle is sound but has one concrete downside: **a dead watchdog creates unbounded pane accumulation.** The current approach of having CLI and cockpit call `reap_stale_agents` is a compensating control for this failure mode, implemented by spreading reaping responsibility rather than fixing watchdog reliability.

The right answer is Option C + D: make the watchdog the sole reaper AND make watchdog death observable and auto-recoverable. Option B alone (just removing cockpit reaping) is insufficient unless watchdog reliability is addressed.

The cockpit reaping (Option B removal) is the clearest violation of the read-only principle and should be removed regardless. The CLI `main()` reaping on every invocation is lower-risk (mutations happen in mutation contexts) but also technically a smell — it could move to the watchdog under Option C.
