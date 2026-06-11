# Brainstorm — Multi-Project Parallel Autopilot

**Date:** 2026-06-10 · **Thread:** WM · **Input:** `2026-06-10-multi-project-autopilot-BRIEF.md`
**Mode:** autonomous (planner agent; superpowers:brainstorming run as self-conducted
intent/options exploration — interactive gates skipped per dispatch overrides).

## Intent

One Juggle session, N repos (juggle, lifeos, trading-edge). Each project keeps its
own task graph; the single watchdog tick drives ready nodes across ALL armed graphs
concurrently under one global agent budget. Arming a second project must ADD, not
replace; disarming one must not touch the others.

## Ground-truth verification (brief claims vs. source)

All brief claims confirmed accurate:

| Claim | Verified at |
|---|---|
| Armed project is scalar settings key, arm overwrites | `juggle_cmd_autopilot.py:54` `db.set_setting(ARMED_PROJECT_KEY, project_id)` |
| Tick is single-project | `juggle_graph_dispatch.py:197-199` `armed = get_armed_project(db); if not armed: return` |
| Parallel dispatch within one graph, capped | `juggle_graph_dispatch.py:212` `for node in ready:`; cap via `create_thread` `"Maximum of"` ValueError (`dbops/threads.py:52`) and pool-full `CapacityError` |
| Sweep/recompute/claim scoped to single armed project | `sweep_stale_claims(db, armed)`, `recompute_ready(db, armed)` — both already take `project_id` (loop-ready) |
| Cockpit DAG assumes one armed project | `juggle_cockpit_graph_dag.py:24-65` reads the settings key raw, returns one `GraphDag` |
| Hooks inject single armed project | `juggle_hooks_autopilot.py:44-65` `_armed_graph_context()` formats one project |

Additional facts that shape the design:

- `juggle_graph_dispatch.py` is at 296 lines — already at the 300-line architecture
  gate. Any addition requires extraction first.
- Disarm-mid-batch guard is `get_armed_project(db) != armed` (`dispatch:214`) — must
  become per-project membership (`armed_pid in armed_set`).
- `_dispatch_fails` is keyed `(db_path, node_id)` — node ids are globally unique
  (PK), already multi-project safe.
- `build_graph_injection` has a HARD 500-char budget (`juggle_graph_status.py:17`);
  N projects would inject N×500 chars unless the budget is split.
- Status JSON shape (`armed_project`, `graph`, `diverged`) is consumed by tests and
  possibly scripts — keep deprecated fields one release.
- `test_graph_dispatch.py` has a `FakeDispatch` recorder + tmp-DB fixture — the
  exact harness multi-project tick tests need.

## Option space

### D1 — How to store the armed SET

| Option | Pros | Cons |
|---|---|---|
| **A. CSV in the existing key** (chosen) | Single-element value is byte-identical to today's scalar → zero migration, old DBs "just work"; one key, one authority (preserves DA M6) | Old *binary* reading a multi value sees `"a,b"` as one id (degrades to "no graph loaded", not corruption); ids must not contain commas |
| B. New key `autopilot_armed_projects` (JSON) | Clean name/type | Two keys to keep coherent during transition; needs doctor migration + fallback read everywhere |
| C. Per-project keys `autopilot_armed:<pid>` | Atomic per-project arm/disarm | Enumeration needs LIKE scan; settings API is exact-key get/set |
| D. `projects.armed` column | Relationally honest | Schema migration; splits arming authority away from the settings key (breaks DA M6 invariant) |

**Chosen: A.** The killer property is that backward compatibility is structural, not
migratory. Guard: `arm` rejects project ids containing `,` or whitespace (existing
ids are slugs; this is a fail-loud assert, not a real-world case).

### D2 — How the tick covers N graphs

| Option | Verdict |
|---|---|
| **A. Sequential per-project sweep/recompute, then ONE interleaved dispatch pass** (chosen) | Sweep + recompute are already `project_id`-parameterized — pure loop. Dispatch ordering across projects is where fairness lives, so build a single cross-project ordered list and run the existing claim→thread→dispatch body over it. Capacity is global, so a cap hit breaks the whole pass (claim released, retry next tick) — unchanged semantics. |
| B. Thread-per-project parallel ticks | SQLite write contention, non-deterministic tests, violates "lean, no distributed scheduler". Rejected. |
| C. Independent full `graph_tick` per project, run back-to-back | Simple but fairness-blind: project 1 fills the whole budget before project 2 is even looked at. Rejected — this is exactly the starvation R3 forbids. |

### D3 — Fair-sharing policy for the global budget

| Option | Failure mode |
|---|---|
| Per-project hard cap = budget/N | Wastes slots when a project has fewer ready nodes than its share; re-distributing the slack is just round-robin with extra steps |
| Weighted/priority config | YAGNI; config nobody sets correctly; not in requirements |
| Plain round-robin in arm order | When budget admits only 1 dispatch/tick, the first-armed project wins every tick → starves the rest |
| **Least-loaded-first + round-robin interleave** (chosen) | See spec DA section; no identified starvation mode |

**Chosen policy:** order armed projects by **current in-flight node count ascending**
(states `dispatching|running|integrating`), tie-break by arm order; then take ready
nodes **one per project per round** until ready sets or the global cap are exhausted.

Why it wins: it is stateless (no persisted cursor), deterministic (unit-testable as
a pure function), and self-balancing — a project that got slots last tick has higher
in-flight count this tick, so it sorts later. The 50-vs-2 case: with budget 5,
interleave yields 3+2 — the small graph drains fully. The 1-slot-per-tick case:
P1 dispatched last tick → P1 has 1 in-flight → P2 sorts first this tick. Plain
round-robin fails this without persisted state; least-loaded gets it for free.

### D4 — Cockpit (R5)

Stacked sections in graph mode: `load_graph_dags(conn) -> list[GraphDag]` (one per
armed project that has nodes), panel renders each DAG under a project-titled rule,
reusing the existing per-DAG layout. Rejected tab-cycling (new keybinding + state
for marginal benefit; can add later). Viewport smoke matrix is the gate.

### D5 — Hooks (R7)

Carve-out names the full set; per-project graph injection with the 500-char budget
**split evenly** across armed projects (floor 160 chars each) so total injection
stays bounded regardless of N.

### D6 — CLI surface (R1)

`arm P` adds (idempotent). `disarm [P]` / `off [P]` gain an optional project arg:
with arg → remove just P; without → clear all (current behavior). `off` additionally
clears the global flag only when the set becomes empty. `status` lists every armed
project with its own progress line; JSON gains `armed_projects` + `graphs`, keeps
deprecated `armed_project`/`graph` (first project) for one release.

### Resolved brief open questions

1. **Fair policy** → least-loaded-first + interleave (D3).
2. **Settings migration** → CSV in existing key, no migration (D1).
3. **MAX_THREADS semantics** → global only. Per-project ceilings are YAGNI: a busy
   project consuming all slots while others have nothing ready is utilization, not
   starvation; the moment another project HAS ready nodes, least-loaded ordering
   puts it first as slots free.
4. **Arm with no graph loaded** → unchanged, per-project: arm succeeds and prints
   the decompose-spec instructions for that project; the tick skips projects with
   no nodes (counts None → empty ready set).
5. **50-vs-2 starvation** → solved by construction (D3).

No genuinely unresolvable questions remain → no `--open-questions` batch.
