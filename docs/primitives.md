# Juggle Project-Management Primitives

> Defines the four PM primitives — **Project**, **Topic**, **Node (Task)**, **Thread** —
> their relationships, and how their states are managed. Companion to
> [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`topic-lifecycle.md`](topic-lifecycle.md).

Source of truth for state logic: `src/dbops/db_graph.py` (nodes — the *only* state
writer is `node_transition`) and `src/dbops/db_topics.py` (topics — reuses the **same**
`_TRANSITIONS` table: one state machine, two tables).

---

## 1. The primitives

| Primitive | Table | What it is | Granularity |
|-----------|-------|-----------|-------------|
| **Project** | `projects` | A long-running objective with success criteria + a match profile. Top-level container. | Coarsest — spans weeks |
| **Topic** | `graph_topics` | A workstream *within* a project that **owns a task-DAG**. The unit the autopilot watchdog claims, dispatches, and integrates as a whole. | Mid — one cohesive deliverable |
| **Node** *(= Task)* | `graph_nodes` (+ `graph_edges`) | A single task: a dispatch prompt, optional `verify_cmd`, dependency edges, and the `handoff` it produces on completion. | Finest — one agent dispatch |
| **Thread** | `threads` | The conversation/agent execution bound to a node (or a loose one-off). Carries `project_id` (default `INBOX`). | Per execution |

**"Task"** is the colloquial name for the leaf:
- inside a project → a **Node**;
- outside a project → a bare **Thread** (`project_id='INBOX'`), no DAG, no topic.

---

## 2. Relationships

```
Project  (objective + success criteria)
  └─ Topic  (owns a DAG)              ← watchdog tick's unit of claim/dispatch/integrate
       ├─ Node = Task                 ← one agent dispatch
       │    ├─ graph_edges → upstream deps (N:N)
       │    ├─ thread_id  → the executing Thread
       │    └─ handoff    → OUTPUT, feeds dependents' hydration
       └─ …
```

- **Project → Topic:** 1‑to‑N. A project is the *goal*; a topic is an *executable
  workstream*. Project "done" = success criteria met; topic "done" = `state='verified'`.
- **Topic → Node:** 1‑to‑N. The topic owns the dependency graph; nodes are its vertices.
- **Node → Node:** N‑to‑N via `graph_edges` (a node's deps are its upstream nodes).
- **Topic → Topic deps are DERIVED,** never authored: a cross-topic node edge induces a
  dep between the two owning topics (`db_topics.topic_ready_eligible`).
- **Node → Thread:** a node binds to the thread executing it (`graph_nodes.thread_id`);
  reverse lookup via `db_graph.get_node_by_thread`.
- **Node → handoff:** the durable OUTPUT (`graph_nodes.handoff` + `diffstat`),
  re-hydrated into each dependent's dispatch prompt via `build_hydration`
  (never `thread.summary`).
- **agent_runs ledger** (I/O history) keys every dispatch by `thread_id`, then resolves
  `project_id` / `topic_id` / `node_id` — so project *and* non-project work pair INPUT
  with OUTPUT uniformly.

---

## 3. Node state machine

`node_transition` (in `db_graph.py`) is the **only** writer of `graph_nodes.state`.
Every transition is `(current_state, event) → next_state`; anything else **fails loud**.

```
pending → ready → dispatching → running → integrating → verified
                                   │
                                   └─ exec_fail → failed-exec
```

### States

| State | Meaning |
|-------|---------|
| `pending` | Created; deps not all verified yet. **Operator territory.** |
| `ready` | All upstream deps `verified`; eligible to claim. **Tick-owned.** |
| `dispatching` | Claimed (atomic CAS), being sent to an agent. **Tick-owned, protected.** |
| `running` | Agent is executing. **Tick-owned, protected.** |
| `integrating` | Agent reported; integration (merge/verify) in progress. **Tick-owned, protected.** |
| `verified` | Integrated + verified. **Terminal success. `verified` ⇒ MERGED.** |
| `failed-exec` | Agent died / gave up. Operator territory. |
| `failed-integration` | Integration failed. Operator territory. |
| `failed-verify` | `verify_cmd` failed. Operator territory. |
| `blocked-failed` | A dependency failed; this node can't run. Operator territory. |

### Transition table (authoritative)

| From | Event | To |
|------|-------|----|
| `pending` | `deps_ready` | `ready` |
| `pending` | `dep_fail` | `blocked-failed` |
| `pending` | `reload` | `pending` |
| `ready` | `claim` | `dispatching` |
| `ready` | `dep_fail` | `blocked-failed` |
| `ready` | `unready` | `pending` *(`add-node --required-by` demotes a ready node)* |
| `ready` | `reload` | `pending` |
| `dispatching` | `dispatch` | `running` |
| `dispatching` | `stale_reset` | `ready` *(watchdog reclaim of a stuck claim)* |
| `running` | `integrate_start` | `integrating` |
| `running` | `exec_fail` | `failed-exec` |
| `integrating` | `integrate_ok` | `verified` |
| `failed-exec` / `failed-integration` / `failed-verify` | `reload` | `pending` |
| `blocked-failed` | `reload` | `pending` *(lets a blocked tail resume after a spec reload)* |

### Two guard sets

- **`PROTECTED_STATES` = {`dispatching`, `running`, `integrating`, `verified`}** — a
  guarded spec re-load (`project-graph load`) will **not** modify a node in these states.
- **`TICK_OWNED` = {`ready`, `dispatching`, `running`, `integrating`, `verified`}** — a
  thread bound to a node in one of these is the watchdog's territory. A manual
  `send-task` is **refused without `--force-node`**. `pending` / `failed-*` /
  `blocked-failed` remain **operator territory** (you may edit/redispatch them).

> **Readiness rule:** a node becomes `ready` only when **all** its dep nodes are
> `verified` (`db_graph` ready-set query). Promotion is CAS — a lost race is a silent
> no-op, never a double-dispatch.

---

## 4. Topic state machine

Topics **reuse the node `_TRANSITIONS` table** — one state machine, two tables. The
lifecycle mirrors nodes: `pending → ready → … → verified`, with `failed-exec` for
agent death / give-up (`mark_topic_exec_failed`).

- **Topic readiness is DERIVED:** `topic_ready_eligible` returns `pending` topics whose
  derived dep-topics are **all** `verified` (a dep-topic = a topic on the other end of a
  cross-topic node edge). `recompute_topic_ready` CAS-promotes `pending → ready`.
- **`verified` ⇒ MERGED at topic level** (spec §2.3). `mark_completion` is idempotent on
  the success path: an already-`verified` topic returns `verified` without raising, so a
  node never gets stuck at `running` on a double-integrate.
- **Topic state stays in sync with member-node states** (B1+B3): a topic's state is kept
  consistent with the states of the nodes it owns.

---

## 5. Project & Thread state

**Project** (`projects`): coarse status — `active`, plus `summary` / `closed_at`
(migration 29) when wound down. A project is "done" when its **success criteria** are
met, which is judged at the project level, not derived from a single topic.

**Project arming** (`autopilot`): an *armed* project's topics are **tick-owned** — the
watchdog claims, dispatches, and integrates them. NEW work for an armed project must be
filed as a task (`juggle graph add-node … --topic <t>`), **not** a manual `send-task`
(code-enforced; refused without `--force-node`). Integration runs once per topic.

**Thread** (`threads`): carries `project_id` (default `INBOX` for non-project work).
A thread's *conversation status* is **independent** of node state — "done" is
`node.state='verified'`, **never** a thread status. Context propagates through the
durable `handoff` + `diffstat`, re-hydrated by `build_hydration` — never via
`thread.summary`.

---

## 6. One-paragraph summary

A **Project** is the objective. It contains **Topics**, each owning a **task-DAG** of
**Nodes** (a node = a task = one agent dispatch). Node edges form the DAG; topic-level
deps are *derived* from edges that cross topic boundaries. Nodes advance through a strict
`pending → ready → dispatching → running → integrating → verified` machine — written
only by `node_transition`, failing loud on any illegal `(state, event)` — and topics ride
the *same* machine over a *derived* dep graph. `verified` means MERGED. The watchdog owns
everything `ready`-and-beyond on armed projects; operators own `pending` and the
`failed-*` tail. Execution lives in **Threads**, whose output is captured durably as a
node's `handoff` (+ `diffstat`) and re-hydrated into downstream dispatches.
