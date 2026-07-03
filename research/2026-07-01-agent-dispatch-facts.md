# Juggle agent dispatch + pool facts (2026-07-01)

Scope: sub-agent selection, spawn, lifecycle, worktree binding, tick dispatch, affinity.
Source refs are `file:line` at HEAD (juggle repo `src/`). Feeds verify-fallback design
(on failed-verify, hand verify to an idle agent, preferring the original agent).

---

## 1. `agent get <thread> --role R [--model M]` — selection

CLI `cmd_get_agent` → `juggle_dispatch_core.acquire_agent(...)`.
- `juggle_cmd_agents_lifecycle.py:19-62` (thin wrapper; snapshots existing ids to print " new").
- Core selection: **`juggle_dispatch_core.acquire_agent`** `juggle_dispatch_core.py:23-105`.

**Reuse path (default, `fresh=False`)** — `acquire_agent` `:57-76`:
1. Iterate `db.get_ranked_idle_agents(thread_id, role=role)` — best-first (see §7).
2. Hard filters, candidate skipped unless ALL match:
   - `repo_path == target_repo` (`:62-63`; `target_repo` defaults to `_spawn_repo_path()` when `repo` arg is None, `:50-52`).
   - `candidate.role == role` (`:64-65`) — **role is a hard gate on reuse**.
   - `candidate.harness == requested_harness` (`:66-67`; harness from arg or `settings.agent.harness` or `"claude"`).
   - pane is paste-ready: `wait_for_ready_to_paste(pane_id, attempts=1)` (`:68-69`).
   - CAS win: `db.cas_assign_agent(candidate.id, thread_id)` (`:70-71`).
3. First candidate passing all → reused; `cd <repo>` sent to reset pane cwd (`:72-75`).

**Spawn path** — `acquire_agent` `:78-94`: if no idle candidate survives (or `fresh=True`,
`:57`), `mgr.spawn_agent(db, role or "researcher", model=…, harness_override=…, effort=…)`
then mark `status=busy, assigned_thread, busy_since` (+ `model`/`repo_path` if passed).
- CLI prints `[juggle] No idle agent available, spawned new agent …` to stderr (`lifecycle.py:55-60`).

Always ends with `db.set_conversation_background(thread_id)` (`:104`) → thread state=background.

## 2. Pool cap — `JUGGLE_MAX_BACKGROUND_AGENTS`

- `MAX_BACKGROUND_AGENTS` read from env in `juggle_db.py` (imported `:40`).
- **Cap is on TOTAL agents, not busy ones**: `acquire_agent:45-48` — `if len(db.get_all_agents())
  >= MAX_BACKGROUND_AGENTS: raise CapacityError`. Idle-but-alive agents count against it.
- **Behavior at cap = error/defer, NOT block/queue**:
  - CLI `cmd_get_agent` catches `CapacityError` → prints `Error: Agent pool full (N max)…` +
    `sys.exit(1)` (`lifecycle.py:46-50`).
  - Tick path: `CapacityError` → archive the just-created thread, reset topic/task to ready,
    add to `deferred`, **`break`** (retry next tick). Topics `graph_dispatch.py:255-260`;
    flat tasks `:354-359`.
- `spawn_agent` has a second identical guard raising `ValueError` (`juggle_tmux.py:578-582`)
  — reached only if a slot freed between the acquire check and spawn.
- Note: the cap blocks reuse too, because the count check (`:45`) runs BEFORE the idle walk.
  A full pool of idle agents cannot be reused — get errors out. (Relevant to verify-fallback:
  if pool is full, even the original idle agent is unreachable via `acquire_agent`.)

## 3. Lifecycle after `agent complete` — stays idle, reused across threads

`cmd_complete_agent` `juggle_cmd_agents_complete.py:19-202`:
- `:125` `agent = db.get_agent_by_thread(thread_uuid)` (matches `assigned_thread AND status='busy'`,
  `dbops/agents.py:168-174`).
- `:137` `db.update_agent(agent.id, status="idle", assigned_thread=None)`.
  **The agent is NOT decommissioned — it returns to the pool as idle and is reusable by ANY
  thread** subject to the §1 filters.
- ⚠️ **complete does NOT append the thread to `context_threads`, does NOT clear `last_task`/`model`.**
  Only `cmd_release_agent` does that bookkeeping (see below). → affinity is NOT recorded on complete.

Decommission / release paths:
- `cmd_release_agent` `juggle_cmd_agents_lifecycle.py:65-167`: sets idle, **appends `assigned` to
  `context_threads` (last-10)** `:101-112`, mirrors dispatch payload to thread `:120-128`, then
  **clears** `last_task/model/…` `:134-141`. If `status==decommission_pending` → hard decommission `:78-84`.
- `cmd_decommission_agent` `:170-180` → `JuggleTmuxManager().decommission_agent` (kill pane + delete row).
- **Reaper** `juggle_tmux.py:774-830`: idle agents past `agent_idle_ttl_secs` → `decommission_agent`
  (`:819-829`); dead-pane agents deleted (past boot grace) `:799-812`; `decommission_pending` killed `:814-817`.
- **One-shot reconcile** `reconcile_oneshot_agents` `:667-757`: non-interactive (one-shot) busy agent
  whose PID died past ~20s grace → `status=idle, assigned_thread=None` + HIGH "died without
  complete-agent" action item (`:744-754`). Run at `agent list` (`pool.py:52-56`).
- `cmd_fail_agent` persistent path also frees the agent: `status=idle, assigned_thread=None`
  (`complete.py:246-248`).

## 4. Role handling — typed at spawn, hard gate on reuse

- Agents are **role-typed at spawn**: `create_agent(role=…)` at `spawn_agent`
  `juggle_tmux.py:590,615`; role persisted on the row and used to pick harness/template/model.
- **Reuse requires exact role match** (`acquire_agent:64-65`) → **an idle `coder` is NEVER
  reused as a `researcher`.** Role mismatch just falls through to spawning a new agent.
- No per-task role reassignment of an existing pane; role is immutable for an agent's life.

## 5. Worktree binding — thread-scoped, coder/planner only

- Auto-create lives in `send_task_to_agent` `juggle_dispatch_core.py:153-217`, gated on
  `_role in ("coder","planner")` AND a thread exists (`:157`).
- Trigger: no existing `thread.worktree_path`, a resolvable repo base, and not `allow_main`
  (`:178-192`). Uses `_com._create_worktree(repo, label, DEFAULT_WORKTREE_ROOT)`; branch is
  deterministic `cyc_<thread_label>` (label = `user_label` or `id[:6]`).
- **Worktree is bound to the THREAD, not the agent**: persisted via `db.update_thread(thread_id,
  worktree_path=…, worktree_branch=…, main_repo_path=…)` (`:182-187`). Agent only contributes
  `repo_path` as a base hint.
- Tick pre-stamps `worktree_branch=cyc_<label>` on the thread before send (`graph_dispatch.py:245-247`).
- Hard fail if a coder/planner task cannot get an isolated worktree and `allow_main` is false
  (`:194-199`).
- **Design implication:** re-dispatching a verify to a *different* idle agent is safe wrt worktree
  — the worktree travels with the thread; any agent sent the same `thread_id` gets the same
  `cd <worktree>` preamble.

## 6. Watchdog tick dispatch path — SAME core as `agent get`, CAS-guarded

- Tick entry `graph_tick` `juggle_graph_dispatch.py:167-299` (topics) + `_dispatch_flat_task_fallback`
  `:302-383` (parentless tasks). Watchdog poll loop only calls `graph_tick`.
- Claim is atomic and separate from agent acquisition:
  - `claim_topic` / `claim_task` `:49-60` = single conditional `cas_state(ready→dispatching)`;
    `rowcount==1` is the claim → **prevents double-dispatch of the same node**.
  - Then create thread, bind topic→thread BEFORE send (`:251`; comment DA round-2 MAJOR-4) so a
    crash leaves it thread-bound and the stale sweep won't re-dispatch.
- Agent acquisition is the **same path as `agent get`**: `_dispatch_via_pool` → `dispatch_node`
  → `acquire_agent` + `send_task_to_agent` (`:98-107`, `juggle_dispatch_core.py:267-293`).
  So tick reuse/spawn obeys the identical §1 filters (role hard-coded `TASK_ROLE="coder"`, `:35,107`).
- Double-dispatch avoidance = (a) node-level CAS claim, (b) agent-level `cas_assign_agent` CAS,
  (c) `set_topic_thread`/`set_task_thread` before send, (d) `reconcile_orphaned_inflight` +
  `sweep_stale_claims` (>10min, no dispatch edge → ready) `:63-84,189-196`.
- On send failure `dispatch_node` frees the agent (`status=idle, assigned_thread=None`) `:287-293`.

## 7. Agent↔thread affinity — EXISTS but weak, populated only on release

- **`get_ranked_idle_agents(thread_id, role)`** `dbops/agents.py:117-145` scores idle agents:
  - **+2 if `thread_id` in the agent's `context_threads`** (prior work on this thread)
  - +1 if `agent.role == role`
  - tie-break: most recent `last_active`; returns best-first.
- `context_threads` is the ONLY affinity signal. **It is written ONLY by `cmd_release_agent`**
  (`lifecycle.py:101-112`, last-10 ring) — **NOT by `cmd_complete_agent`**.
- ⇒ In the normal autopilot flow (agent calls `complete-agent`, never `release`), the original
  agent is set idle but its `context_threads` never records the thread. **The +2 affinity bonus
  does not fire**, so a re-dispatch to the same thread has no built-in preference for the original
  agent beyond role/repo/harness match + recency tiebreak.

---

## Verify-fallback design flags (prefer original agent, else any idle)

**Makes it EASY:**
- The scoring hook already exists: `get_ranked_idle_agents` +2 for `thread_id ∈ context_threads`.
  Re-dispatching the SAME `thread_id` for the verify would naturally prefer the original agent
  IF that thread were in its `context_threads`.
- Worktree is thread-bound (§5) → any agent handed the verify gets the correct worktree/branch
  automatically. No per-agent state to migrate.
- Tick and CLI share one acquisition core (§6) → a fallback added in `acquire_agent`/scoring
  benefits both paths at once.
- Role/harness/repo already narrow the idle set to compatible agents.

**Makes it HARDER (gaps to close):**
1. **Affinity not recorded on complete (§3, §7).** `cmd_complete_agent:137` sets idle but skips
   `context_threads`. To "prefer the original agent," complete must append `thread_uuid` to the
   agent's `context_threads` (mirror `release`'s `:101-112`), OR the verify-dispatch must look up
   the original agent explicitly (e.g. via the dispatch ledger / `last_dispatched_*` fields the
   release path writes to the thread, `lifecycle.py:120-128`).
2. **No stored "original agent id" on the thread/task.** Affinity is heuristic (context ring),
   not an explicit pointer. `get_agent_by_thread` only finds a *currently busy* agent
   (`dbops/agents.py:168-174`), useless once the agent went idle. Consider persisting the
   completing agent's id on the thread/task at complete for a deterministic "prefer original."
3. **Idle agent may be reaped before verify.** Idle TTL (`agent_idle_ttl_secs`) decommissions the
   original agent (`juggle_tmux.py:819-829`); one-shot agents' processes exit entirely. "Prefer
   original, else any idle" must gracefully fall back to spawn when the original is gone.
4. **Pool-full blocks even the original (§2).** `acquire_agent:45` errors when `len(all_agents) >=
   cap` before the idle walk — a full pool means the original idle agent can't be acquired either;
   verify-fallback must handle `CapacityError` (defer/retry, like the tick).
5. **Role gate (§4).** Original agent must have the role the verify wants. If verify runs as a
   distinct role, exact-match filter (`acquire_agent:64`) excludes the original coder → new spawn.
   Verify likely wants `coder`/same role to reuse.
6. **verify-failed already has a state.** `mark_graph_topic`/`mark_graph_task` map a red verify to
   `failed-verify` (`juggle_cmd_agents_graph_topics.py:53,75-76`, states set
   `:20`), currently a terminal-ish failure that propagates + files a HIGH action item — NOT a
   re-dispatch. The verify-fallback would hook here (or in the tick reconcile) to instead re-enter
   the ready set / re-acquire preferring the original agent, rather than blocking dependents.
