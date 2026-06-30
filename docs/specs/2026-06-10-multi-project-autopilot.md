# Spec — Multi-Project Parallel Autopilot (3-Tier: Project → Topic → Task)

**Date:** 2026-06-10 · rev 2026-06-11 (thread WQ folds in R9) · **Threads:** WM, WQ
**Status:** ready for planning
**Inputs:** `2026-06-10-multi-project-autopilot-BRIEF.md` (R1–R9; R9 = canonical
3-tier hierarchy + user-decided hybrid execution model),
`2026-06-10-multi-project-autopilot-BRAINSTORM.md` (option analysis, all code
claims verified against source).
**Assumes:** thread WL's dispatch cross-connection-visibility fix is merged.

## 1. Overview

Two changes, layered:

1. **Multi-project arming (R1–R8):** a SET of projects armed simultaneously; the
   single watchdog tick drives all armed graphs each cycle under the global
   agent budget with a fair cross-project policy; disarming one project leaves
   the rest running; cockpit and hooks reflect the set; ad-hoc `send-task` to an
   armed project is code-refused (R8).
2. **3-tier hierarchy (R9, user-decided):** **Project → Topic → Task.** Today the
   graph is flat — each `graph_nodes` row creates its OWN top-level topic via
   `create_thread`, so task ≡ topic. After this change a **Topic** owns a task-DAG;
   execution is **hybrid topic-agent / task-commits**: ONE long-lived agent + ONE
   worktree per Topic; each Task is a discrete TDD unit with its own commit +
   `verify_cmd`; `juggle integrate` runs ONCE per Topic after all its tasks pass.
   Only TOPICS count toward the concurrency budget (`MAX_THREADS`, ~10
   non-archived); tasks are sub-units, never top-level topics.

The integrate-once-per-topic property is not incidental: it directly bounds the
integrate/lock contention class we hit in production (commit `5fc261b`,
"FULL fsync-per-commit caused integrate lock-hold storms" — N per-task
integrates serialized on the repo lock). With topics, a 10-task topic produces
ONE rebase+merge instead of ten.

Non-goals: per-project budget config, weighted priorities, parallel/threaded
ticks, per-task concurrency inside a topic (tasks are sequential by design —
one agent), any new daemon. The watchdog tick remains the sole dispatcher
(DA B4/M1); the settings table remains the sole arming authority (DA M6).

**Reuse constraint (user directive):** existing constructs are reused maximally —
`graph_tick`, `_dispatch_via_pool` → `cmd_get_agent`/`cmd_send_task`, the
claim CAS, `dbops.db_graph` state machine, `mark_completion`, per-thread
integrate, hooks, cockpit graph panel. New modules only for genuinely new
concerns (topic store, scheduler).

## 2. Design

### 2.1 Armed-set storage (R1, R6) — unchanged from rev 1

The existing settings key `autopilot_armed_project` holds a **comma-separated
ordered list** of project ids. A single-element value is identical to today's
scalar, so existing DBs need **no migration** for arming.

New module **`src/juggle_autopilot_state.py`** (extraction —
`juggle_graph_dispatch.py` is at the 300-line LOC gate) owns the accessor API:

```python
ARMED_PROJECT_KEY = "autopilot_armed_project"   # moved here; re-exported from dispatch

def get_armed_projects(db) -> list[str]   # CSV parse, strip, drop empties, dedupe (keep first), [] on any error
def set_armed_projects(db, pids: list[str]) -> None   # join; None/"" when empty
def arm_project(db, pid) -> list[str]     # append if absent; rejects pid with ',' or whitespace (ValueError)
def disarm_project(db, pid) -> list[str]  # remove if present; returns new set
def get_armed_project(db) -> str | None   # COMPAT SHIM: first armed or None
```

`juggle_graph_dispatch` re-exports `ARMED_PROJECT_KEY` and `get_armed_project`
so existing imports keep working.

### 2.2 Data model (R9) — `graph_topics` + `graph_nodes.topic_id`

**Ground truth today:** `graph_nodes(id PK, project_id, title, prompt,
verify_cmd, state, thread_id, handoff, diffstat, verified_at, …)` with
`graph_edges(node_id, depends_on_id)`; `node_transition` is the only state
writer; the dispatcher's CAS claim and `recompute_ready`'s CAS are the two
sanctioned exceptions. A juggle "topic" IS a `threads` row (`threads.topic`
label; `MAX_THREADS` caps non-archived threads).

**New table (migration 37):**

```sql
CREATE TABLE IF NOT EXISTS graph_topics (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES projects(id),
  title       TEXT NOT NULL,
  objective   TEXT NOT NULL DEFAULT '',
  state       TEXT NOT NULL DEFAULT 'pending',
  thread_id   TEXT,
  handoff     TEXT,
  diffstat    TEXT,
  verified_at TEXT,
  created_at  TEXT NOT NULL, updated_at TEXT NOT NULL);
-- plus: ALTER TABLE graph_nodes ADD COLUMN topic_id TEXT REFERENCES graph_topics(id);
-- indexes: graph_topics(project_id, state); graph_nodes(topic_id)
```

- **Topics reuse the node state machine verbatim** — same `VALID_STATES`, same
  `_TRANSITIONS` (pending → ready → dispatching → running → integrating →
  verified; failed-exec/-integration/-verify; blocked-failed). The transition
  map and TICK_OWNED/PROTECTED sets move to shared constants; a thin
  `dbops/db_topics.py` provides `topic_transition` / CRUD / ready-set over
  `graph_topics` with the same CAS discipline (`claim` CAS in the dispatcher,
  `recompute_topic_ready` CAS). No second state-machine invention.
- **Tasks** are `graph_nodes` rows with `topic_id` set. Task rows keep
  `prompt`/`verify_cmd`/`state`/`handoff`. A task's `thread_id` stays NULL in
  3-tier execution (the TOPIC owns the thread); it remains populated only on
  legacy synthetic topics (below).
- **Edges stay task-level** (`graph_edges` unchanged). Intra-topic edges order
  the agent's sequential execution (topological, `created_at,id` tie-break).
  **Topic-level deps are derived:** topic A depends on topic B iff any task of
  A has an edge to a task of B (A≠B). A topic is ready-eligible when every
  derived dep topic is `verified`. One SQL join, no new edge table.

**Migration 37 backfill (flat → 3-tier):** every existing `graph_nodes` row
with `topic_id IS NULL` gets a **synthetic single-task topic**
`id = 'T-' || node.id` that ADOPTS the node's `state`, `thread_id`, `handoff`,
`diffstat`, `verified_at`, `title`, and project; the node's `topic_id` is set
to it. Properties:

- task ≡ topic is preserved exactly — a 1-task topic behaves byte-for-byte like
  today's flat node (same states, same thread binding, same integrate path), so
  existing single-node usage needs **zero behavioral migration**.
- **In-flight flat graphs keep running:** a node mid-`running` yields a synthetic
  topic mid-`running` bound to the same thread; the (now topic-level) sweep,
  completion marking, and stale-claim logic continue on the adopted state. No
  drain-the-world flag day.
- Idempotent and re-runnable (`topic_id IS NULL` guard), consistent with the
  migration 34–36 try/skip pattern in `dbops/migrations_recent.py`.

### 2.3 Execution model (R9) — hybrid topic-agent / task-commits

Per dispatched topic (user decision 2026-06-10):

1. The tick claims a READY topic (CAS), creates **one thread** (`create_thread`
   with the topic title — this is what counts against `MAX_THREADS`), binds
   `graph_topics.thread_id`, and dispatches **one agent** via the existing
   `_dispatch_via_pool` → `cmd_get_agent`/`cmd_send_task` path with
   `force_node=True`. One worktree per topic — exactly the existing per-thread
   worktree machinery, untouched.
2. The hydrated topic prompt contains: project objective, dep-TOPIC handoffs +
   diffstats (existing `build_hydration` shape, fed topic rows), and the
   topic's ordered task list (each task: id, title, prompt, `verify_cmd`), plus
   the contract: per task do TDD → run `verify_cmd` → **commit** → mark via
   `juggle graph mark-task <task-id> --handoff '…'` (or `--fail`). Tasks are
   sequential; the agent never opens threads or worktrees of its own.
3. `mark-task` maps onto the EXISTING node machine via `mark_completion(db,
   task_id, integrate_ok=True, verify_ok=…)`: a task's `verified` means
   "committed in the topic worktree + its verify_cmd green" — **NOT merged**.
   `verified-means-merged` is hereby a **TOPIC-level invariant**: only
   `graph_topics.state='verified'` implies code in main. (Task-level hydration
   across topics is therefore forbidden; cross-topic hydration uses topic
   handoffs, written at integrate time.)
4. The agent finishes with the normal `agent complete <topic-thread>` →
   the existing per-thread `juggle integrate` runs **once for the whole topic**
   (free: integrate is already per-thread, and the topic owns the thread) →
   `mark_graph_topic` (the topic twin of today's `mark_graph_node`) maps
   (integrate_ok, verify_ok) onto the topic machine. **Completion gate:**
   agent complete on a topic thread REFUSES (fail loud, nothing marked) unless
   every task of the topic is terminal (`verified` or `failed-*`); topic
   verify_ok = all tasks `verified`.
5. Topic failure semantics: any task left `failed-*` → topic `failed-verify`
   (main untouched — integrate is skipped per existing DA M3 path); dependents
   block via the existing `propagate_failure`, now applied at topic level.

### 2.4 Graph spec format + loading

`project-graph load` gains a topic tier; `juggle_graph_upsert.parse_graph_spec`
extends:

```markdown
## topic <topic-id>: <Title>
<objective lines>                 (optional)

### <task-id>: <Title>
deps: <task-id>, <task-id>        (optional; intra- or cross-topic)
verify_cmd: pytest tests -q       (optional; same lint allowlist)
<remaining lines = task prompt>
```

**Legacy fallback (R6):** a spec with old flat `## <node-id>: <Title>` sections
(no `## topic` headers) loads exactly as before, wrapped in one synthetic
single-task topic per node (same shape migration 37 produces). Existing spec
files keep working unmodified. Mixing both forms in one file is rejected
(fail loud).

`juggle graph add-task` gains `--topic <topic-id>` (required when the project
has any real topic; defaults to a fresh synthetic topic otherwise — preserving
today's call signature for flat projects). Guarded upsert / PROTECTED_STATES /
cycle validation apply at both tiers (topic cycle = cycle in derived topic deps).

### 2.5 CLI surface (R1)

- `autopilot arm P` — **adds** P to the set (idempotent; PR-mode refusal and
  project-exists check unchanged, per project). Global flag ON, as today.
- `autopilot disarm [P]` — with P: remove just P (unknown P → stderr + exit 1).
  Without: clear the set. Global flag untouched.
- `autopilot off [P]` — with P: remove just P; clear the global flag **only if
  the set becomes empty**. Without: clear set + flag.
- `autopilot status [--json]`:

  ```
  Autopilot global: ON
  Armed projects (2): juggle, lifeos
    juggle: topics 2/5 done (1 running), tasks 7/23
    lifeos: no graph loaded
  ```

  JSON: `{"global_on", "armed_projects": [pid…], "graphs": {pid: {topics:
  counts, tasks: counts} | null}, "diverged", "armed_project": <first|null>,
  "graph": <first graphs value|null>}` — the last two deprecated one release.

### 2.6 Tick (R2, R4) — claims TOPICS

`graph_tick(db, mgr=None, *, dispatch_fn=None) -> dict` keeps signature and
never-raises contract. Shape:

1. `armed = get_armed_projects(db)`; empty → return.
2. **Per project** (isolation, R4): topic-level stale-claim sweep
   (`dispatching` >10 min, `thread_id IS NULL` → ready, same SQL on
   `graph_topics`) + `recompute_topic_ready` + collect ready TOPICS. A
   per-project exception logs and skips THAT project only.
3. ONE cross-project dispatch order via the scheduler (2.7) over topics.
4. Existing claim → thread create → bind → hydrate → dispatch → running body,
   transplanted from nodes to topics:
   - CAS claim on `graph_topics` (same single conditional UPDATE pattern).
   - `db.update_thread(thread_id, project_id=pid)` uses the topic's project
     (carried as `(pid, topic)` pairs — no loop-variable capture).
   - per-topic disarm guard: skip the topic if its project left the armed set
     mid-batch; other projects' topics keep going.
   - Capacity (`MAX_THREADS` ValueError "Maximum of", pool `CapacityError`)
     **breaks the whole pass** — the cap is global; unvisited topics were never
     claimed. `MAX_THREADS` now bounds exactly what R9 demands: concurrent
     TOPICS (each topic = 1 thread = 1 agent); tasks consume no budget.
   - Retry cap (`MAX_DISPATCH_FAILS`, `_give_up_dispatch`) keyed by topic id.

Stats dict keeps its flat shape (`dispatched/swept/deferred/errors`), now
containing **topic ids** — consumers (watchdog, tests) treat them opaquely.

### 2.7 Fair scheduler (R3) — topic-level, module `src/juggle_graph_scheduler.py`

The pure function is tier-agnostic (it orders opaque dicts); R9 changes WHAT it
is fed, not the policy:

```python
def interleave_ready(ready_by_project: dict[str, list[dict]],   # ready TOPICS
                     in_flight: dict[str, int],                 # in-flight TOPIC count
                     armed_order: list[str]) -> list[tuple[str, dict]]
```

**Policy: least-loaded-first round-robin over TOPICS.** Sort armed projects by
in-flight topic count ascending (`dispatching|running|integrating`), tie-break
arm order; emit ready topics one-per-project-per-round. Within a project, ready
order stays `created_at, id`. Tasks inside a topic are sequential (one agent)
and never enter the scheduler — there is no per-task concurrency budget by
construction.

Justification (failure-mode analysis, unchanged in substance from rev 1 but now
counted in topics — the topic is the cost unit, which makes fairness MORE
accurate, since a 50-task topic and a 2-task topic each consume exactly one
agent slot):

- **Project with 50 ready topics vs project with 2, budget 5:** interleave
  yields 3+2 — the small project drains fully; sequential per-project ticking
  gives 5+0 forever.
- **Budget admits 1 dispatch/tick:** least-loaded is self-correcting without
  persisted state — last tick's winner carries in-flight ≥1 and sorts after an
  idle project. Plain arm-order round-robin starves without a cursor.
- **One project holding all slots while others have nothing ready:**
  utilization, not starvation; a newly-ready topic elsewhere has 0 in-flight
  and sorts first as slots free.
- Stateless + deterministic → pure-function unit tests; no cursor key.

Rejected: per-project hard caps (waste slack, then re-distribute ≡ round-robin
with extra steps); weighted config (YAGNI); per-task scheduling (defeats R9's
budget model and resurrects integrate-per-task lock storms).

`MAX_THREADS` / `MAX_BACKGROUND_AGENTS` stay **global-only**.

### 2.8 Hooks (R7)

- `_ARMED_CARVEOUT` names the comma-joined set and the 3-tier rule: topics are
  tick-owned; NEW work for an armed project enters as a task via
  `juggle graph add-task … --topic <t>`, never ad-hoc send-task.
- Graph injection: one `build_graph_injection(db, pid, budget=per)` line per
  armed project, `per = max(160, 500 // len(armed))`; the injection now counts
  topics ("topics 2/5, tasks 7/23; running: T-auth (task 3/6)") — topic-level
  granularity fits the budget where 23 task titles would not.
- Degrade-to-empty-string on error preserved.

### 2.9 Cockpit (R5) — project → topic → task tree

- Loader: `load_graph_dags(conn) -> list[GraphDag]`, one per armed project with
  topics; **DAG nodes are TOPICS** (the derived topic-dep edges are the DAG
  edges). Each topic cell renders `⬢ <topic-id> 3/6` (tasks verified/total) —
  the existing rank layout, glyphs, fold/pan machinery apply unchanged because
  the panel just receives fewer, coarser nodes.
- The task tier appears in the node detail modal (`_GraphNodeModal`): selecting
  a topic lists its tasks with per-task state glyphs — tree depth lives in the
  modal, not the rank layout (no new layout engine).
- Multi-project: DAGs render **stacked**, each under a project-titled rule;
  selection iterates the concatenated topic list. `load_graph_dag` stays as a
  first-or-None compat shim; `CockpitState.graph_dag` joined by `graph_dags`.
- Gate: `cockpit --smoke --all-viewports` green with 0, 1, and 3 armed graphs.

### 2.10 Backward compatibility (R6)

- 1-element CSV ≡ legacy scalar arming: no migration.
- Migration 37 synthetic topics ≡ flat nodes: 1-task topics behave identically,
  including in-flight ones (state + thread adopted).
- Legacy flat spec files load unchanged (2.4 fallback).
- `get_armed_project` shim, `ARMED_PROJECT_KEY` re-export, status-JSON
  deprecated fields (`armed_project`, `graph`), `load_graph_dag` shim — all one
  release.
- Existing dispatch/contract test suites are the safety net: they must pass
  with assertions updated ONLY where they touch renamed surfaces (node→topic in
  tick stats, status JSON shape); regression pins may not be weakened.

### 2.11 Armed-project dispatch guard (R8) — adapted to 3-tier

Extends `juggle_cmd_agents_graph.check_node_guard`:

- Thread bound to a TOPIC in a tick-owned state → refuse (the tick dispatches
  it); operator-territory states (`failed-*`, `pending`) stay manually
  redispatchable — DA B5 semantics, lifted from node to topic.
- Thread **unbound** but `thread.project_id` in the armed set → refuse, pointing
  to `juggle graph add-task … --topic <t> --project <pid>`; `--force-node`
  remains the single override (the tick passes it). R8's narrow exceptions
  (graph-machinery fixes; planning whose output IS the nodes) are operator
  judgment via the flag, never content heuristics.
- Disarming lifts the guard instantly; unarmed projects unaffected.

## 3. Devil's Advocate

### Assumption-by-assumption challenge

| # | Assumption | What if wrong? | Mitigation |
|---|---|---|---|
| A1 | Project ids never contain commas/whitespace | CSV split corrupts the armed set | `arm_project` rejects with ValueError; ids are slugs — belt-and-braces |
| A2 | All readers of the armed key are in-repo and updated together | A missed raw reader treats `"a,b"` as one id | Plan grep gate: every literal `autopilot_armed_project` outside the accessor + cockpit loader is a failure |
| A3 | WL's visibility fix is merged | Multi-topic dispatch multiplies an existing race | Plan Task 0 verifies presence on base; absence is reported, not built on silently |
| A4 | In-flight TOPIC count is a fair load proxy | A project running one 50-task topic is "loaded 1" while another running five 1-task topics is "loaded 5" | Correct by design: the budgeted resource is agents/threads, and each topic holds exactly one — the proxy now EQUALS the resource. Task-weighted fairness would re-couple budget to tasks, which R9 explicitly rejects |
| A5 | Global capacity break-out-of-pass stays correct | A future per-project cap would under-dispatch | Cap is global today (`dbops/threads.py` MAX_THREADS, agent pool); fair prefix at break time; documented |
| A6 | The node state machine fits topics unchanged | A topic-only state appears later (e.g. 'paused') | `_TRANSITIONS` is shared data, not duplicated code — extending it is additive |
| A7 | Old binary + new DB (rollback) degrades safely | Old code ignores `graph_topics`, reads flat nodes whose states migration 37 left INTACT on the node rows | Reads degrade to the flat view; synthetic topics go stale but never corrupt. We do not engineer beyond this for binary rollback |
| A8 | `--force-node` suffices for R8 exceptions | Legit machinery-fix dispatch gets an annoying refusal | Refusal names the flag + exceptions; heuristics would create silent bypasses |
| A9 | One agent reliably finishes a multi-task topic | Agent dies at task 3/6: topic worktree holds 3 commits, 3 tasks unmarked | Existing agent-death path (`test_graph_agent_death` machinery) maps to topic `failed-exec`; tasks keep their per-task states, so the RESUME story is a spec reload → topic `pending` with verified tasks skipped by the agent prompt ("tasks already verified: skip"). Pinned in plan |
| A10 | Completion gate (all tasks terminal) is enforceable | Agent calls agent complete early → half-done topic integrates | Gate is CODE in cmd_complete_agent (refuse + exit 1, nothing marked), not prompt; pinned test |

### Weakest item: the schema migration (37) — challenged hard

**Claim:** backfilling synthetic topics over live data is the weakest link in
this design. Failure modes examined:

1. **In-flight adoption races the tick.** A node is `dispatching` while
   migration 37 runs; the tick (new code) immediately sweeps the synthetic
   topic as a stale claim because `graph_topics.updated_at` is fresh but
   `thread_id` was adopted… — actually safe: sweep requires `thread_id IS
   NULL`; adopted bindings carry the thread. The dangerous window is a node
   `dispatching` with `thread_id` still NULL (claim-to-bind window): the
   synthetic topic inherits that and is swept to `ready` after 10 min — which
   is EXACTLY the recovery the flat system would have performed. Verdict: the
   state adoption must copy `updated_at` from the node (not `now()`) so sweep
   timing is preserved; pinned in the plan.
2. **Half-applied migration** (topics created, `topic_id` backfill crashes):
   re-run is idempotent (`topic_id IS NULL` guard creates only missing topics;
   `INSERT OR IGNORE` on `'T-'||id`). The migration runs in one transaction
   per the migrations_recent pattern.
3. **Old completion path writes node state after migration:**
   `mark_graph_node` (by thread) would mark the NODE while the new tick watches
   the TOPIC. Mitigation: completion marking migrates in the same plan task as
   the tick (single release); `get_node_by_thread` lookups are replaced by
   topic-by-thread lookups with a node fallback for synthetic topics. The plan
   sequences schema → topic store → tick/completion BEFORE any release point.
4. **`'T-'||id` collides with an existing node id** (a node literally named
   `T-x` while node `x` exists): collision check in the migration; on collision
   use `'T#'||id` — deterministic, logged. (Cosmetic, but silent PK violation
   would abort the migration transaction.)

**Simpler alternative considered:** no new table — encode topics as
`graph_nodes` rows with `kind='topic'` and parent edges. Rejected: every
existing query (`ready_eligible`, sweep, counts, cockpit loader) would need a
`kind` filter to stay correct — a missed filter silently dispatches a topic
header as a task. A separate table makes the old queries wrong-by-type instead
of wrong-by-silence, and the topic store is ~100 lines of thin reuse.

**Flat→3-tier back-compat verdict:** the synthetic-topic device means there is
no "compat mode" branching in the execution path — after migration, EVERYTHING
is 3-tier; flat is just the 1-task degenerate case. That is the property that
keeps the tick/completion code single-pathed.

### Topic-vs-task scheduling fairness — challenged

Could topic-level fairness starve a project whose topics are huge? Project A:
one 50-task topic; project B: fifty 1-task topics. A gets 1 agent, B gets up to
budget−1. Is that unfair to A? No — A *cannot use* more than one agent (its 50
tasks are sequential inside one topic by the user's execution model); giving A
more slots would idle them. The budget unit, the thread unit, and the scheduling
unit are now the same object, which eliminates rev 1's proxy mismatch (A4).
The real cost: total wall-clock for a huge topic. That is a spec-authoring
concern (decompose into more topics), surfaced by cockpit progress (`3/50`),
not a scheduler defect.

### What happens to in-flight flat graphs — explicit answer

Migration 37 adopts their exact state + thread binding into synthetic topics
(including `updated_at`, see weakest-item #1). Running agents complete via
agent complete → topic-by-thread lookup finds the synthetic topic → existing
integrate-once + `mark_completion` semantics apply (a 1-task topic's gate is
trivially satisfied: its single task adopted `verified`-or-terminal state — for
a synthetic topic the task row mirrors the topic, and the completion gate
treats synthetic single-task topics as gated by the topic's own lifecycle).
Nothing is drained, cancelled, or re-dispatched.

### Edge cases hunted

- **Agent dies mid-topic (A9):** topic `failed-exec`, per-task states preserved,
  reload-resume pinned.
- **Premature agent complete (A10):** code-refused, nothing marked.
- **Empty topic (0 tasks) in a spec:** rejected at load (fail loud) — a topic
  with no tasks can never satisfy its completion gate.
- **Cross-topic dep on an unverified task:** impossible to hydrate — hydration
  only reads TOPIC handoffs of verified (= merged) topics; the derived-dep
  readiness rule keeps the dependent topic un-ready until then.
- **Intra-topic dep cycle / cross-topic cycle:** load-time validation at both
  tiers (existing `find_cycle` on tasks; same algorithm on derived topic deps).
- **Disarm-one-keep-rest:** per-topic guard; pinned.
- **Poisoned project scan (R4):** per-project isolation; pinned.
- **50-vs-2 ready topics:** interleave; pinned with capacity-limited FakeDispatch.
- **MAX_THREADS cap:** defers topics of ALL projects fairly, claims released; pinned.
- **R8 guard:** unbound armed-project thread refused; disarm lifts; topic-bound
  operator-state allowed; pinned.
- **Status divergence warning:** set non-empty + flag absent; names all projects.
- **Cockpit small viewport, 3 stacked topic-DAGs:** viewport smoke matrix gates;
  topics-as-nodes makes DAGs SMALLER than rev 1's task-DAGs.
- **`legacy + topic` mixed spec file:** rejected (2.4).

## 4. Open questions

None requiring a user decision — R9's genuinely contentious fork (execution
model) was decided by the user (hybrid). Remaining choices (synthetic-topic id
scheme, modal-based task tier, topic objective optional) are resolved above
with rationale; all are cheap to revisit post-implementation.

## 5. Verification gates (agent-runnable)

- `uv run pytest -q` green (incl. new pins; pre-existing failures documented
  with proof they exist on base).
- Migration: scripted flat DB (3 nodes, one `running` with thread) →
  `db init`/doctor path applies 37 → `sqlite3` asserts synthetic topics adopt
  state/thread/updated_at and `topic_id` backfilled.
- `uv run src/juggle_cli.py autopilot status --json` over a scripted 2-project
  arm/disarm sequence yields deterministic JSON (exact assertions in plan).
- `uv run src/juggle_cli.py cockpit --smoke --all-viewports` green.
- `uv run src/juggle_cli.py doctor --dry-run` smoke vs tmp DB.
