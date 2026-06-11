# Spec — Multi-Project Parallel Autopilot

**Date:** 2026-06-10 · **Thread:** WM · **Status:** ready for planning
**Inputs:** `2026-06-10-multi-project-autopilot-BRIEF.md` (requirements R1–R7),
`2026-06-10-multi-project-autopilot-BRAINSTORM.md` (option analysis, all code
claims verified against source).
**Assumes:** thread WL's dispatch cross-connection-visibility fix is merged.

## 1. Overview

Allow a SET of projects to be armed for autopilot simultaneously. The single
watchdog tick drives ready nodes across ALL armed graphs each cycle under the
existing global agent budget, with a fair cross-project scheduling policy.
Disarming one project leaves the rest running. Cockpit graph mode shows every
armed graph; hooks inject the full set. Single-project behavior is unchanged
(a 1-element set behaves byte-for-byte like today's scalar).

Non-goals: per-project budget config, weighted priorities, parallel/threaded
ticks, any new daemon. The watchdog tick remains the sole dispatcher (DA B4/M1);
the settings table remains the sole arming authority (DA M6).

## 2. Design

### 2.1 Armed-set storage (R1, R6)

The existing settings key `autopilot_armed_project` now holds a **comma-separated
ordered list** of project ids. A single-element value is identical to today's
scalar, so existing DBs need **no migration** and `doctor` needs no change.

New module **`src/juggle_autopilot_state.py`** (extraction — `juggle_graph_dispatch.py`
is at 296 lines, at the LOC gate) owns the accessor API:

```python
ARMED_PROJECT_KEY = "autopilot_armed_project"   # moved here; re-exported from dispatch

def get_armed_projects(db) -> list[str]   # CSV parse, strip, drop empties, dedupe (keep first), [] on any error
def set_armed_projects(db, pids: list[str]) -> None   # join; None/"" when empty
def arm_project(db, pid) -> list[str]     # append if absent; rejects pid with ',' or whitespace (ValueError); returns new set
def disarm_project(db, pid) -> list[str]  # remove if present; returns new set
def get_armed_project(db) -> str | None   # COMPAT SHIM: first armed or None
```

`juggle_graph_dispatch` re-exports `ARMED_PROJECT_KEY` and `get_armed_project`
so existing imports (`juggle_cmd_autopilot`, `juggle_hooks_autopilot`, tests)
keep working.

### 2.2 CLI surface (R1)

- `autopilot arm P` — **adds** P to the set (idempotent; PR-mode refusal and
  project-exists check unchanged, per project). Global flag set ON, as today.
- `autopilot disarm [P]` — with P: remove just P (error to stderr + exit 1 if P
  not armed — fail loud). Without: clear the whole set. Global flag untouched
  (today's contract).
- `autopilot off [P]` — with P: remove just P; clear the global flag **only if
  the set becomes empty**. Without: clear set + flag (today's contract).
- `autopilot status [--json]` — text lists each armed project with its own
  progress line:

  ```
  Autopilot global: ON
  Armed projects (2): juggle, lifeos
    juggle: 3/14 done, 2 ready
    lifeos: no graph loaded
  ```

  JSON: `{"global_on": bool, "armed_projects": [pid…], "graphs": {pid: counts|null},
  "diverged": bool, "armed_project": <first|null>, "graph": <counts of first|null>}`.
  The last two are deprecated compat fields (one release), documented in the
  command help. `diverged` = set non-empty while flag file absent (unchanged
  semantics, now set-based).

### 2.3 Tick (R2, R3, R4)

`graph_tick(db, mgr=None, *, dispatch_fn=None) -> dict` keeps its signature and
its never-raises contract. New shape:

1. `armed = get_armed_projects(db)`; empty → return (notify-only, unchanged).
2. **Per project** (isolation, R4): `sweep_stale_claims(db, pid)` +
   `recompute_ready(db, pid)` + collect ready nodes. Both functions are already
   `project_id`-parameterized — pure loop. A per-project exception logs and
   skips THAT project only (today it skips the whole tick; with N projects the
   blast radius must shrink to one graph).
3. Build ONE cross-project dispatch order via the scheduler (2.4).
4. Run the existing claim → create-thread → bind → dispatch → running body over
   that ordered list, with two changes:
   - disarm-mid-batch guard becomes per-node: skip the node if its project is no
     longer in `get_armed_projects(db)` (other projects' nodes keep going — R4).
   - `db.update_thread(thread_id, project_id=node_project)` uses the node's own
     project (carried with each entry), not a single `armed` variable.
   - Capacity (`MAX_THREADS` ValueError, `CapacityError`) still **breaks the
     whole pass** — the cap is global; remaining nodes are deferred to next tick
     with their claims released, exactly as today.

Stats dict unchanged in shape (`dispatched/swept/deferred/errors` flat lists of
node ids) — consumers (watchdog, tests) don't break; node ids are globally unique.

### 2.4 Fair scheduler (R3) — new module `src/juggle_graph_scheduler.py`

Pure function, no DB:

```python
def interleave_ready(ready_by_project: dict[str, list[dict]],
                     in_flight: dict[str, int],
                     armed_order: list[str]) -> list[tuple[str, dict]]
```

**Policy: least-loaded-first round-robin.** Sort armed projects by current
in-flight node count ascending (states `dispatching|running|integrating`),
tie-break by arm order; then emit ready nodes one-per-project-per-round until
all ready lists are exhausted. Within a project, ready order stays
`created_at, id` (existing `list_nodes` order).

Justification (failure-mode analysis):

- **50-vs-2 ready, budget 5:** interleave yields P1:3, P2:2 — the small graph
  drains completely. A naive sequential tick gives P1:5, P2:0 forever.
- **Budget admits 1 dispatch/tick:** plain arm-order round-robin redispatches
  the first project every tick (no memory of who went last). Least-loaded is
  self-correcting without persisted state: the project that won last tick now
  has in-flight ≥1 and sorts after an idle project.
- **One project hogging all slots while others are empty:** that is utilization,
  not starvation; the moment another project gains a ready node, it has 0
  in-flight and sorts first as slots free.
- Stateless + deterministic → directly unit-testable as a pure function, and no
  cursor key to migrate or corrupt.

Rejected: per-project hard caps (waste slack, then re-distribute = round-robin
with extra steps), weighted config (YAGNI, no requirement).

`MAX_THREADS` / `MAX_BACKGROUND_AGENTS` stay **global-only** (resolved brief
question 3).

### 2.5 Hooks (R7) — `juggle_hooks_autopilot.py`

- `_ARMED_CARVEOUT` formats the comma-joined set: `ARMED PROJECTS p1, p2: …`
  (wording otherwise unchanged — nodes of ANY armed project are tick-owned).
- Graph injection: one `build_graph_injection(db, pid, budget=per)` line per
  armed project, where `per = max(160, 500 // len(armed))` — total stays
  bounded near the existing 500-char discipline regardless of N.
- Degrade-to-empty-string on error preserved.

### 2.6 Cockpit (R5) — `juggle_cockpit_graph_dag.py` + graph panel

- `load_graph_dags(conn) -> list[GraphDag]`: parse the CSV key, one `GraphDag`
  per armed project that has nodes (projects without nodes are skipped, as the
  single-project loader does today). `load_graph_dag(conn)` stays as a
  first-or-None compat shim.
- `CockpitState.graph_dag` is joined by `graph_dags: list[GraphDag]` (shim field
  keeps old readers alive one release).
- Graph mode renders DAGs **stacked**, each under a `─ project: <pid> (progress)`
  title rule, reusing the existing per-DAG layout/keys; node selection iterates
  the concatenated node list. No new keybindings.
- Gate: `cockpit --smoke --all-viewports` green with 0, 1, and 3 armed graphs.

### 2.7 Backward compatibility (R6)

- 1-element CSV ≡ today's scalar: no migration, no doctor change.
- `get_armed_project` shim + `ARMED_PROJECT_KEY` re-export keep every existing
  import working.
- Status JSON keeps `armed_project`/`graph` (deprecated) one release.
- Single-armed tick behavior is pinned by the existing `test_graph_dispatch.py`
  suite, which must pass unmodified except where assertions touch the JSON
  compat fields.

## 3. Devil's Advocate

### Assumption-by-assumption challenge

| # | Assumption | What if wrong? | Mitigation |
|---|---|---|---|
| A1 | Project ids never contain commas/whitespace | CSV split corrupts the armed set silently | `arm_project` rejects such ids with ValueError (fail loud); ids are slugs today — this is belt-and-braces |
| A2 | All readers of the settings key are in-repo and updated together | A missed raw reader treats `"a,b"` as one project id | Grep gate in the plan: every literal `autopilot_armed_project` outside `juggle_autopilot_state.py` + the cockpit loader must go through the accessor; pinned test asserts the cockpit raw-SQL path parses CSV |
| A3 | WL's cross-connection-visibility fix is merged | Multi-project dispatch multiplies an existing race's frequency | Plan Task 0 verifies the fix is present on the base before building; if absent, the coder fails the task loudly rather than building on sand |
| A4 | In-flight count is a good fairness proxy | A project with long-running nodes is deprioritized even when it has urgent ready work | Acceptable by design: in-flight work IS budget consumption; "urgency" is not a concept the graph has. Weighted policy can layer on later without storage changes |
| A5 | Capacity break-out-of-whole-pass stays correct | If a future cap became per-project, breaking globally would under-dispatch | Cap is global today (`dbops/threads.py` MAX_THREADS, agent pool); scheduler already ordered fairly at break time, so the partial prefix is fair. Documented in module docstring |
| A6 | Stats dict can stay flat (node-id lists) | A consumer wanting per-project stats has to re-query | Node ids are unique and `list_nodes` recovers the project; extending stats later is additive, not breaking |
| A7 | Old binary + new multi-value DB (rollback) degrades safely | Old code reads `"a,b"` as one id → `get_project` misses → status "no graph loaded", tick dispatches nothing | Degradation is silent-but-safe (notify-only), never corrupting; called out in spec. Accepted: we do not engineer for binary rollback |

### Weakest item + failure mode

**The tick refactor (2.3) is the weakest item.** It rewrites the one loop whose
bugs double-dispatch real agents or strand claims. Specific failure mode: the
per-node disarm guard or the per-node `project_id` thread-binding regresses
under refactor, and a node from project B gets a thread tagged project A —
hydration then pulls the wrong objective and the agent does wrong-repo work.
Mitigation: the scheduler emits `(project_id, node)` pairs so the project
travels WITH the node (no loop-variable capture); a pinned test dispatches a
2-project graph and asserts each created thread's `project_id` matches its
node's; the existing single-project pins must pass unmodified.

### Simpler alternative considered (and why rejected)

"Just call the existing `graph_tick` once per armed project, sequentially."
~10 lines, zero scheduler. Rejected because it fails R3 by construction:
project 1 fills the entire global budget before project 2 is examined, which
is exactly the 50-vs-2 starvation the brief names. Fairness requires a merged
dispatch order, and once you have that, the per-project-tick simplification
buys nothing.

### Edge cases hunted

- **50 ready vs 2 ready (starvation):** solved by interleave — pinned fairness
  test with a capacity-limited FakeDispatch asserts both projects dispatch.
- **Scalar → set migration:** none needed (CSV superset); pinned test arms via
  the OLD code path (`set_setting(KEY, "pid")`) and asserts the new accessor
  returns `["pid"]`.
- **MAX_THREADS global vs per-project:** global-only, resolved (2.4); test
  asserts a cap hit defers nodes from BOTH projects and releases claims.
- **Disarm-one-keep-rest:** pinned test disarms P1 mid-batch via `dispatch_fn`
  side-effect; P1's remaining nodes are skipped, P2's still dispatch.
- **Arm a project with no graph:** tick must skip it without error while still
  dispatching others (empty ready set, no exception).
- **Same node id in two projects:** impossible — `graph_nodes.id` is the PK;
  noted so reviewers don't "fix" it.
- **Per-project recompute crash (R4):** exception in one project's
  sweep/recompute skips only that project — pinned test with a poisoned
  project asserts the healthy one still dispatches.
- **Injection bloat:** budget split (2.5); test asserts total injected graph
  text stays ≤ ~520 chars with 3 armed projects.
- **Cockpit small viewport with 3 DAGs:** viewport smoke matrix is the gate;
  stacked layout clips per existing panel rules.
- **`status` divergence warning:** now fires when set non-empty + flag absent;
  message names all armed projects.

## 4. Open questions

None — all brief questions resolved above (brainstorm doc, "Resolved brief open
questions"). No `--open-questions` batch needed.

## 5. Verification gates (agent-runnable)

- `uv run pytest -q` green (incl. new pins; pre-existing failures documented).
- `uv run src/juggle_cli.py autopilot status --json` over a scripted 2-project
  arm/disarm sequence yields deterministic JSON (exact assertions in plan).
- `uv run src/juggle_cli.py cockpit --smoke --all-viewports` green.
- `uv run src/juggle_cli.py doctor --dry-run` smoke vs tmp DB.
