# Source-Facts: Topic ≡ Graph Node Unification
_Generated 2026-06-18. All citations are file:line in `src/`._

---

## §1 DATA MODEL

### 1.1 `threads` table  (`dbops/schema.py:47–71`)

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | UUID |
| `session_id` | TEXT | session tag |
| `topic` | TEXT | human-written title |
| `status` | TEXT | see §2 below |
| `summary`, `key_decisions`, `open_questions`, `last_user_intent` | TEXT | conversation metadata |
| `agent_task_id`, `agent_result` | TEXT | last dispatched result |
| `show_in_list` | INTEGER | 1=visible |
| `title` | TEXT | derived/display title |
| `created_at`, `last_active` | TEXT | ISO timestamps |
| `last_dispatched_task/role/model` | TEXT | snapshot at last dispatch |
| `worktree_path`, `worktree_branch`, `main_repo_path` | TEXT | git worktree binding |

`thread.status` values (from `dbops/threads.py:392–410`, `juggle_cockpit_model.py:264–295`):  
`active` → `background` (get-agent) → `closed` (complete-agent) / `failed` (fail-agent/release-agent)  
Also: `running`, `done`, `archived` (archive_thread)

---

### 1.2 `graph_tasks` table  (`dbops/schema_graph.py:13–26`)

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | |
| `project_id` | TEXT FK→projects | |
| `title`, `prompt` | TEXT | |
| `verify_cmd` | TEXT | shell command run pre-merge |
| `state` | TEXT DEFAULT 'pending' | see §2 |
| `thread_id` | TEXT | bound thread |
| `handoff` | TEXT | free-text handoff summary |
| `diffstat` | TEXT | pre-merge diff captured at integrate |
| `verified_at` | TEXT | |
| `created_at`, `updated_at` | TEXT | |
| `topic_id` | TEXT FK→graph_topics | **migration 37** (`dbops/migrations_graph.py:208`) — nullable; NULL = pre-3-tier row |

`topic_id` FK was added by migration 37 (`migrations_graph.py:196–242`); existing rows got backfilled.

---

### 1.3 `graph_topics` table  (`dbops/schema_graph.py:37–50`)

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | |
| `project_id` | TEXT FK→projects | |
| `title`, `objective` | TEXT | |
| `state` | TEXT DEFAULT 'pending' | **same machine** as graph_tasks |
| `thread_id` | TEXT | bound thread |
| `handoff` | TEXT | |
| `diffstat` | TEXT | |
| `verified_at` | TEXT | |
| `merged_sha` | TEXT | set by integrate; **required for G1 gate** |
| `created_at`, `updated_at` | TEXT | |
| `is_mirror` | INTEGER DEFAULT 0 | **migration 42** (`migrations_graph.py:264–277`) — mirror topics excluded from dispatch/reconcile/list |

---

### 1.4 `graph_edges` table  (`dbops/schema_graph.py:27–31`)

```
task_id TEXT → graph_tasks(id)
depends_on_id TEXT → graph_tasks(id)
PK (task_id, depends_on_id)
```

Edges reference **graph_tasks** only. Cross-topic deps are derived by finding edges where `task.topic_id` crosses topic boundaries (`dbops/db_topics.py:183–194`).

---

### 1.5 How the two systems are joined today

1. `graph_topics.thread_id` → `threads.id`  
   Set by `set_topic_thread` (`dbops/db_topics.py:113–119`) — written at dispatch time.

2. `graph_tasks.topic_id` → `graph_topics.id`  
   Set at task insert (`juggle_graph_add.py:189–190`).

3. Thread title prefix convention: `graph_tick` creates the thread with  
   `f"[{tid}] {topic['title']}"[:80]` (`juggle_graph_dispatch.py:229`).  
   The `[T-<id>]` prefix is NOT a DB column — it is embedded in `threads.topic`.

4. Cockpit reads `task_state` per thread by joining `graph_tasks.thread_id`:  
   `juggle_cockpit_model.py:219–225` builds `task_state_by_thread` from  
   `SELECT project_id, state, thread_id FROM graph_tasks`.

---

## §2 STATE TRANSITIONS

### 2.1 Task/Topic state machine  (`dbops/db_graph.py:36–71`)

One `_TRANSITIONS` dict, imported and reused by `db_topics.topic_transition`  
(`dbops/db_topics.py:15`): `from dbops.db_graph import _EVENTS, _TRANSITIONS, _cx`

```
pending   --deps_ready--> ready
pending   --dep_fail-----> blocked-failed
pending   --reload-------> pending  (thread_id cleared)
ready     --claim---------> dispatching   [CAS, not task_transition]
ready     --dep_fail------> blocked-failed
ready     --reload--------> pending
ready     --unready-------> pending  [add-task --required-by]
dispatching --dispatch----> running
dispatching --stale_reset-> ready
running   --integrate_start-> integrating
running   --exec_fail------> failed-exec
integrating --integrate_ok -> verified
integrating --integrate_fail-> failed-integration
integrating --verify_fail--> failed-verify
failed-*/blocked-failed --reload-> pending
```

**Topic extra guard**: `verified` additionally requires `_verified_allowed` → `topic_is_merged`  
(G1 gate, `dbops/db_topics.py:52–55`). Topic must have non-NULL `merged_sha` that is  
an ancestor of canonical main.

`TICK_OWNED_STATES = {ready, dispatching, running, integrating, verified}` (`db_graph.py:81–83`)  
`PROTECTED_STATES = {dispatching, running, integrating, verified}` (`db_graph.py:76`)  
`MUTABLE_STATES = {pending, ready, failed-*, blocked-failed}` (`juggle_graph_add.py:26–35`)

---

### 2.2 Where each transition fires

| Transition | Module:Line | Notes |
|---|---|---|
| `pending→ready` (CAS) | `db_graph.recompute_ready:265–283` | sanctioned writer #3 |
| `pending→ready` (topic, CAS) | `db_topics.recompute_topic_ready:230–243` | |
| `ready→dispatching` (task CAS) | `juggle_graph_dispatch.claim_task:47–60` | SQL UPDATE, not task_transition |
| `ready→dispatching` (topic CAS) | `juggle_graph_dispatch_topics.claim_topic:29–38` | SQL UPDATE |
| `dispatching→running` | `juggle_graph_dispatch.graph_tick:288` | topic_transition(dispatch) |
| `dispatching→ready` (stale) | `juggle_graph_dispatch.sweep_stale_claims:63–82` | |
| `dispatching→ready` (topic stale) | `juggle_graph_dispatch_topics.sweep_stale_topic_claims:41–56` | |
| `running→integrating→verified/failed-*` | `dbops/db_topics.mark_topic_completion:255–283` | via complete-agent |
| `running→failed-exec` | `dbops/db_topics.mark_topic_exec_failed:285–302` | via fail-agent |
| `pending→blocked-failed` (propagate) | `dbops/db_topics.propagate_topic_failure:305–327` | |
| `*→pending` (reload) | `juggle_graph_upsert` + `juggle_graph_add` | spec reload |

---

### 2.3 `threads.status` transitions

| Transition | Module:Line |
|---|---|
| create → `active` | `juggle_cmd_threads:create_thread` |
| `active` → `background` | `juggle_cmd_agents_lifecycle.cmd_get_agent:122` |
| `background/active` → `closed` | `juggle_cmd_agents_complete.cmd_complete_agent:105` |
| `background/active` → `failed` | `cmd_fail_agent` + `cmd_release_agent:215` |
| `*` → `archived` | `juggle_db.archive_thread` |

---

## §3 SCHEDULER

### 3.1 Tick interval

`_POLL_INTERVAL = int(os.environ.get("JUGGLE_WATCHDOG_INTERVAL", "30"))`  
(`juggle_watchdog_daemon.py:43`) — default **30 seconds**, purely periodic.

**No tick-on-demand exists today.** `graph_tick` is called only from  
`juggle_watchdog_daemon.py:270` inside the sleep loop (`time.sleep(_POLL_INTERVAL):396`).

---

### 3.2 `graph_tick` flow  (`juggle_graph_dispatch.py:186–307`)

```
graph_tick(db):
  armed = get_armed_projects(db)           # juggle_autopilot_state.py:14
  for pid in armed:
    sweep_stale_topic_claims(db, pid)      # dispatch_topics.py:41
    recompute_topic_ready(db, pid)         # db_topics.py:230
    collect ready topics
  interleave_ready(ready_by_project, in_flight, armed)  # graph_scheduler.py:20
  for (pid, topic) in interleaved:
    claim_topic(db, tid)                   # CAS SQL, dispatch_topics.py:29
    create_thread("[{tid}] {title}")       # threads table
    set_topic_thread(db, tid, thread_id)   # bind BEFORE dispatch
    hydrate_for_topic(db, pid, topic)      # juggle_graph_hydration.py
    _dispatch_via_pool(db, thread_id, prompt, topic)  # ← calls cmd_get_agent + cmd_send_task
    topic_transition(db, tid, "dispatch")  # → running
  _dispatch_flat_task_fallback(...)        # legacy: projects with tasks but 0 topics
```

`interleave_ready`: least-loaded-first round-robin across projects  
(`juggle_graph_scheduler.py:20–36`).

`_dispatch_via_pool` (`juggle_graph_dispatch.py:96–151`):  
**The tick dispatches by calling `cmd_get_agent` + `cmd_send_task` internally.**  
This is the primary coupling that must break on deletion of those commands.

---

### 3.3 Flat-task fallback  (`juggle_graph_dispatch.py:300–386`)

For projects that have `graph_tasks` but 0 `graph_topics` (pre-3-tier or migration 37  
backfilled 0 rows) — dispatches tasks directly using `claim_task` / `hydrate_for_task`.  
Added 2026-06-11 (bug J comment at line 302).

---

## §4 MANUAL DISPATCH PATH (candidates for deletion)

### 4.1 `get-agent`  (`juggle_cmd_agents_lifecycle.py:21–125`)

DB writes:  
- `db.update_agent(id, status="busy", assigned_thread=thread_uuid, busy_since=now)`  
- `db.update_thread(thread_uuid, status="background")`

Side effects: walks ranked idle agents (repo + role + harness filter, CAS assign);  
spawns new tmux pane if none available.

---

### 4.2 `send-task`  (`juggle_cmd_agents_tasks.py:20–244`)

DB writes:  
- `db.update_agent(id, last_task, last_send_task_pane_hash, last_send_task_at, ...)`  
- `db.update_thread(uuid, worktree_path, worktree_branch, main_repo_path)` (worktree auto-create)  
- `db.insert_agent_run(...)` (ledger)

Guards checked before tmux write:  
- `check_task_guard` (`juggle_cmd_agents_graph.py:81`) — refuses tick-owned state or armed-project thread without `--force-task`

---

### 4.3 `complete-agent`  (`juggle_cmd_agents_complete.py:19–120+`)

DB writes (in order):  
1. `enforce_topic_gate` — refuses if any task non-terminal (juggle_cmd_agents_graph_topics.py:25–44)  
2. `enforce_handoff_contract` — refuses task-with-dependents without --handoff  
3. `_run_integrate(thread, db)` — full git pipeline  
4. `db.set_thread_status(uuid, "closed")`  
5. `db.update_agent(id, status="idle", assigned_thread=None)`  
6. `mark_graph_topic(db, thread_uuid, integrate_ok, handoff, session_id)` — walks topic state machine  
7. `recompute_topic_ready(db, project_id)` — promotes unblocked topics

---

### 4.4 `fail-agent`  (`juggle_cmd_agents_complete.py`)

DB writes:  
- `db.set_thread_status(uuid, "failed")`  
- `fail_graph_topic` → `mark_topic_exec_failed` → `topic_transition(exec_fail)`  
- `propagate_topic_failure` — blocks derived dependents

---

### 4.5 `release-agent`  (`juggle_cmd_agents_lifecycle.py:128–233`)

DB writes:  
- `db.update_agent(id, status="idle", assigned_thread=None, context_threads=[...])`  
- If thread still `background`: `db.update_thread(uuid, status="failed")` + action item

---

### 4.6 Couplings that break on deletion

1. **`_dispatch_via_pool` calls `cmd_get_agent` + `cmd_send_task`** (`juggle_graph_dispatch.py:96–151`).  
   The tick's own dispatch path goes through these. Must be replaced with direct lower-level calls.

2. **`schedules/dogfood.py:180,192`** — calls `get-agent` + `send-task` as subprocesses.

3. **`.claude/settings.local.json`** — `complete-agent` and `release-agent` in allowed Bash list.

4. **Agent task template** (`juggle_settings.py`) — `complete-agent <THREAD>` in the coder template  
   prompt. Running agents have been primed with this command.

5. **`juggle_harness.py` / hooks** — `UserPromptSubmit` hook emits the `complete-agent` call  
   instruction in the session context block.

---

## §5 ARMING / AUTOPILOT

### 5.1 Armed state location

`settings` table, key `"autopilot_armed_project"` = CSV of project ids  
(`juggle_autopilot_state.py:11–25`).  
- Empty or NULL → disarmed.  
- `get_armed_project(db)` is a compat shim returning the first entry (`autopilot_state.py:60–63`).
- `get_armed_projects(db)` returns the full ordered list.

---

### 5.2 `--force-task` guard  (`juggle_cmd_agents_graph.py:81–121`)

Called from `cmd_send_task` (`juggle_cmd_agents_tasks.py:36–43`) before any tmux write.

Logic:
1. If `force=True` → pass.  
2. If thread bound to topic/task in `TICK_OWNED_STATES` → refuse (DA B5).  
3. Else if thread belongs to an ARMED project (even unbound thread) → refuse (R8 guard, §2.11).

`TICK_OWNED_STATES = {ready, dispatching, running, integrating, verified}` (`db_graph.py:81–83`).

---

### 5.3 Armed-project call sites

| Location | Line | Purpose |
|---|---|---|
| `juggle_graph_dispatch.py` | 200, 222 | tick main loop: skip disarmed projects |
| `juggle_cmd_agents_graph.py` | 113 | R8 guard in check_task_guard |
| `juggle_watchdog_daemon.py` | 270 | drives graph_tick |
| `juggle_cockpit_modals.py` | 748 | UI arm/disarm |
| `juggle_cockpit_graph_panel.py` | 221, 305 | "no armed graph" message |
| `juggle_cockpit_graph_dag.py` | 30–44 | DAG loading gated on armed set |

---

## §6 INTEGRATE + INVARIANTS

### 6.1 `_run_integrate` pipeline  (`juggle_cmd_integrate.py:105–454`)

Steps:
1. Source-binding guard (`juggle_repo_binding._assert_source_binding`) — refuse mis-bound repos  
2. `acquire_repo_lock` (per-repo mutex, 30–600s timeout)  
3. Abort in-progress rebase  
4. `git fetch --prune`  
5. Determine rebase target (`origin/main` → `origin/master` → local)  
6. Idempotency: if branch already merged → `_record_merged_sha` + cleanup  
7. `git rebase <target>`  
8. Run `test_cmd` (scoped or full) — single retry for flakes  
9. `verify_task_premerge` (`juggle_integrate_verify`) — runs `verify_cmd` pre-merge  
10. Validate `local_main == expected_main` (wrong-branch guard)  
11. `git merge --ff-only` / push  
12. **`_record_merged_sha`** → `db_topics.set_topic_merged_sha` — required for G1 gate  
13. Remove worktree + branch, clear thread fields

---

### 6.2 G1 verified gate

`topic_is_merged(db, topic_id)` (`dbops/graph_guards.py`, called from `dbops/db_topics.py:30`):  
requires `merged_sha` is set and is an ancestor of canonical main.  
`verified` state is blocked until this passes.

---

### 6.3 Orphan guard  (`dbops/orphan_guard.py`)

**`find_unmerged_completed_topics`** (line 34): topics where all tasks are `verified` but  
`topic_is_merged` is False. Catches completed-but-unmerged stranded topics.

**`reconcile_out_of_band_merges`** (line 81): stamps `merged_sha` for topics whose branch  
is already on main via an out-of-band merge. Prevents false-positive re-flags.

**`flag_unmerged_completed_topics`** (line 119): called each watchdog tick;  
deduped via `watchdog_events` (one flag per 24h per topic).

**What becomes structurally unnecessary with one completion path:**  
- `reconcile_out_of_band_merges` — only needed because manual-close-without-integrate strands topics.  
  If integrate always runs inside complete-agent (already the case) and complete-agent is the only  
  close path, orphans shouldn't form. Guard can be simplified to pure verification.  
- `release-agent → failed` path — entire concept of "released without completing" disappears  
  since the tick is the sole executor.

---

## §7 CREATE VERBS

### 7.1 `create-thread`  (`juggle_cmd_threads.py`)

Required: title (positional)  
Optional: `--project`  
DB write: `INSERT INTO threads (id, topic, status='active', ...)`  
Assigns slug via wheel (`schema.py:270–279`).

---

### 7.2 `graph add-task`  (`juggle_graph_add.py:121–225`)

Required: `project_id`, `task_id`, `title`, `prompt`  
Optional: `--deps`, `--required-by`, `--verify-cmd`, `--topic`, `--auto-create-topic`

Validation (`validate_add_task:43–118`):
- project must have existing tasks (else live dict is empty, deps check passes vacuously)  
- every `--deps` id must exist in live graph (any state)  
- every `--required-by` target must be in `MUTABLE_STATES`  
- re-adding existing `task_id` only if that task is mutable  
- cycle check (Kahn) over resulting edge set

Topic auto-create (`auto_create_topic=True`): creates topic in the **same transaction** as the task  
insert to close the empty-topic TOCTOU window (2026-06-13 incident, `juggle_graph_add.py:168–174`).

The "unknown topic refusal": if `--topic <id>` given but topic doesn't exist and  
`auto_create_topic=False`, the FK insert fails at the DB layer (no explicit guard in Python).

---

### 7.3 What a unified `add-node` must accept

Minimum surface for the spec author:
- `kind` ∈ {task, research, conversation, decision}
- `title`, `objective/prompt`
- `--project` (optional tag; defaults to INBOX)
- `--deps`, `--required-by` (optional)  
- `--verify-cmd` (optional, kind=task only)

The owning-project + topic wrapping is an internal implementation detail in the target model.

---

## §8 COCKPIT COUPLING

### 8.1 `juggle_cockpit_model.snapshot`  (`juggle_cockpit_model.py:160–418`)

Reads from **threads** table:
- `active`, `running`, `background`, `closed` (within TTL), `archived` (last N) — lines 264–295

Reads from **graph_tasks** table:
- `SELECT project_id, state, thread_id FROM graph_tasks` (line 219)  
- Builds `task_state_by_thread` dict (line 220–225)  
- Builds `graph_by_project` aggregate counts (line 227–230)

Attaches to `Topic.task_state` (line 28): the graph task's state for a thread-bound topic.

Graph DAG:
- `_load_graph_dags(conn)` (`juggle_cockpit_graph_dag.py`) — reads `graph_topics` as DAG nodes  
  when `load_graph_dag=True` (graph mode only)

---

### 8.2 `juggle_cockpit_graph_dag.py` (`juggle_cockpit_graph_dag.py:1–80+`)

`GraphDag.tasks` = list of `GraphTask` objects built from **`graph_topics`** rows (topic tier).  
Falls back to `_load_one_legacy_tasks` which reads **`graph_tasks`** for pre-topic projects.

`member_tasks` dict (topic_id → list of task dicts) populated from `graph_tasks` for the  
task-detail modal.

---

### 8.3 `[T-<id>]` prefix origin

Created in `graph_tick` (`juggle_graph_dispatch.py:229`):
```python
thread_id = db.create_thread(
    f"[{tid}] {topic['title']}"[:80],
    ...
)
```
NOT a separate DB column. The `[T-<id>]` appears in `threads.topic` (the human title field).  
`test_cockpit_graph_thread_label.py` exercises this.

---

### 8.4 Cockpit changes after unification

- `topic.task_state` JOIN becomes direct node.state read — drop the `task_state_by_thread` pass  
- `graph_by_project` can read from unified `nodes` table  
- `_load_graph_dags` can read from `nodes` filtered by project and kind  
- Thread-list queries (`status=active/background/...`) become `nodes WHERE kind=conversation` or equivalent

---

## §9 TEST SURFACE

### 9.1 Graph/autopilot path (must migrate or delete)

| Test File | Coverage |
|---|---|
| `test_cmd_graph.py` | graph add-task, spec load, state machine, topic creation |
| `test_autopilot_guards.py` | --force-task, TICK_OWNED_STATES, R8 armed-project guard |
| `test_autopilot_state.py` | arm/disarm, CSV parsing, get_armed_projects |
| `test_cmd_autopilot.py` | CLI arm/disarm/status |
| `test_verified_merged_sha.py` | merged_sha/G1 verified gate |
| `test_cockpit_graph.py`, `test_cockpit_graph_*.py` (6 files) | DAG panel, layout, thread label |
| `tests/watchdog/` | watchdog daemon, graph_tick, orphan guard |

### 9.2 Manual dispatch path (must migrate or delete)

| Test File | Coverage |
|---|---|
| `test_cli_agents.py` | get-agent, send-task, complete-agent, fail-agent, release-agent |
| `test_completion_commands.py` | complete-agent flow, topic gate, handoff contract |
| `test_tmux_send_task.py` | send-task check_task_guard |
| `test_tmux_lifecycle.py` | agent lifecycle, CAS assign |

### 9.3 Mixed / shared

| Test File | Notes |
|---|---|
| `test_cockpit_model.py`, `test_cockpit_model_snapshot.py` | reads both tables |
| `test_cockpit_graph_thread_label.py` | [T-] prefix from graph_tick |
| `test_projects_db.py` | projects as tags |

---

## §10 RISK NOTES

### R1 ⚠️ `_dispatch_via_pool` calls the to-be-deleted CLIs  
`juggle_graph_dispatch.py:96–151` calls `cmd_get_agent` then `cmd_send_task` internally  
(via `Namespace` fake args). Deleting get-agent/send-task as commands doesn't delete  
these Python functions — but the refactor spec must decide whether to:  
(a) keep the functions but mark them internal-only (no CLI registration), or  
(b) replace `_dispatch_via_pool` with direct lower-level spawn+push primitives.

### R2 ⚠️ Running agents have `complete-agent` baked into their prompts  
The coder task template contains `complete-agent <THREAD> "<summary>" --retain "<key finding>"`.  
Any agent dispatched before the transition will call the old CLI. The command must stay  
registered (even as a compatibility shim) until all in-flight agents complete.

### R3 ⚠️ `graph_tasks.topic_id` is a migration-added nullable column  
Pre-migration rows have `topic_id IS NULL`. In the current code, `list_topics(db, pid)` excludes  
`is_mirror=1` rows but does NOT require non-NULL `topic_id` on tasks. The tick's flat-task  
fallback (`_dispatch_flat_task_fallback`) exists specifically to handle tasks without a topic.  
After unification, the spec must decide how to handle these legacy rows.

### R4 ⚠️ `is_mirror` convention must be preserved or migrated  
Mirror topics (`graph_topics.is_mirror=1`) represent conversational threads assigned to a project  
but NOT managed by the graph machinery. They are excluded from `list_topics`, `claim_topic`,  
`recompute_topic_ready`, and `reconcile`. In the unified model, the concept maps to  
`kind=conversation` nodes that enter the node table without going through `add-node`.

### R5 ⚠️ No tick-on-demand today — new infrastructure needed  
The target spec says "watchdog tick as the SOLE executor (tick-on-demand)". Currently there  
is no API to trigger `graph_tick` outside the 30s daemon loop. A `juggle tick` command or  
a Unix socket/signal mechanism must be added.

### R6 Data migration for existing `threads` rows  
Existing threads with `status='background'` that are bound to a `graph_topics` row are  
mid-flight topics. These must either be preserved as-is (old format) or converted to  
unified `nodes` rows. The `merged_sha` on graph_topics must be carried forward — it is  
the sole verified gate.

### R7 External subprocess callers of get-agent/send-task  
`schedules/dogfood.py:180,192` calls `get-agent` and `send-task` as subprocesses.  
`.claude/settings.local.json` has these in the Bash allowlist.  
Both must be updated before the CLI registrations are removed.

### R8 `orphan_guard` becomes simpler but not unnecessary  
`find_unmerged_completed_topics` (`orphan_guard.py:34`) joins `graph_topics + graph_tasks`.  
After unification this query becomes: `nodes WHERE all child tasks verified AND merged_sha IS NULL`.  
`reconcile_out_of_band_merges` remains useful (manual git merges bypass juggle integrate).

### R9 projects as optional tags — INBOX sentinel  
`INBOX_PROJECT_ID = "INBOX"` (`schema.py:41`) is hardcoded as the default project.  
Threads without a `project_id` display under Inbox. In the unified model, if projects are  
optional tags, the INBOX sentinel must be handled consistently in `list_nodes`, cockpit  
grouping, and `graph_tick` (which iterates `get_armed_projects`).

### R10 `cockpit_model.Topic.task_state` dual-read  
`juggle_cockpit_model.py:219–225` does a separate `SELECT * FROM graph_tasks` pass to  
populate `task_state_by_thread`. After unification this is the node's own `state` field —  
the extra join disappears. Any test that monkeypatches the task-state join must be rewritten.

---

## AGENT-FIRST: Programmatic verification hooks

| Behavior | Verifiable today via CLI | What needs human eyeballing |
|---|---|---|
| State machine transitions | `juggle graph status --json` + assert state field | Visual cockpit rendering |
| Armed-project set | `juggle autopilot status --json` → `armed_projects` array | Nothing |
| Topic state after tick | `juggle graph status --json` → per-topic `state` | Dispatch timing |
| Merge/verified gate | `juggle graph status --json` → `merged_sha`, state=verified | None |
| Orphan detection | `juggle watchdog inspect --json` or action_items table | None |
| Thread-topic binding | Direct SQL: `SELECT thread_id, state FROM graph_topics` | None |
| Tick interval | `echo $JUGGLE_WATCHDOG_INTERVAL` | None |
| Tick-on-demand (new) | No CLI today — must be added | All of dispatch timing |
| `_dispatch_via_pool` success | `juggle graph status` 30s later → state='running' | Race conditions |
