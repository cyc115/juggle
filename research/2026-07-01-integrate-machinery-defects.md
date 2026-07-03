# Integrate / Dispatch-Machinery Defects — RCA Facts Doc

**Date:** 2026-07-01
**Author:** researcher (thread HX), read-only investigation
**Repo:** `/Users/mikechen/github/juggle` @ `main` (graph built from `38fd31f7`)
**Method:** superpowers:systematic-debugging (Phase 1–2: root cause + pattern). No code changed.
**Scope:** three defects surfaced during the 2026-07-01 overnight autonomous run — (B) stale-daemon activation gap, (C) topic wedged in `integrating` after a successful merge, (D) `acquire_agent` raises `CapacityError` before trying reuse.

> Facts + options only. No chosen design — the design decision happens in the main thread.

---

## Defect B — Stale-daemon activation gap

**Symptom:** the watchdog daemon ran 9-hour-old code all night; fixes merged to `main`
(#5038 serialized integrate lock, #5045 config-authoritative `max_agents`) never took
effect until a **manual** daemon restart.

### How the daemon is spawned and how long it lives
- Spawn: `start_watchdog_detached` (`src/juggle_watchdog_singleton.py:174-206`) runs
  `uv run python src/juggle_watchdog_daemon.py` with `start_new_session=True` (detached
  from the cockpit's process group) from `canonical_repo_path()` — the **main worktree**
  (`juggle_watchdog_singleton.py:152-171`). It **pops** `JUGGLE_WATCHDOG_SUPERVISED`
  (`:187`) so the daemon runs **unsupervised** (no launchd KeepAlive).
- Lifetime: `main()` (`src/juggle_watchdog_daemon.py:322-423`) imports every module **once**
  at start, then loops forever (`while _running:` `:380`). Python never re-imports already
  loaded modules, so **only a fresh process picks up new code** — an mtime change alone does
  nothing.
- Singleton: an exclusive per-DB `flock` (`acquire_singleton_lock`,
  `juggle_watchdog_singleton.py:91-111`) — the lock **is** the singleton truth. A self-restart
  via `os.execv` keeps the held fd (no `FD_CLOEXEC`), so the lock survives the re-exec; a
  stop+respawn instead needs the old PID to die (releasing the flock) before the new one
  acquires — `ensure_watchdog` already lock-gates that (`:225-273`).

### Root cause — three separate refresh paths, all with gaps
There is a staleness *check* in the loop, but it is neutered, and the *complete* reload
mechanism that exists is never wired in:

1. **In-loop check watches ONE file and never restarts when unsupervised.**
   `juggle_watchdog_daemon.py:39` `_WATCHDOG_SRC = juggle_watchdog.py`. The loop stats only
   that single file (`:378`, `:381`) and gates on
   `should_exit_for_reload(stale=True, supervised=_SUPERVISED)`
   (`:384`). `should_exit_for_reload` returns `stale and supervised`
   (`src/juggle_watchdog_restart.py:33-40`). Because the daemon is **unsupervised**
   (`_SUPERVISED=False`), it takes the `else` branch (`:391-397`): logs, **re-baselines the
   mtime, and continues without restarting**. So even a change to `juggle_watchdog.py` never
   triggers a reload, and changes to **any other module** (`juggle_integrate_lock.py` = #5038,
   `juggle_settings.py`/`juggle_watchdog.py` `max_agents` = #5045, `juggle_dispatch_core.py`,
   `juggle_graph_dispatch.py`, …) are **never even stat'd**.

   ```python
   # juggle_watchdog_daemon.py:380-397
   while _running:
       new_mtime = _WATCHDOG_SRC.stat().st_mtime if _WATCHDOG_SRC.exists() else 0.0
       stale = new_mtime > _src_mtime
       if stale:
           if should_exit_for_reload(stale=True, supervised=_SUPERVISED):
               ... sys.exit(0)        # only when supervised → never here
           else:
               ... _src_mtime = new_mtime   # unsupervised: re-baseline and keep old code
   ```

2. **The complete hot-restart mechanism is dead code (never called).**
   `juggle_watchdog_restart.py` ships `_collect_mtimes` (stats **all** `src/*.py`, `:74-85`),
   `should_hot_restart` (300 s stability grace, `:43-71`), and `_maybe_hot_restart` (import
   crash-guard + `os.execv` re-exec, `:88-138`). These are imported/re-exported by
   `juggle_watchdog.py:31-34` but **grep confirms they are never invoked** anywhere in the
   daemon loop — only the crippled `should_exit_for_reload` is. The correct, all-files,
   supervisor-independent restart already exists; it is simply not wired into `main()`.

3. **Self-repo integrate explicitly SKIPS restarting the watchdog.**
   After a ff-merge into juggle's own repo, `_run_integrate` calls `_restart_juggle_daemons`
   (`juggle_cmd_integrate.py:394-397`). But that function
   (`src/juggle_integrate_selfrepo.py:22-51`) restarts **talkback** and kills the **monitor**
   only — the watchdog is deliberately excluded: `# Watchdog is owned by the cockpit — no
   restart needed here.` (`:30`). So merging a watchdog fix does **not** refresh the daemon
   that merged it.

**Causal chain:** unsupervised daemon → in-loop check only watches `juggle_watchdog.py` and
never exits when unsupervised → other-module fixes invisible → the one real reload path
(`_maybe_hot_restart`/`os.execv`) is never called → self-integrate's daemon-restart hook
skips the watchdog by design → the process keeps its import-time code (and import-time
constants like `MAX_BACKGROUND_AGENTS`, `juggle_db.py:52`) for 9 h until a human runs the R
hotkey / `stop`+`ensure`.

### "Current version" sources of truth available
- **File mtimes of all `src/*.py`** — already collected by `_collect_mtimes`
  (`juggle_watchdog_restart.py:74`). Zero new infra.
- **git HEAD of the canonical repo** — daemon is launched from `canonical_repo_path()` (the
  main worktree), so `git -C <repo> rev-parse HEAD` at boot vs. per-tick is an exact
  "code advanced past what I loaded" signal.
- **`.claude-plugin/plugin.json` `version`** — weakest: the daemon runs from the **repo**
  (`uv run … src/…`), not the installed plugin cache, and the version only bumps on manual
  release, so it misses between-release merges.

### How it is restarted today
Manual only: cockpit **R** hotkey → `restart_watchdog` (`juggle_watchdog_singleton.py:325-345`,
stop → wait for lock release → `ensure_watchdog(force=True)`), or **W** toggle
(`toggle_watchdog`, `:310-322`). No automatic self-refresh.

### Existing tests on this path
`tests/test_watchdog_daemon.py`, `tests/test_juggle_watchdog.py`,
`tests/test_ensure_watchdog_debounce.py`, `tests/test_cockpit_watchdog_owner.py`.
(`should_hot_restart`/`_collect_mtimes` have unit coverage but the loop never calls them.)

### Candidate fix directions (trade-offs)
- **Wire the existing `_maybe_hot_restart` into the loop** (replace the single-file
  `should_exit_for_reload` block at `:380-397`). *Pro:* reuses tested code, all-files + grace
  + import-guard + `os.execv` keeps the flock. *Con:* `os.execv` inside a live tick needs
  care (finish/land current tick first; the 300 s grace already debounces edit flurries).
- **git-HEAD check per tick** (compare boot HEAD to `rev-parse HEAD`; re-exec on change).
  *Pro:* precise "main advanced" semantics, ignores irrelevant untracked churn. *Con:*
  a git subprocess per tick; must resolve the same worktree the daemon was launched from.
- **Make self-repo integrate restart the watchdog** (extend `_restart_juggle_daemons` /
  drop the `:30` exclusion). *Pro:* immediate refresh exactly when juggle's own code merged.
  *Con:* runs in the **agent's** process, must respawn a *detached* sanctioned daemon and not
  race the singleton lock; doesn't help non-self-repo staleness (only juggle's own fixes).

---

## Defect C — Topic stuck in `integrating` after a successful merge

**Symptom:** topic `T-fix-max-agents-config` stayed in state `integrating` after its integrate
completed and merged to `main`; this gated the watchdog tick from dispatching the next chained
task. `graph reconcile` did **not** repair it — required manual SQL to force `verified`.

### Empirical evidence (read-only copy of prod DB)
```
tid                        state       merged_sha   updated_at
T-fix-max-agents-config    verified    NULL         2026-07-01T17:12:56   ← the incident
T-serial-integrate-lock    verified    92cb0c167a   2026-07-01T08:40:50
T-verify-fallback          verified    1b54b4a36b   2026-07-01T07:50:22
… (all other recent verified topics carry a real merged_sha)
```
`T-fix-max-agents-config` is now `verified` (forced by the manual SQL described in the
incident) **yet its `merged_sha` is still NULL**, while every sibling recorded a real SHA.
Its single task node `fix-max-agents-config` is `verified`. So: the work merged, but the
topic's `merged_sha` was **never recorded**. That NULL is the whole defect.

### The two-part `verified` contract
`verified` requires BOTH, written at DIFFERENT times by DIFFERENT code:
1. a state-machine transition `integrating --integrate_ok--> verified`
   (`src/dbops/db_node_machine.py:44`), AND
2. a recorded `merged_sha` that is an ancestor of `main`
   (`_verified_allowed` → `topic_is_merged`, `src/dbops/graph_guards.py:114-132`).

`topic_transition` enforces (2) as a hard gate on (1):
```python
# src/dbops/db_topics.py:76-80
if new_state == "verified" and not _verified_allowed(db, topic_id):
    raise UnmergedVerifyRefused(          # class UnmergedVerifyRefused(ValueError)  (:47)
        f"refusing to verify topic {topic_id!r}: its work is not merged into main …")
```

### Who sets `integrating`; who owns `integrating`→`verified`
- Both events fire inside a **single** call to `mark_topic_completion`
  (`src/dbops/db_topics_marking.py:26-53`): it walks the topic legally to `integrating`
  (`integrate_start`) and then immediately applies `integrate_ok`→`verified` — as **separate,
  individually-committed `topic_transition` calls** (no enclosing transaction, `:47-53`).
- That call is driven by `mark_graph_topic` (`src/juggle_cmd_agents_graph_topics.py:55-92`),
  invoked at the **end** of `cmd_complete_agent` (`src/juggle_cmd_agents_complete.py:184-192`),
  which runs in the **agent's own pane process** — *after* `_run_integrate` already merged and
  pushed (`juggle_cmd_agents_complete.py:43-44`).

### Root cause — `merged_sha` is recorded fail-soft, BEFORE the push, against `origin/main`
`_run_integrate` records the SHA and pushes in this order (juggle is `push_mode="direct"`,
per `~/.juggle/config.json`):
```python
# src/juggle_cmd_integrate.py
369  _record_merged_sha(db, thread_uuid, main_repo_path, local_main)   # RECORD (local main tip)
371  if push_mode == "direct":
373      subprocess.run(["git","-C",main_repo_path,"push","origin", f"{local_main}:{local_main}"])  # PUSH
```
`_record_merged_sha` (`src/juggle_integrate_mergedsha.py:15-76`) will only persist the SHA if
**Guard 2** passes: the SHA must be an ancestor of `_canonical_main_ref(repo)`. And
`canonical_main_ref` (`src/juggle_repo_binding.py:134-155`) **fetches and prefers
`origin/<main>`** over the local ref:
```python
for branch in ("main","master"): git fetch origin <branch>
for candidate in ("origin/main","origin/master","main","master"): return first that resolves
```
At line 369 the freshly ff-merged commit is on **local** `main` but **not yet on `origin/main`**
(the push is line 371). So `merge-base --is-ancestor <local_sha> origin/main` returns
non-zero → Guard 2 fails → `merged_sha` is left **NULL**. Every failure is **silently
swallowed**: `_record_merged_sha` wraps everything in `try/except: pass` (`:26,:75`) and logs
only at WARNING through an unconfigured logger in the agent process (so nothing reached
`~/.juggle/watchdog.log`, which in any case stopped at 12:44 — the stale daemon, Defect B).
Critically, **`_run_integrate` still returns `(True, …)`** — the merge succeeded, so the
missing SHA is invisible to the caller.

Then, downstream:
```python
# src/juggle_cmd_agents_graph_topics.py:73-82
try:
    state = db_topics.mark_topic_completion(db, topic["id"], integrate_ok=…, verify_ok=…)
except ValueError as e:                 # UnmergedVerifyRefused IS a ValueError
    print(f"Warning: graph topic … not marked — {e}"); return   # ← swallowed
```
`mark_topic_completion` walks to `integrating` (committed), then `integrate_ok`→`verified`
raises `UnmergedVerifyRefused` (NULL `merged_sha`), caught here as `ValueError`, printed as a
warning, and **the function returns with the topic left at `integrating`.**

**Causal chain:** `push_mode=direct` + record-before-push + `canonical_main_ref` preferring
`origin/main` → Guard 2 ancestry check fails → `merged_sha` NULL (silently) → `_run_integrate`
returns success anyway → `mark_topic_completion` commits `→integrating` then raises on
`→verified` → `mark_graph_topic` swallows the `ValueError` → topic wedged at `integrating`,
which is an `IN_FLIGHT_STATE` that blocks the next chained dispatch.

> Note on why most topics DO record a SHA: the outcome is timing/network-sensitive
> (concurrent integrate that already pushed the parent, an origin fetch that fails and falls
> back to local `main`, etc.). The record-before-push ordering makes the direct-push path
> fragile rather than deterministically broken; the overnight run (with the stale
> serialized-lock daemon, Defect B) hit the failing timing.

### Why `graph reconcile` cannot repair it
`graph reconcile` → `reconcile_project_topics` → `reconcile_topic_state`
(`src/dbops/db_topics_reconcile.py:47-119`) **re-derives** the topic state from its member
tasks. With all tasks `verified` it applies the **same** gate:
```python
# db_topics_reconcile.py:80-91
if all(s == "verified" for s in task_states):
    target = "verified" if _verified_allowed(db, topic_id) else "integrating"
```
`_verified_allowed` is still False (SHA still NULL) → it re-derives `integrating` → **no-op**.
The invariant reconcile checks (recorded `merged_sha` ancestor-of-main) is exactly the one
that is missing; it has **no fallback** that asks "are this topic's branch commits already in
`main`?" and **no path to (re)record** `merged_sha`. The per-tick orphan reconciler is even
further off: `reconcile_orphaned_inflight` (`src/juggle_graph_reconcile.py:65-112`) scans only
`kind='task'` nodes (`:82` `WHERE kind='task'`), never topics, and its `_HEAL_EVENT` for
`integrating` is `integrate_fail`→`failed-integration` (`:42-46`) — it deliberately refuses to
touch a maybe-merged in-flight node, so even if it applied to topics it would mark
`failed-integration`, not `verified`.

**Invariant that WOULD have caught it:** "a topic whose bound branch commits are already an
ancestor of `main` (or whose task set is all-verified and whose branch is gone because it
merged) must be `verified`, even when `merged_sha` was never recorded" — i.e. a reconcile that
can re-derive/repair `merged_sha` from live git history rather than trusting only the
integrate-time write.

### Existing tests on this path
`tests/test_integrate_phantom_sha.py` (the Guard-1/Guard-2 behavior of `_record_merged_sha`),
`tests/test_db_topics.py`, `tests/test_topic_invariants.py`, `tests/test_topic_reconcile.py`,
`tests/test_topic_tick_sweep.py`, `tests/test_graph_orphan_reconcile.py`,
`tests/test_integrate.py`, `tests/test_autopilot_guards.py`,
`tests/test_graph_autopilot_integration.py`. **No test asserts the record-before-push /
origin-ancestry interaction, nor that a successful direct-push integrate leaves a non-NULL
`merged_sha`.**

### Candidate fix directions (trade-offs)
- **Record `merged_sha` AFTER the push** (move `_record_merged_sha` below the direct-push
  block, or record against **local** `main`). *Pro:* eliminates the ordering hole at the
  source; the local tip is unambiguously present. *Con:* need to keep `push_mode=pr`/`none`
  working (record the branch/local ref there); Guard 2 semantics may need to check local main
  rather than origin.
- **Stop swallowing the failure / fail the integrate loudly when `merged_sha` can't be
  recorded** (make `_record_merged_sha` return status; `_run_integrate` surfaces it instead of
  returning `True`). *Pro:* turns a silent wedge into a visible, actionable failure. *Con:*
  could block otherwise-successful merges on a transient git hiccup; needs a retry/backfill.
- **Give reconcile a repair path** (when all tasks `verified` and `merged_sha` NULL, re-derive
  it from git: if the recorded/last branch tip — or the topic's committed work — is an ancestor
  of `main`, backfill `merged_sha` then `→verified`). *Pro:* self-heals the exact stuck state
  without manual SQL; complements either fix above. *Con:* needs a durable branch/commit
  handle after integrate clears the worktree fields (`juggle_cmd_integrate.py:391`); must stay
  fail-closed to preserve the "verified ⟺ merged" guarantee (no fail-open regression).

---

## Defect D — `acquire_agent` raises `CapacityError` before trying reuse

**Symptom:** at pool cap, a ready task stalls even though an **idle** agent exists —
`acquire_agent` raises `CapacityError` before scanning for a reusable idle agent. Reuse does
**not** grow the pool (it CAS-reassigns an existing agent), so it should be legal at cap.

### Exact ordering (cap-check BEFORE idle-scan)
```python
# src/juggle_dispatch_core.py:40-81
from juggle_db import MAX_BACKGROUND_AGENTS
...
if len(db.get_all_agents()) >= MAX_BACKGROUND_AGENTS:          # :45  CAP CHECK — counts ALL agents
    raise CapacityError(f"agent pool full ({MAX_BACKGROUND_AGENTS} max) …")   # :46-48
...
agent = None
if not fresh:
    for candidate in db.get_ranked_idle_agents(thread_id, role=role):         # :59  IDLE-REUSE SCAN
        ... role/repo/harness/pane checks ...
        if not db.cas_assign_agent(candidate["id"], thread_id): continue      # :71  reassign, no new pane
        agent = candidate; break
if agent is None:
    agent = mgr.spawn_agent(...)                                              # :85  ONLY here does the pool grow
```
The cap check (`:45`) counts **all** agents (idle + busy) and fires **before** the reuse loop
(`:59-81`). `get_ranked_idle_agents` (`src/dbops/agents.py:117-145`) only ever returns
`status='idle' AND assigned_thread IS NULL` agents, and reuse takes the CAS path (`:71`,
`cas_assign_agent` `agents.py:147-166`) — **no `spawn_agent`, no new pane**. So at cap, a
legal warm-reuse is refused; only the spawn branch (`:83-99`) actually adds to the pool.

### Callers (all inherit the ordering)
- **Watchdog tick dispatch:** `graph_tick` → `_dispatch_via_pool` → `dispatch_node`
  (`juggle_dispatch_core.py:272-298`) → `acquire_agent` (`:286`). `CapacityError` bubbles so
  the tick **defers** (`juggle_graph_dispatch.py:255,354` catch `CapacityError`) — this is the
  overnight stall.
- **CLI `get-agent`:** `cmd_get_agent` (`src/juggle_cmd_agents_lifecycle.py:35-50`) →
  `acquire_agent`; on `CapacityError` prints "Agent pool full … Wait for one to finish" and
  `sys.exit(1)`.

### Why the ordering exists / what a reorder must preserve
The cap check is a blanket "don't exceed `MAX_BACKGROUND_AGENTS` live panes" guard placed
before any work — simplest correct-for-spawn form, but it conflates *reuse* (pool-neutral)
with *spawn* (pool-growing). A "scan-reuse-first, cap-check-only-before-spawn" reorder must
keep every constraint the reuse loop already enforces:
- **role match** (`:64-66` `candidate.role != role → skip`) and the scoring in
  `get_ranked_idle_agents` (`agents.py:141-142`),
- **repo/worktree binding** (`:60-63` `agent_repo != target_repo → skip`),
- **harness match** (`:67-68`),
- **agent health / pane readiness** (`:69` `wait_for_ready_to_paste(attempts=1)`),
- **atomic claim** (`:71` `cas_assign_agent` — guarded UPDATE; loser continues/spawns),
- **clear-on-reuse `/clear` flow** (`:76-79` — Claude harness only: `/clear` + `cd <repo>`
  before the pane gets a new task; must still run on the at-cap reuse path),
- **`fresh=True` must still bypass reuse** (`:58`) and therefore must still hit the cap
  (a forced-fresh request legitimately needs a new pane).

`MAX_BACKGROUND_AGENTS` is an **import-time constant** (`juggle_db.py:52`, resolved from
`resolve_max_agents`/`config_max_agents`, `juggle_settings.py:339-376`, #5045) — so a reorder
interacts with Defect B: a stale daemon also carries a stale cap.

### Existing tests on this path
`tests/test_max_agents_config_cap.py` (the #5045 config-authoritative cap),
`tests/test_dispatch_node.py`, `tests/test_graph_dispatch.py`, `tests/test_clear_on_reuse.py`
(the `/clear` reuse flow), `tests/test_get_agent_busy_guard.py`,
`tests/test_get_agent_harness.py`. **No test asserts "idle agent is reused at pool cap"** —
that is the missing regression pin.

### Candidate fix directions (trade-offs)
- **Reorder: attempt the reuse scan first; only cap-check before the `spawn_agent` branch.**
  *Pro:* directly fixes the stall; reuse is genuinely pool-neutral. *Con:* must move the check
  to guard **only** `:83-99` (spawn), preserving `fresh=True`→cap and all reuse constraints
  above; keep the CAS-loser fallthrough honest (a loser that must spawn re-hits the cap).
- **Count only pool-*growing* outcomes against the cap** (cap-check counts toward whether a
  *new* pane is needed, evaluated after reuse fails). *Pro:* cleanest semantics ("cap = max
  live panes, reuse never adds one"). *Con:* larger change to the function shape; more
  surface to test.
- **Leave `acquire_agent`; make the watchdog tick pre-scan idle agents and prefer reuse before
  calling the capped path.** *Pro:* narrow blast radius, keeps CLI behavior identical. *Con:*
  duplicates reuse logic in the tick, drifting from the single dispatch primitive (violates
  "one source of truth"); the CLI `get-agent` at-cap case stays broken.

---

## Cross-defect notes
- **B amplifies C and D.** The stale daemon (B) ran the pre-#5038 integrate path and a stale
  `MAX_BACKGROUND_AGENTS` all night, which is the environment in which C's fragile
  record-before-push timing failed and D's cap-before-reuse stall bit. Fixing B (auto-refresh)
  shrinks the window for both.
- **Shared anti-pattern: fail-soft writes on a hard gate.** C's `merged_sha` is written
  best-effort/​swallowed yet is a *hard* precondition for `verified`; the swallow turns a
  recoverable miss into a silent permanent wedge with no log trail. Any C fix should make the
  gate-input either reliably written or loudly surfaced.
- **Regression pins to add (per repo policy):** B — "unsupervised daemon re-execs when any
  `src/*.py` (or git HEAD) advances"; C — "direct-push integrate leaves a non-NULL `merged_sha`
  that is an ancestor of `main`, and the topic reaches `verified`" (RED on current code) plus
  "reconcile heals an all-tasks-verified topic whose branch is already in `main`"; D — "an idle
  role-matching agent is reused at pool cap without raising `CapacityError`" (RED on current
  code).
