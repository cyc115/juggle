# Spec: Unified Topic Graph (topic ≡ graph node)

_2026-06-18 · status: LOCKED design_

---

## 1. Goals

| Goal | Why |
|---|---|
| Collapse threads / graph_topics / graph_tasks into ONE `nodes` table | Eliminates the dual-join read path, the "unknown topic refusal" friction (§7.2 src-facts), and the 3-tier mental model overhead |
| Single kind-gated state machine | One transition function, one `state` column — no per-table status enum divergence |
| Tick is the sole executor for task/research nodes | Ends manual-dispatch escape hatch; removes R8 armed-project guard and --force-task |
| Tick-on-demand via SIGUSR1 | Sub-second response on node→ready instead of 0–30s periodic wait |
| `add-node` unified verb | Zero-friction node creation; no topic pre-creation ceremony |
| Projects become optional tags | Default INBOX graph; no mandatory parent |
| Arming deleted | All ready nodes execute unconditionally |
| Strangler-fig migration | Tests green and complete-agent functional at every phase boundary |

## 2. Non-Goals / YAGNI Cuts

- No change to the agent pool model (agents, tmux panes, `busy_since`, CAS assign).
- No change to the `projects` table schema; projects stay as lightweight records.
- No new DAG visualization format; cockpit DAG reads from `nodes` instead of `graph_topics`.
- No multi-worktree per node (one node → one branch remains).
- No cross-DB or multi-repo support.
- No streaming state (WebSocket/SSE) in this spec.
- No "pause/resume" node semantics beyond existing `reload→open`.

---

## 3. Unified Data Model

### 3.1 `nodes` table (replaces threads + graph_topics + graph_tasks)

```sql
CREATE TABLE IF NOT EXISTS nodes (
  -- Identity
  id              TEXT PRIMARY KEY,          -- UUID; conversation nodes reuse prior thread.id
  kind            TEXT NOT NULL,             -- 'task' | 'research' | 'conversation' | 'decision'

  -- Content
  title           TEXT NOT NULL,
  objective       TEXT NOT NULL DEFAULT '',  -- prompt/objective (was graph_tasks.prompt or graph_topics.objective)

  -- State machine
  state           TEXT NOT NULL DEFAULT 'open',

  -- Structural
  project_id      TEXT REFERENCES projects(id),  -- optional tag; NULL treated as INBOX
  parent_id       TEXT REFERENCES nodes(id),     -- sub-task parent (was graph_tasks.topic_id)

  -- Execution (kind='task' only; NULL for conversation/decision)
  verify_cmd      TEXT,
  worktree_path   TEXT,
  worktree_branch TEXT,
  main_repo_path  TEXT,

  -- Completion artifacts (task/research)
  handoff         TEXT,
  diffstat        TEXT,
  verified_at     TEXT,
  merged_sha      TEXT,                      -- G1 gate anchor; required for verified→done on task

  -- Agent tracking
  agent_task_id           TEXT,
  agent_result            TEXT,
  last_dispatched_task    TEXT,
  last_dispatched_role    TEXT,
  last_dispatched_model   TEXT,

  -- Conversation metadata (kind='conversation' only; NULL for others)
  session_id              TEXT,
  summary                 TEXT DEFAULT '',
  key_decisions           TEXT DEFAULT '[]',
  open_questions          TEXT DEFAULT '[]',
  last_user_intent        TEXT DEFAULT '',
  summarized_msg_count    INTEGER NOT NULL DEFAULT 0,
  show_in_list            INTEGER NOT NULL DEFAULT 1,

  -- Timestamps
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes(project_id);
CREATE INDEX IF NOT EXISTS idx_nodes_state   ON nodes(state);
CREATE INDEX IF NOT EXISTS idx_nodes_kind    ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_parent  ON nodes(parent_id);
```

**Column provenance:**

| Old source | Old column | → nodes column |
|---|---|---|
| threads | id | id (conversation nodes only) |
| threads | topic | title |
| threads | status | state (see §4 mapping) |
| threads | session_id, summary, key_decisions, open_questions, last_user_intent, summarized_msg_count, show_in_list | same (NULL for task/research/decision) |
| threads | worktree_path/branch/main_repo_path | same |
| threads | last_dispatched_task/role/model | same |
| threads | agent_task_id, agent_result | same |
| graph_topics | id | id (for topic-tier nodes) |
| graph_topics | title, objective | title, objective |
| graph_topics | project_id | project_id |
| graph_topics | state | state |
| graph_topics | merged_sha | merged_sha |
| graph_topics | handoff, diffstat, verified_at | same |
| graph_topics | thread_id | **eliminated** (node IS the thread) |
| graph_topics | is_mirror=1 | → kind='conversation' |
| graph_tasks | id | id (sub-task nodes) |
| graph_tasks | prompt | objective |
| graph_tasks | topic_id | parent_id |
| graph_tasks | project_id | project_id |
| graph_tasks | verify_cmd | verify_cmd |
| graph_tasks | state | state |
| graph_tasks | handoff, diffstat, verified_at | same |

**Eliminated columns:** `threads.last_active` (→ `nodes.updated_at`), `graph_topics.thread_id`, `graph_tasks.thread_id`, `graph_tasks.topic_id` (replaced by `parent_id`), `graph_topics.is_mirror`.

### 3.2 `node_edges` table (replaces graph_edges)

```sql
CREATE TABLE IF NOT EXISTS node_edges (
  node_id         TEXT NOT NULL REFERENCES nodes(id),
  depends_on_id   TEXT NOT NULL REFERENCES nodes(id),
  PRIMARY KEY (node_id, depends_on_id)
);
```

`graph_edges.(task_id, depends_on_id)` map 1:1 to `node_edges.(node_id, depends_on_id)` — both referenced graph_tasks.id, which become nodes.id after migration.

### 3.3 ERD (text)

```
projects (optional tag)
  └─ nodes  [kind, state, parent_id, project_id]
       ├─ task node (parent_id=NULL) ← top-level unit of work; 1 branch/worktree
       │    └─ task sub-node (parent_id→task node) ← was graph_tasks row
       │         └─ node_edges (DAG between sub-nodes; edges may cross parent boundaries)
       ├─ research node (parent_id=NULL)
       ├─ conversation node (parent_id=NULL; session-bound)
       └─ decision node (parent_id=NULL)

messages   → references nodes.id (was threads.id; column rename only)
notifications → references nodes.id
action_items  → references nodes.id (was threads.id)
```

The `messages` / `notifications` / `action_items` tables have FK columns named `thread_id`. These are renamed `node_id` in a schema migration; the FK target is `nodes(id)`.

---

## 4. State Machine

### 4.1 States

```
open            — entry state (replaces 'pending' on task/research; replaces 'active' on conversation/decision)
ready           — deps satisfied, queued for tick dispatch (task/research only)
dispatching     — CAS-claimed by tick (task/research only; internal, sub-second)
running         — agent working (task/research only)
integrating     — integrate pipeline in progress (task only)
verified        — merged + G1 gate passed (task only; pre-done confirmation)
done            — terminal success (all kinds)
failed-exec     — agent crashed or fail-agent called (task/research)
failed-integration  — git integrate failed (task)
failed-verify   — verify_cmd failed pre-merge (task)
blocked-failed  — upstream dep failed (task/research)
archived        — soft-deleted terminal (all kinds)
```

### 4.2 Full Transition Table

| From | Event | To | Guard |
|---|---|---|---|
| open | deps_ready | ready | kind ∈ {task, research}; all deps done/verified |
| open | answer | done | kind ∈ {conversation, decision}; inline only |
| open | dep_fail | blocked-failed | kind ∈ {task, research} |
| open | reload | open | kind ∈ {task, research}; clears agent binding |
| open | archive | archived | any kind |
| ready | claim | dispatching | tick CAS; kind ∈ {task, research} |
| ready | dep_fail | blocked-failed | kind ∈ {task, research} |
| ready | reload | open | kind ∈ {task, research} |
| ready | unready | open | kind ∈ {task, research}; --required-by add reorders |
| dispatching | dispatch | running | tick; kind ∈ {task, research} |
| dispatching | stale_reset | ready | kind ∈ {task, research}; >5min since claim |
| running | integrate_start | integrating | kind=task only |
| running | complete | done | kind=research only; no merge |
| running | exec_fail | failed-exec | kind ∈ {task, research} |
| integrating | integrate_ok | verified | kind=task; merged_sha set |
| integrating | integrate_fail | failed-integration | kind=task |
| integrating | verify_fail | failed-verify | kind=task |
| verified | g1_pass | done | kind=task; merged_sha ancestor-of-main check passes |
| failed-* / blocked-failed | reload | open | kind ∈ {task, research} |
| any non-archived | archive | archived | any kind |

### 4.3 threads.status → node.state Mapping (for migration)

| threads.status | node.state | Notes |
|---|---|---|
| active | open | Conversation actively open in session |
| background | running | Agent dispatched and working |
| running | running | Same semantic |
| closed | done | complete-agent has been called |
| failed | failed-exec | fail-agent or release-agent set this |
| done | done | Synonym for closed |
| archived | archived | Archive terminal |

### 4.4 Legal Transitions Per Kind

| Transition | task | research | conversation | decision |
|---|---|---|---|---|
| open→ready | ✓ | ✓ | ✗ | ✗ |
| open→done (answer) | ✗ | ✗ | ✓ | ✓ |
| open→blocked-failed | ✓ | ✓ | ✗ | ✗ |
| ready→dispatching | ✓ | ✓ | ✗ | ✗ |
| dispatching→running | ✓ | ✓ | ✗ | ✗ |
| running→integrating | ✓ | ✗ | ✗ | ✗ |
| running→done (complete) | ✗ | ✓ | ✗ | ✗ |
| running→failed-exec | ✓ | ✓ | ✗ | ✗ |
| integrating→verified | ✓ | ✗ | ✗ | ✗ |
| integrating→failed-* | ✓ | ✗ | ✗ | ✗ |
| verified→done | ✓ | ✗ | ✗ | ✗ |
| failed-*→open (reload) | ✓ | ✓ | ✗ | ✗ |
| *→archived | ✓ | ✓ | ✓ | ✓ |

### 4.5 Implementation: single `node_transition(db, node_id, event)` function

```python
def node_transition(db, node_id: str, event: str) -> str:
    """CAS transition. Returns new state. Raises InvalidTransition on bad event."""
    node = db.get_node(node_id)
    kind  = node["kind"]
    state = node["state"]
    key = (state, event)
    if key not in _TRANSITIONS:
        raise InvalidTransition(f"{state} --{event}--> ? undefined")
    new_state = _TRANSITIONS[key]
    _assert_kind_allows(kind, state, event, new_state)   # raises if illegal
    if new_state == "verified":
        _assert_g1_merged(db, node_id)                   # merged_sha + ancestor check
    db.update_node(node_id, state=new_state, updated_at=utcnow())
    return new_state
```

`_assert_kind_allows` reads a static `_KIND_LEGAL` dict (kind → frozenset of legal events).
The function is the SOLE writer of `node.state` (except the `ready→dispatching` CAS SQL below).

`ready→dispatching` remains a raw SQL CAS to avoid TOCTOU:
```sql
UPDATE nodes SET state='dispatching', updated_at=?
WHERE id=? AND state='ready'
```

---

## 5. Executor Model

### 5.1 dispatch_node() — internal function

`_dispatch_via_pool` (`juggle_graph_dispatch.py:96–151`) currently calls `cmd_get_agent` then `cmd_send_task` as `Namespace`-faked CLI commands. This coupling breaks if those commands are removed from the surface.

**Refactor:** extract the lower-level logic that `cmd_get_agent` and `cmd_send_task` implement into a shared internal `dispatch_node(db, node_id, prompt, role, model)` function in a new module `juggle_dispatch_internal.py`. The tick calls `dispatch_node()` directly. `cmd_get_agent` and `cmd_send_task` are **removed from CLI registration** (no `@cli.command`) but their helper functions survive internally and are called by `dispatch_node`.

`dispatch_node(db, node_id, prompt, role, model)`:
1. `claim_node(db, node_id)` — CAS `ready→dispatching`
2. `_assign_agent(db, node_id, role)` — pool walk (was `cmd_get_agent` logic)
3. `_create_worktree_if_needed(db, node_id)` — (was `cmd_send_task` worktree logic)
4. `_send_to_tmux(db, node_id, prompt)` — (was `cmd_send_task` tmux write)
5. `node_transition(db, node_id, "dispatch")` → running

`dogfood.py` (§4.6 src-facts) calls `get-agent` and `send-task` as subprocesses. These calls are replaced with direct `juggle dispatch-node <node_id>` CLI calls (a thin public wrapper around `dispatch_node()`).

### 5.2 tick-on-demand: SIGUSR1 + 30s backstop

**Current state:** purely periodic 30s loop in `juggle_watchdog_daemon.py`; no signal handler beyond SIGTERM/SIGINT.

**Target:**

```python
# juggle_watchdog_daemon.py additions
import threading

_tick_requested = threading.Event()

def _handle_sigusr1(signum, frame):
    _tick_requested.set()   # idempotent; coalesces multiple signals

signal.signal(signal.SIGUSR1, _handle_sigusr1)

# main loop
while not _shutdown.is_set():
    _tick_requested.wait(timeout=_POLL_INTERVAL)  # wakes on SIGUSR1 or 30s
    _tick_requested.clear()
    if _shutdown.is_set():
        break
    graph_tick(db)
```

**Signal sender:** any code that transitions a node INTO `ready` calls `_signal_watchdog()`:

```python
# juggle_watchdog_signal.py  (new, <50 lines)
def signal_watchdog_tick():
    """Send SIGUSR1 to the watchdog PID from the singleton PID file."""
    pid = _read_watchdog_pid()   # reads ~/.juggle/watchdog.pid (SINGLETON_PID_FILE)
    if pid:
        try:
            os.kill(pid, signal.SIGUSR1)
        except (ProcessLookupError, PermissionError):
            pass  # watchdog not running; 30s backstop will handle it
```

Call sites for `signal_watchdog_tick()`:
- `recompute_node_ready()` — after promoting node(s) to `ready`
- `complete-agent shim` — after marking a node `done`, deps may become `ready`
- `add-node` — new node with no deps enters `ready` immediately; signal

**Re-entrancy:** `threading.Event` coalesces multiple concurrent SIGUSR1 signals into one tick. If a tick is already running when the signal arrives, `_tick_requested.set()` queues exactly one more tick after the current one completes (the `_tick_requested.clear()` happens before `graph_tick(db)` so a signal during tick sets it again).

**Conversation/decision inertness:** `graph_tick` iterates only nodes where `kind IN ('task', 'research')`. It never touches conversation or decision nodes. Those are answered inline by the live session via `node_transition(db, node_id, "answer")`.

### 5.3 TICK_OWNED_STATES (unchanged set, new name)

```python
TICK_OWNED_STATES = frozenset({"ready", "dispatching", "running", "integrating", "verified"})
```

No external code may transition a node in a TICK_OWNED_STATE except `node_transition()` and the CAS SQL. The `check_task_guard` is **deleted** (it was the --force-task guard; arming is removed).

---

## 6. add-node: Unified Creation Verb

### 6.1 CLI surface

```
juggle add-node <title>
  --kind         task|research|conversation|decision  (default: task)
  --objective    "..." or read from stdin
  --project      <project_id>  (optional; default: INBOX)
  --deps         <node_id> [<node_id> ...]
  --required-by  <node_id> [<node_id> ...]
  --verify-cmd   "..."  (task only; error if given for other kinds)
  --parent       <node_id>  (optional; makes this a sub-task)
  --json         emit {"node_id": "..."}
```

### 6.2 Behavior

1. Validate: `--verify-cmd` only on `kind=task`; `--deps`/`--required-by` only on `task`/`research`; cycle check (Kahn) over resulting edge set.
2. Insert node with `state='open'`.
3. If no `--deps` → emit `deps_ready` immediately → `state='ready'`.
4. Insert `node_edges` for `--deps` and `--required-by`.
5. Call `signal_watchdog_tick()` if node entered `ready`.
6. Return `node_id`.

### 6.3 Shims (thin wrappers, not deleted)

- `create-thread <title> [--project]` → `add-node <title> --kind conversation [--project]`
- `graph add-task <pid> <tid> <title> --prompt "..."` → `add-node <title> --kind task --project <pid> --objective "..." [--deps ...]`

These shims emit a deprecation warning but remain registered for at least P5+P6 to avoid breaking external callers.

---

## 7. Projects as Optional Tags

- `project_id` on `nodes` is nullable; NULL is treated as INBOX (`INBOX_PROJECT_ID = "INBOX"`).
- The `projects` table is unchanged. Project records still hold name, objective, etc.
- `get_armed_projects()` is **deleted** (arming is gone). The tick iterates all ready task/research nodes regardless of project.
- Cockpit grouping by project remains: `SELECT DISTINCT project_id FROM nodes WHERE state NOT IN ('done','archived')` produces the list; NULL → INBOX bucket.
- No `--project` required on `add-node`; zero-friction path: `juggle add-node "Fix the login bug"` lands in INBOX, tick picks it up.

---

## 8. Migration Design

### 8.1 Principles

- Forward-only. No dual-read compatibility layer. Old tables remain present-but-unused until P8 cleanup.
- Runs via `juggle doctor` migration runner only (never by worktree agents against prod DB).
- Before running: `git tag pre-topic-graph-merge HEAD` as rollback anchor.
- After migration: `juggle graph status --json` must return all former topics/tasks with equivalent states.

### 8.2 Row Mapping Rules

**threads rows → nodes:**

| threads.status | kind | state | Notes |
|---|---|---|---|
| active | conversation | open | Live session thread |
| background | conversation | running | Has assigned agent — preserve agent binding |
| running | conversation | running | Same |
| closed | conversation | done | Already answered |
| failed | conversation | failed-exec | Release/fail-agent was called |
| done | conversation | done | |
| archived | conversation | archived | |

**graph_topics rows → nodes:**

| graph_topics condition | kind | state | Notes |
|---|---|---|---|
| is_mirror=1 | conversation | state mapped via §4.3 | Mirror = conversational thread in a project |
| is_mirror=0 | task | state as-is (open replaces pending) | Topic-tier task node |

State renaming for task nodes: `pending → open`.

**graph_tasks rows → nodes:**

| condition | kind | parent_id | state |
|---|---|---|---|
| topic_id IS NOT NULL | task | topic_id (now nodes.id) | state as-is (pending→open) |
| topic_id IS NULL ("flat task", pre-3-tier) | task | NULL | state as-is (pending→open) |

**graph_edges → node_edges:** direct 1:1 remap of (task_id, depends_on_id) to (node_id, depends_on_id) — both reference what are now `nodes.id` values.

### 8.3 Field Backfill

| nodes column | Source for conversation | Source for task (topic-tier) | Source for task (task-tier) |
|---|---|---|---|
| id | threads.id | graph_topics.id | graph_tasks.id |
| title | threads.topic | graph_topics.title | graph_tasks.title |
| objective | threads.last_user_intent (or '') | graph_topics.objective | graph_tasks.prompt |
| project_id | NULL (→INBOX) or via graph_topics.project_id if mirror | graph_topics.project_id | graph_tasks.project_id |
| parent_id | NULL | NULL | graph_topics.id (→ topic-tier nodes.id) |
| state | §4.3 mapping | pending→open, others as-is | pending→open, others as-is |
| verify_cmd | NULL | NULL | graph_tasks.verify_cmd |
| merged_sha | NULL | graph_topics.merged_sha | NULL |
| worktree_path/branch/main_repo_path | threads.* | NULL (topic has no worktree directly; agent's thread had it) | NULL |
| session_id, summary, key_decisions, open_questions, last_user_intent, summarized_msg_count | threads.* | NULL | NULL |
| show_in_list | threads.show_in_list | 1 | 1 |
| created_at | threads.created_at | graph_topics.created_at | graph_tasks.created_at |
| updated_at | threads.last_active | graph_topics.updated_at | graph_tasks.updated_at |

**In-flight task nodes** (state=running, background thread exists): the topic-tier node gets `worktree_path/branch/main_repo_path` copied from its bound thread row. The conversation node for that thread is also migrated; the ticket binds them via `parent_id` or the existing `thread_id` (which is dropped post-migration).

### 8.4 Migration SQL sketch (doctor step)

```sql
-- Step 1: create nodes + node_edges tables (additive, old tables untouched)
-- Step 2: INSERT INTO nodes SELECT ... FROM threads  (conversation kind)
-- Step 3: INSERT INTO nodes SELECT ... FROM graph_topics WHERE is_mirror=0  (task kind, topic-tier)
-- Step 4: INSERT INTO nodes SELECT ... FROM graph_topics WHERE is_mirror=1  (conversation kind)
-- Step 5: INSERT INTO nodes SELECT ... FROM graph_tasks  (task kind, task-tier; parent_id=topic_id remapped)
-- Step 6: INSERT INTO node_edges SELECT task_id, depends_on_id FROM graph_edges
-- Step 7: UPDATE messages SET node_id=thread_id  (column alias or rename)
-- Step 8: Validate: COUNT(*) match per kind; no NULL title; all parent_ids resolvable
```

Steps run in a single SQLite transaction with ROLLBACK on any validation failure.

### 8.5 Worktree State for In-Flight Nodes

Topics with `state='running'` have a bound thread (`thread_id`) that holds the worktree fields. The migration copies `threads.(worktree_path, worktree_branch, main_repo_path)` into the topic-tier task node. The thread row becomes a companion conversation node (`state='running'`). After P3 lands, `dispatch_node` keeps both in sync.

---

## 9. complete-agent Shim Contract

### 9.1 Why it must survive

Three external binding points bake in `complete-agent`:
1. **Agent task template** (`juggle_settings.py`) — the coder prompt ends with `complete-agent <THREAD>`.
2. **`UserPromptSubmit` hook** (`juggle_harness.py`) — emits a session context block referencing `complete-agent`.
3. **`settings.local.json`** — `complete-agent` is in the Bash allowlist.
4. **`dogfood.py:180,192`** — calls `get-agent` + `send-task` as subprocesses (see §5.1).

Running agents before P5 ships have already been primed with `complete-agent`. The CLI registration must not be removed until all in-flight agents at migration time have completed.

### 9.2 Shim behavior

`complete-agent <NODE_ID> "<summary>" [--retain "<finding>"] [--open-questions '<json>']`

```
1. Lookup node by NODE_ID (accepts both old thread UUID and new node UUID)
2. Verify node.state == 'running'  (else: error "node not in running state")
3. Run integrate pipeline (_run_integrate) — same as today
4. node_transition(db, node_id, "integrate_start") → integrating
5. node_transition(db, node_id, "integrate_ok")    → verified
6. node_transition(db, node_id, "g1_pass")         → done   (if merged_sha verified)
7. Release agent pool entry (agent → idle)
8. signal_watchdog_tick()  (may unblock child/sibling deps)
```

For `kind=research` nodes: step 3 is skipped; step 4 fires `complete` event → `done`.
For `kind=conversation` nodes: step 4 fires `answer` event → `done`; no integrate.

**fail-agent shim:**
```
fail-agent <NODE_ID> ["<reason>"]
→ node_transition(db, node_id, "exec_fail") → failed-exec
→ propagate_failure to dependents (blocked-failed)
→ agent → idle
```

**release-agent:** removed (no manual release path; tick is sole executor). If an agent disappears, the watchdog's stale-claim sweep (`dispatching→ready` after 5min) recovers the slot.

### 9.3 External caller compatibility

| Caller | Current call | Post-P5 |
|---|---|---|
| Agent task template | `complete-agent <THREAD> "..."` | unchanged; shim maps THREAD→node_id |
| `juggle_harness.py` UserPromptSubmit hook | emits `complete-agent <THREAD>` | unchanged |
| `settings.local.json` allowlist | `complete-agent` | unchanged |
| `dogfood.py` | `get-agent` subprocess → `send-task` subprocess | replaced with `juggle dispatch-node <node_id>` |

`complete-agent` and `fail-agent` remain registered CLI commands permanently. `get-agent` and `send-task` are de-registered from CLI after dogfood.py is updated (P3).

---

## 10. Deletion List

| What | Where | Replaced by |
|---|---|---|
| Arming state (`autopilot_armed_project` settings key) | `juggle_autopilot_state.py`, `settings` table | Nothing; tick runs unconditionally |
| `get_armed_projects()` / `get_armed_project()` | `juggle_autopilot_state.py` | Deleted |
| `--force-task` flag | `juggle_cmd_agents_tasks.py` | Deleted |
| `check_task_guard` (R8 guard) | `juggle_cmd_agents_graph.py` | Deleted |
| `juggle autopilot arm/disarm` CLI | `juggle_cmd_autopilot.py` | Deleted |
| `get-agent` CLI registration | `juggle_cmd_agents_lifecycle.py` | Internal `dispatch_node()` |
| `send-task` CLI registration | `juggle_cmd_agents_tasks.py` | Internal `dispatch_node()` |
| `release-agent` CLI command | `juggle_cmd_agents_lifecycle.py` | Stale-claim sweep (watchdog) |
| `set_topic_thread()` / `thread_id` in graph_topics | `dbops/db_topics.py` | Node IS the thread |
| `_dispatch_flat_task_fallback` | `juggle_graph_dispatch.py:300–386` | All nodes are first-class; no flat-task fallback needed |
| `reconcile_out_of_band_merges` | `dbops/orphan_guard.py:81` | Simplify to pure verification (see §11) |
| `task_state_by_thread` JOIN in cockpit model | `juggle_cockpit_model.py:219–225` | Direct `node.state` read |
| `[T-<id>]` thread title prefix hack | `juggle_graph_dispatch.py:229` | Node ID is the identity; no title mangling |
| `_dispatch_via_pool` (as caller of cmd_get/send) | `juggle_graph_dispatch.py:96–151` | `dispatch_node()` |
| `armed` gating in `juggle_cockpit_graph_panel.py` | lines 221, 305 | Show all nodes |
| `armed` gating in `juggle_cockpit_graph_dag.py` | lines 30–44 | Load DAG unconditionally |
| `CREATE_GRAPH_TOPICS`, `CREATE_GRAPH_TASKS`, `CREATE_GRAPH_EDGES` | `dbops/schema_graph.py` | `CREATE_NODES`, `CREATE_NODE_EDGES` (P8 cleanup) |
| `threads` table (DDL) | `dbops/schema.py` | `nodes` (P8 cleanup) |

### 10.1 Simplified orphan_guard

After unification, `reconcile_out_of_band_merges` becomes simpler:

```python
# New shape (all reads from nodes table):
def find_orphan_nodes(db):
    """Nodes where all child tasks are done/verified but merged_sha is NULL."""
    ...  # query nodes WHERE kind='task' AND parent_id IS NULL AND state NOT IN ('done','archived')
         # AND all children done/verified AND merged_sha IS NULL

def verify_merged_nodes(db):
    """Stamp merged_sha for nodes whose branch is already on main (out-of-band merge)."""
    ...  # same git ancestor check, simpler: no graph_topics join needed
```

`reconcile_out_of_band_merges` is NOT deleted — it becomes `verify_merged_nodes` with the same git logic but a simpler DB query.

---

## 11. Agent-First Acceptance Criteria

Each phase boundary must be verifiable by a CLI-only agent with no human eyeballing.

| Area | Verifiable assertion |
|---|---|
| Node creation | `juggle add-node "T" --kind task --json` → `{"node_id": "<uuid>"}` |
| State after add (no deps) | `juggle node show <id> --json` → `{"state": "ready"}` |
| State machine transition | `juggle node show <id> --json` → state field equals expected string |
| Migration completeness | `SELECT COUNT(*) FROM nodes` == prior `COUNT(*) FROM threads` + `graph_topics` + `graph_tasks` |
| No stale states post-migration | `SELECT COUNT(*) FROM nodes WHERE state='pending'` == 0 |
| G1 gate | `juggle node show <id> --json` → `{"state": "done", "merged_sha": "<sha>"}` |
| Tick-on-demand latency | `date +%s%N; juggle add-node "..." --kind task; sleep 2; juggle node show <id> --json` → state='dispatching' or 'running' within 2s |
| SIGUSR1 coalescing | Send 10 SIGUSR1 in 100ms; `graph_tick` call count == 1 or 2 (not 10) — asserted via watchdog counters endpoint |
| complete-agent shim | `complete-agent <NODE_ID> "done"` exits 0; subsequent `juggle node show <id> --json` → `{"state": "done"}` |
| fail-agent shim | `fail-agent <NODE_ID>` exits 0; `juggle node show <id> --json` → `{"state": "failed-exec"}` |
| Arming deleted | `juggle autopilot arm <pid>` exits non-zero with "command not found" |
| projects-as-tags | `juggle add-node "T" --json` with no `--project` → `{"project_id": null}` or `{"project_id": "INBOX"}` |
| Orphan guard | `juggle watchdog inspect --json` → `orphan_nodes` array; no `reconcile_needed` field (old API) |
| Cockpit no armed gate | `juggle cockpit --out` without any armed project → DAG panel renders nodes (not "no armed graph") |
| Dep propagation | fail node X; `juggle node show <dep_of_X> --json` → `{"state": "blocked-failed"}` |

---

## 12. Risks + Rollback

### 12.1 Risks

| ID | Risk | Mitigation |
|---|---|---|
| RK1 | In-flight agents hold stale `complete-agent <thread_id>` — thread_id no longer maps to a node | Shim accepts both old thread UUIDs and new node UUIDs; migrated conversation nodes reuse their thread.id as nodes.id |
| RK2 | SIGUSR1 in Python signal handler touches non-async-safe code | Handler only sets a `threading.Event`; all DB/tmux work happens in the main loop thread |
| RK3 | `node_id` namespace collision (threads.id vs graph_topics.id vs graph_tasks.id) | All use UUID v4; collision probability negligible; migration validates uniqueness before insert |
| RK4 | `messages.thread_id` FK rename breaks existing queries | Add `node_id` column alias first (VIEW or column add), migrate reads, then rename in P8 |
| RK5 | cockpit snapshot reads both old tables and new in transition phases | Cockpit reads old tables until P8 (old tables still present); flip read to `nodes` in one atomic PR per P8 |
| RK6 | `_TRANSITIONS` dict expansion breaks existing task/topic tests | The transition fn is extended, not replaced; existing test inputs still valid (pending→open is a rename, not removal) |
| RK7 | `reconcile_out_of_band_merges` removal causes G1 false positives | It becomes `verify_merged_nodes` — same logic, renamed. Not deleted in P1–P7. |
| RK8 | `dogfood.py` subprocess calls to `get-agent`/`send-task` break in P3 before dogfood update | P3 preparation step updates dogfood.py to use `dispatch-node` BEFORE de-registering the CLI commands |

### 12.2 Rollback

```bash
# Before P1 migration:
git tag pre-topic-graph-merge HEAD

# Rollback at any phase (before P8 table cleanup):
git checkout pre-topic-graph-merge
# Old tables (threads/graph_topics/graph_tasks) were never dropped in P1–P7
# DB state: old tables still present, new nodes table can be DROPped
sqlite3 ~/.claude/juggle/juggle.db "DROP TABLE IF EXISTS nodes; DROP TABLE IF EXISTS node_edges;"
# Restart watchdog; system resumes from prior state
```

After P8 (old table drop), rollback requires DB restore from backup. P8 must be preceded by an explicit DB backup step.

---

## 13. Implementation Sequence

Each phase is independently shippable: tests green, `complete-agent` functional, watchdog running at every boundary.

---

### P1 — Unified `nodes` schema + migration (additive)

**Behavior:** Add `nodes` + `node_edges` tables. Run doctor migration inserting all rows from threads/graph_topics/graph_tasks into nodes. Old tables remain and are the authoritative read path. No behavioral change.

**Preparatory refactor:** None required; purely additive.

**RED tests to write first:**
```python
test_nodes_table_exists()  # schema migration creates nodes table
test_migration_row_count()  # COUNT(nodes) == COUNT(threads) + COUNT(graph_topics WHERE is_mirror=0) + COUNT(graph_tasks)
test_migration_no_pending_state()  # SELECT COUNT(*) WHERE state='pending' == 0
test_migration_node_edges_count()  # COUNT(node_edges) == COUNT(graph_edges)
test_migration_conversation_kind()  # all migrated thread rows have kind='conversation'
test_migration_task_parent_id()  # all migrated graph_tasks rows have parent_id == their topic's nodes.id
test_migration_in_flight_worktree()  # running topic node gets worktree fields from its thread
test_migration_idempotent()  # running migration twice does not duplicate rows
```

**Done check:**
```bash
uv run python src/juggle_cli.py doctor --dry-run  # exits 0, prints migration plan
uv run python src/juggle_cli.py doctor             # runs against tmp DB
sqlite3 /tmp/test.db "SELECT kind, COUNT(*) FROM nodes GROUP BY kind"
# → task: N, conversation: M, research: 0, decision: 0
uv run pytest tests/test_migration_nodes.py -v     # all RED tests green
```

---

### P2 — Single transition function folding threads.status (behind a seam)

**Behavior:** Introduce `node_transition(db, node_id, event)` in `dbops/db_nodes.py`. The function handles all kinds. `db_topics.topic_transition` and `db.set_thread_status` become thin shims calling `node_transition`. No callers change yet; shims translate old events to new events at the boundary.

**Preparatory refactor:** Extract `_TRANSITIONS` dict to `dbops/db_node_machine.py` (shared by old shims and new function). `db_graph._TRANSITIONS` imports from there.

**RED tests to write first:**
```python
test_node_transition_task_happy_path()   # open→ready→dispatching→running→integrating→verified→done
test_node_transition_research_path()     # open→ready→dispatching→running→done (no integrate)
test_node_transition_conversation()      # open→done via 'answer' event
test_node_transition_decision()          # open→done via 'answer' event
test_node_transition_illegal_kind()      # conversation 'deps_ready' raises InvalidTransition
test_node_transition_g1_gate()           # verified requires merged_sha set
test_node_transition_cas_dispatching()   # concurrent claim: second UPDATE fails (returns 0 rows)
test_thread_status_shim()               # set_thread_status('closed') → node state='done'
test_topic_transition_shim()            # topic_transition('dispatch') → node state='running'
```

**Done check:**
```bash
uv run pytest tests/test_node_machine.py tests/test_cmd_graph.py tests/test_completion_commands.py -v
# All pre-existing tests still pass; new state machine tests green
```

---

### P3 — Internal dispatch_node extraction

**Behavior:** Create `juggle_dispatch_internal.py::dispatch_node(db, node_id, prompt, role, model)`. The tick calls `dispatch_node()` directly instead of `_dispatch_via_pool`. `cmd_get_agent` and `cmd_send_task` Python functions become internal helpers (no `@cli.command` registration). `dogfood.py` updated to call `juggle dispatch-node <node_id>`. `release-agent` CLI removed.

**Preparatory refactor:** Move `cmd_get_agent` helper logic (pool walk, CAS assign, tmux pane spawn) and `cmd_send_task` helper logic (worktree create, tmux write) into `juggle_dispatch_internal.py`. Register a new `juggle dispatch-node <node_id>` CLI as a thin wrapper.

**RED tests to write first:**
```python
test_dispatch_node_assigns_agent()       # dispatch_node() → agent.status='busy'
test_dispatch_node_creates_worktree()    # node gets worktree_path populated
test_dispatch_node_sends_to_tmux()       # mock tmux; assert send called
test_dispatch_node_transitions_running() # node.state == 'running' after dispatch
test_graph_tick_uses_dispatch_node()     # tick no longer calls cmd_get_agent/cmd_send_task
test_dogfood_uses_dispatch_node()        # dogfood subprocess call uses dispatch-node
test_get_agent_cli_removed()             # juggle get-agent → "Unknown command" error
test_send_task_cli_removed()             # juggle send-task → "Unknown command" error
```

**Done check:**
```bash
uv run pytest tests/test_cli_agents.py tests/test_tmux_send_task.py tests/test_tmux_lifecycle.py -v
# Tests for removed commands deleted or rewritten; dispatch_node tests green
uv run python src/juggle_cli.py dispatch-node --help  # exits 0
```

---

### P4 — Tick-on-demand SIGUSR1 wake + 30s backstop

**Behavior:** Watchdog installs `SIGUSR1` handler that sets `threading.Event`. Main loop uses `event.wait(timeout=30)` instead of `time.sleep(30)`. Any `recompute_node_ready()` call that promotes nodes to `ready` fires `signal_watchdog_tick()`.

**Preparatory refactor:** Extract `_read_watchdog_pid()` from daemon_pidfile into `juggle_watchdog_signal.py`. Add `_watchdog_tick_count` counter (for test assertion on coalescing).

**RED tests to write first:**
```python
test_sigusr1_triggers_tick()             # send SIGUSR1; assert graph_tick called within 1s
test_sigusr1_coalesces()                 # 10 signals in 10ms; graph_tick called ≤2 times
test_30s_backstop_fires()               # no signal; mock sleep; tick fires at 30s boundary
test_signal_watchdog_tick_no_daemon()   # pid file absent → silent no-op (no exception)
test_signal_on_ready_promotion()        # recompute_node_ready promoting a node fires signal
```

**Done check:**
```bash
uv run pytest tests/watchdog/ -v
# New latency test: add-node → node ready → watchdog dispatches within 2s (integration test with tmp DB)
```

---

### P5 — add-node unified verb; create-thread/add-task become shims

**Behavior:** Register `juggle add-node` CLI with full spec (§6.1). `create-thread` and `graph add-task` become thin shims emitting a deprecation warning then delegating to `add-node`. Validation logic (cycle check, verify-cmd kind guard) lives in `add-node`.

**Preparatory refactor:** Extract `validate_add_task` (Kahn cycle check, mutable-state guard) into `juggle_node_validate.py` shared by both old and new paths.

**RED tests to write first:**
```python
test_add_node_task_no_deps()             # state='ready' immediately
test_add_node_task_with_deps()           # state='open' until deps done
test_add_node_research()                 # kind=research, state='ready', no verify_cmd
test_add_node_conversation()             # kind=conversation, state='open'
test_add_node_decision()                 # kind=decision, state='open'
test_add_node_verify_cmd_non_task()      # --verify-cmd on research → error
test_add_node_cycle_detection()          # cycle in deps → error
test_add_node_signals_watchdog()         # mock signal; assert fired on ready entry
test_add_node_default_inbox()            # no --project → project_id=NULL
test_create_thread_shim()               # create-thread "T" → add-node internally
test_graph_add_task_shim()              # graph add-task → add-node internally
```

**Done check:**
```bash
uv run python src/juggle_cli.py add-node "Test task" --json  # → {"node_id": "..."}
uv run pytest tests/test_add_node.py tests/test_cmd_graph.py -v
```

---

### P6 — Projects as tags; default INBOX graph; drop owning-topic requirement

**Behavior:** `project_id` on `add-node` defaults to NULL (→ INBOX). Tick iterates all ready task/research nodes regardless of project. Cockpit groups by project_id; NULL bucket = INBOX. `juggle autopilot arm/disarm/status` commands removed. `autopilot_armed_project` settings key removed.

**Preparatory refactor:** Remove `get_armed_projects()` call from `graph_tick`. Replace `for pid in armed_projects` loop with `SELECT DISTINCT project_id FROM nodes WHERE state='ready' AND kind IN (...)`.

**RED tests to write first:**
```python
test_tick_runs_without_armed_project()   # graph_tick on a DB with no autopilot setting → dispatches ready nodes
test_tick_dispatches_inbox_nodes()       # node with project_id=NULL gets dispatched
test_autopilot_arm_command_removed()     # juggle autopilot arm → error
test_cockpit_shows_inbox_bucket()        # cockpit snapshot has INBOX group for NULL project_id nodes
test_add_node_no_project_arg()           # add-node with no --project → project_id=NULL in DB
```

**Done check:**
```bash
uv run pytest tests/test_cmd_autopilot.py tests/test_autopilot_state.py -v
# autopilot arm/disarm tests deleted; "runs without armed" tests pass
uv run python src/juggle_cli.py add-node "inbox task" --json  # dispatches without arming
```

---

### P7 — Remove arming/--force-task; cockpit defaults to showing all

**Behavior:** Delete `check_task_guard`, `--force-task`, `TICK_OWNED_STATES`-as-guard. Cockpit graph panel and DAG remove the "no armed graph" gate — render all task/research nodes. `armed` field removed from cockpit state.

**Preparatory refactor:** Audit all `get_armed_project(s)` call sites (§5.3 src-facts): `juggle_graph_dispatch.py:200,222`, `juggle_cmd_agents_graph.py:113`, `juggle_watchdog_daemon.py:270`, `juggle_cockpit_modals.py:748`, `juggle_cockpit_graph_panel.py:221,305`, `juggle_cockpit_graph_dag.py:30–44`. Each gets a targeted edit.

**RED tests to write first:**
```python
test_force_task_flag_removed()           # --force-task → "unrecognized argument" error
test_cockpit_dag_no_armed_guard()        # _load_graph_dags without any armed project → returns nodes
test_cockpit_graph_panel_no_gate()       # graph panel renders without "no armed graph" message
test_check_task_guard_gone()             # send-task (or dispatch-node) never checks TICK_OWNED_STATES externally
```

**Done check:**
```bash
uv run pytest tests/test_autopilot_guards.py -v  # guard tests deleted or rewritten
uv run python src/juggle_cli.py cockpit --out     # graph panel visible without arming
```

---

### P8 — Collapse reads onto nodes; delete dead tables/columns

**Behavior:** Cockpit model reads exclusively from `nodes`. `messages`, `notifications`, `action_items` `thread_id` columns renamed to `node_id`. Drop `threads`, `graph_topics`, `graph_tasks`, `graph_edges` tables. Drop `schema_graph.py` DDL constants. Orphan guard simplified to use `nodes` only.

**Preparatory refactor:** Full audit of all SQL that touches dropped tables. Replace each query. Add a `juggle doctor --pre-p8-check` subcommand that lists remaining references to old tables (zero must remain before migration proceeds).

**RED tests to write first:**
```python
test_cockpit_model_reads_nodes()         # snapshot built from nodes, not threads join
test_messages_fk_renamed()              # messages.node_id FK resolves
test_graph_topics_table_gone()           # SELECT * FROM graph_topics → OperationalError
test_threads_table_gone()                # SELECT * FROM threads → OperationalError
test_orphan_guard_nodes_query()          # find_orphan_nodes uses nodes table, not graph_topics join
test_node_edges_table_exists()           # node_edges present; graph_edges absent
```

**Done check:**
```bash
uv run pytest -q  # full suite green
uv run python src/juggle_cli.py doctor --dry-run  # no old-table migration steps remain
uv run python src/juggle_cli.py cockpit --smoke --all-viewports  # viewport matrix passes
sqlite3 ~/.claude/juggle/juggle.db ".tables"  # no 'threads', 'graph_topics', 'graph_tasks', 'graph_edges'
```

---

## 14. Devil's Advocate

### 14.1 Weakest assumptions and failure modes

| # | Assumption | Failure mode | Mitigation |
|---|---|---|---|
| DA1 | Old thread UUIDs and graph_topics UUIDs never collide | If two rows share a UUID (astronomically unlikely but possible in test fixtures), migration INSERT fails on PK constraint | Migration validates `SELECT id FROM threads INTERSECT SELECT id FROM graph_topics` = empty before inserting |
| DA2 | `threading.Event` is async-signal-safe in CPython | CPython GIL means `.set()` from signal handler is safe in practice; not guaranteed by POSIX | Fallback: use `os.write(pipe_fd, b'\0')` to a self-pipe read by `select()` in main loop — pure POSIX safe |
| DA3 | All in-flight agents call `complete-agent <thread_id>` which still maps post-migration | If a thread_id was NOT migrated to nodes (e.g., a plain background thread with no graph_topic binding), the shim lookup fails | Migration step copies ALL thread rows to nodes (including plain threads with no graph affiliation); shim lookup always finds a node |
| DA4 | `graph_tasks.topic_id IS NULL` rows ("flat tasks") become orphan nodes (parent_id=NULL) and the tick dispatches them without a worktree | Flat tasks in the current code relied on `_dispatch_flat_task_fallback` which handled them differently; the unified tick may not set up worktrees correctly for them | P1 migration marks flat-task nodes with a `_legacy_flat_task=1` sentinel (or tag); P3 dispatch_node handles them the same as topic-tier nodes; write a specific test |
| DA5 | Removing `reconcile_out_of_band_merges` (renamed to `verify_merged_nodes`) keeps G1 integrity | If a manual git merge happens between ticks and `merged_sha` is not stamped, the node stalls at `verified` indefinitely | `verify_merged_nodes` runs every tick (same cadence as current reconcile); the 30s backstop ensures it runs even without SIGUSR1 |
| DA6 | P8 table drop is irreversible | Any missed query against dropped tables crashes at runtime post-P8 | `--pre-p8-check` subcommand explicitly enumerates all reference sites; P8 ships only after check shows zero references; backup required before P8 runs |

---

## 15. Spec Self-Review

**Placeholders:** None. All section headers are filled.

**Consistency check:**
- `complete-agent` shim (§9) references `integrate_start` → `integrate_ok` → `g1_pass` events consistent with §4.2 table. ✓
- `signal_watchdog_tick()` call sites (§5.2) cover add-node (P5), recompute_node_ready (P2+), and complete-agent shim (§9). ✓
- Deletion list (§10) cross-references source-facts citations (§4.1–§5.3 src-facts). ✓
- Migration row mapping (§8.2) covers all `is_mirror` variants. ✓
- P3 explicitly updates `dogfood.py` before de-registering `get-agent`/`send-task` CLI (RK8 mitigation). ✓
- `release-agent` is removed in P3 (§10), shim section (§9.2) explains stale-claim recovery. ✓

**Ambiguity remaining:**
- `messages.thread_id` FK rename happens in P8, but between P1 and P7 the column name stays `thread_id` pointing at a `nodes.id` (since threads.id == nodes.id for conversation nodes). No code change needed in P1–P7 because the FK value is the same UUID; only the column name is cosmetic until P8.
- Sub-task nodes (parent_id set) are never directly dispatched by the tick; the tick dispatches only parent_id=NULL nodes. Child task state is managed by the agent working on the parent, which calls `complete-agent` per child sub-task. This matches the current behavior where the agent working on a topic manages its own task-level state.

**Scope:** This spec covers DB schema, state machine, dispatch plumbing, migration, CLI surface, and cockpit read-path. It does NOT spec cockpit rendering changes beyond "drop the armed gate" (cockpit layout is a separate concern).
