# Spec: Topic & Project Notebooks

_2026-06-27 · status: DESIGN (approved in brainstorming) · WHAT & WHY, not HOW_

> **Builds on:** [`specs/2026-06-18-unified-topic-graph.md`](2026-06-18-unified-topic-graph.md) (the P8 unified-`nodes` model).
> **Precondition:** P8 collapse fully landed (see §4). This feature is **BLOCKED-ON** P8.

---

## 1. Overview & Why

Give every **topic** a resumable working **notebook** — the engineer's-notebook equivalent for an
agent or the orchestrator. A notebook lets whoever is working a topic:

- **document context** (why this topic exists, what the user actually wants),
- **track sub-tasks** (what's done, in-flight, blocked, left to do), and
- **hand off / resume cleanly** — pick up a topic days later, or across an agent boundary, without
  re-deriving state from scratch.

A **project** gets an aggregated notebook spanning its open topics — one read gives the whole
project's working state.

**Why now / why this shape.** Juggle already stores the two hard parts of a notebook on the graph:
the *objective/intent* lives on the node, and the *sub-task DAG* lives in `nodes` + `node_edges`.
The only thing missing is a place for free-form narrative ("tried X, it failed because Y, next try
Z") and a clean way to *view* all three together. So the notebook is **mostly a render of state that
already exists**, plus exactly one new append-only store for the narrative. This keeps with the house
rule: behaviour lives in deterministic code over one source of truth, built by reusing primitives.

**Relationship to existing primitives (it complements, does not replace):**

| Primitive | What it is | Notebook relationship |
|---|---|---|
| `node.summary` | auto-generated TL;DR of a conversation | Notebook **Context** is human/agent-authored objective + intent, not the auto TL;DR. Distinct. |
| vault `PROJECT.md` | curated, human-facing project doc | Notebook is the **live working state**; PROJECT.md is the durable curated record. Distinct. |
| `agent_runs` ledger | per-dispatch I/O (input prompt ↔ output) | Notebook **Log** is the topic-scoped narrative thread, not per-dispatch I/O. Distinct. |

---

## 2. Core Principle: There Is ONE Type of Topic

There is exactly **one** topic data model. Whether a topic runs on **autopilot** (watchdog tick
dispatches its work) or **non-autopilot** (the orchestrator/live session drives it) changes only
**HOW the topic is executed** — never **what it is**. Both are the same row in the same table.

This is precisely the P8 unified `nodes` model:

- `kind='conversation'` → a **topic** (a workstream / sub-component of a project),
- `kind='task'`         → a **sub-task** of a topic (a child node),
- one shared state machine (`node.state`),
- `project_id` + `parent_id` give the hierarchy.

The notebook is therefore **defined over nodes**, not over any legacy thread/topic/task table. It
never needs to know whether a topic is autopilot or not — it renders the same subtree either way.

---

## 3. Hierarchy & Unified-Model Mapping

```
Project            ← projects table record (project_id)
  └─ Topic         ← node, kind='conversation', project_id=<proj>, parent_id=NULL
       └─ Sub-task ← node, kind='task',         project_id=<proj>, parent_id=<topic id>
            └─ deps ← node_edges(node_id, depends_on_id)  (first-class graph deps)
```

| Concept | Carrier | Notes |
|---|---|---|
| Project | `projects` row | A tag over nodes; NULL `project_id` ⇒ INBOX |
| Topic | `nodes` (kind='conversation') | Anchors a notebook; its subtree = its sub-tasks |
| Sub-task | `nodes` (kind='task', parent_id=topic) | Rendered as a checkbox |
| Sub-task dependency | `node_edges` | `depends_on_id`; *ready* = all deps verified/done |
| Context | `node.objective` + `node.last_user_intent` | Already on the node — no new storage |
| Sub-task status | `node.state` (+ dep readiness) | Single source of truth; rendered, never hand-edited |
| Narrative log | **`node_notes`** (new) | The one new persistent store (§5.1) |

**Checkboxes are NOT a parallel freeform list.** They are a *rendering* of child task-nodes' `state`
and dependency readiness. Agents change a checkbox by mutating the graph through **existing graph
ops** — never by editing a checklist.

---

## 4. Precondition: P8 Collapse (BLOCKED-ON)

**This feature must be sequenced AFTER the P8 collapse fully lands.** Concretely: the legacy
`threads` / `graph_topics` / `graph_tasks` / `graph_edges` tables are collapsed into `nodes` /
`node_edges`, with reads and writes flipped onto the unified tables (per
`specs/2026-06-18-unified-topic-graph.md` §P8 and the P8 research notes under `research/`).

**Why it is a hard dependency, not a nicety:**

- The notebook's entire value is "one resumable view over one source of truth." Pre-collapse there
  are *three* sources (threads, graph_topics, graph_tasks) plus mirror rows (`is_mirror`) and missing
  parity columns on `nodes`. A notebook built now would need dual-read/disambiguation logic that P8
  then deletes — wasted work that also lies about being a single source of truth.
- The render contract walks the subtree by `parent_id` and computes dep-readiness from `node_edges`.
  Both are P8 constructs. Before collapse, sub-task hierarchy lives in `graph_tasks.topic_id` and deps
  in `graph_edges`, which P8 renames/moves.
- Lifecycle hooks (§9) attach to `send-task` / `complete-agent` operating on **node ids**. Pre-P8
  those operate on thread/topic ids with shim translation in flux.

**Consequence (state this loudly):** finishing P8 is on the critical path for this feature. Do not
start notebook implementation until P8's read-collapse + drop have merged and `juggle doctor
--pre-p8-check` reports zero legacy references.

---

## 5. Data Model

### 5.1 `node_notes` — the ONE new table (append-only)

The narrative **Log** is the only state a notebook adds. Everything else (Context, Tasks) is rendered
from existing `nodes` / `node_edges`.

```sql
CREATE TABLE IF NOT EXISTS node_notes (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,   -- monotonic ⇒ stable ordering
  node_id   TEXT NOT NULL REFERENCES nodes(id),  -- the topic (or any node) the note belongs to
  ts        TEXT NOT NULL,                        -- ISO-8601 UTC append time
  who       TEXT NOT NULL,                        -- 'agent' | 'orch' (free text; see §7.2)
  body      TEXT NOT NULL                         -- the narrative line(s)
);
CREATE INDEX IF NOT EXISTS idx_node_notes_node ON node_notes(node_id);
```

**Contract:**

- **Append-only in v1.** No edit, no delete (out of scope — §10). A note, once written, is permanent.
- **Keyed by node id**, which is stable across P8 (ids are preserved 1:1 through the migration).
- **Orderable & deterministic.** The autoincrement `id` defines a total order independent of `ts`
  granularity, so the render is reproducible even for notes written in the same second.
- Mirrors the existing append-only ledger convention (`agent_runs`, `dbops/schema_runs.py`):
  one row per event, monotonic PK, indexed by the owning entity.

### 5.2 Reuse of `nodes` / `node_edges` — no new columns

The notebook adds **no columns** to `nodes`. It reads what P8 already put there:

| Render input | Source (existing) |
|---|---|
| Topic title | `nodes.title` |
| Context: objective | `nodes.objective` |
| Context: latest user intent | `nodes.last_user_intent` |
| Sub-task title | child `nodes.title` (kind='task', parent_id=topic) |
| Sub-task status | child `nodes.state` |
| Sub-task blocked-ness | `node_edges` (a dep not in {verified, done}) |
| Project membership / aggregation | `nodes.project_id` |
| Topic open/closed (for project aggregation) | `nodes.state` (open ⇔ not done/archived) |

---

## 6. The Render Contract (subtree → markdown)

The notebook is a **rendered markdown VIEW of a node's subtree**. The render is a **pure function of
DB state**: given the same `nodes` / `node_edges` / `node_notes` rows, it always produces the same
string. There is no hand-edited source — so there is **no concurrency / clobber risk**.

### 6.1 Sections (exactly three)

| Section | Content | Source |
|---|---|---|
| **Context** | the topic's objective and the latest user intent | `node.objective` + `node.last_user_intent` |
| **Tasks** | child task-nodes as checkboxes (with blocked marker) | child nodes' `state` + `node_edges` |
| **Log** | append-only narrative, oldest→newest | `node_notes` for this node |

### 6.2 Checkbox glyph derivation

Glyph is derived from `node.state` **and** dependency readiness, evaluated **in this order** (first
match wins):

| Order | Condition | Glyph | Meaning |
|---|---|---|---|
| 1 | node has ≥1 dependency whose state ∉ {verified, done}, and node not itself done | `[⊘]` | **blocked** (waiting on deps) |
| 2 | `state ∈ {verified, done}` | `[x]` | complete |
| 3 | `state ∈ {dispatching, running, integrating}` | `[/]` | in progress |
| 4 | `state ∈ {open, ready}` | `[ ]` | pending |

*Ready* (a sub-task whose every dep is verified/done and is itself `open`/`ready`) renders as `[ ]` —
it is pending but unblocked. The `[⊘]` marker is computed from the graph, never stored.

> **Failure states** (`failed-exec`, `failed-integration`, `failed-verify`, `blocked-failed`) are not
> covered by the four glyphs above; see Open Questions (§13) for the proposed `[!]` marker — flagged,
> not assumed, since the approved design named only the four glyphs.

### 6.3 Exact rendered format (this example IS the contract)

```markdown
# Add OAuth login

_node: 4f3c… · kind: conversation · state: open · project: webapp_

## Context

Let users sign in with Google and GitHub OAuth instead of passwords.

Intent: prioritise Google first; GitHub can follow in a later topic.

## Tasks

- [x] Add OAuth provider config
- [/] Implement Google callback handler
- [⊘] Implement GitHub callback handler (waiting on: Add OAuth provider config)
- [ ] Write integration tests

## Log

- 2026-06-27T14:02:11Z · orch: Topic created; scoped to Google-first.
- 2026-06-27T15:30:44Z · agent: Provider config landed; callback handler in progress.
```

Format rules:
- One H1 = topic title; one metadata line (`node · kind · state · project`).
- `Context` renders `objective`, then `last_user_intent` (omitted if empty).
- `Tasks` lists direct child task-nodes; `[⊘]` rows name the blocking dep(s).
- `Log` lists `node_notes` oldest→newest as `- <ts> · <who>: <body>`.
- Empty sections render their header with an explicit empty placeholder (e.g. `_(none yet)_`) so the
  structure is stable and diffable.

### 6.4 The materialized file artifact

The render is also written to a **configurable file path** (default `~/.juggle/notebooks/<node_id>.md`)
so an agent has a stable on-disk location to read.

- The file is **generated, never hand-edited**: it is overwritten wholesale from the current graph +
  `node_notes` on each (re)render. Because it is derived, a regenerate can never clobber human edits
  (there are none) and concurrent regenerates converge to the same content.
- `juggle notebook show` is the canonical renderer; the file is a materialized copy of its output.
- The default directory **should follow the plugin data-dir convention** (`CLAUDE_PLUGIN_DATA`,
  typically `~/.claude/juggle`); the exact default location is flagged in §13.

---

## 7. CLI Surface — `juggle notebook`

Follows house CLI conventions: subcommands, `--json`, args-or-stdin, sane defaults, graceful errors,
`--help`.

### 7.1 `juggle notebook show <node_id|project_id> [--json]`

- **node id** → render that node's subtree notebook (Context / Tasks / Log).
- **project id** → render the aggregated project notebook (§8).
- Disambiguation: if the id resolves to a project, aggregate; if to a node, render the node.
- Default output: the markdown render to stdout. `--json` emits a structured object (§7.3).
- Side effect: refreshes the materialized file (§6.4) for the resolved node(s).

### 7.2 `juggle notebook append <node_id> "<note>" [--who agent|orch]`

- Appends one `node_notes` row to the node's Log. `<note>` may come from an argument or **stdin**.
- `--who` defaults sensibly by caller context (an agent dispatch → `agent`; orchestrator → `orch`).
- Append is the **only** write the notebook CLI performs.

### 7.3 `--json` shapes

`show <node_id> --json`:
```json
{
  "node_id": "4f3c…",
  "kind": "conversation",
  "state": "open",
  "project_id": "webapp",
  "title": "Add OAuth login",
  "context": { "objective": "…", "last_user_intent": "…" },
  "tasks": [
    { "id": "…", "title": "Add OAuth provider config", "state": "verified", "glyph": "x", "blocked_by": [] },
    { "id": "…", "title": "Implement GitHub callback handler", "state": "open", "glyph": "⊘", "blocked_by": ["…"] }
  ],
  "log": [
    { "id": 1, "ts": "2026-06-27T14:02:11Z", "who": "orch", "body": "Topic created; scoped to Google-first." }
  ],
  "markdown": "# Add OAuth login\n…"
}
```

`show <project_id> --json`: `{ "project_id": "...", "name": "...", "topics": [ <per-topic object as above> ] }`.

### 7.4 Task mutations stay on graph ops

There is **no** `notebook add-task` / `notebook check` / `notebook edit-task`. Sub-task creation and
state changes go through the **existing graph operations** (the unified `add-node` / graph mutation
surface). The notebook only ever **reads** task state and **appends** to the Log. This guarantees the
checkbox list cannot drift from the graph — there is no second write path.

---

## 8. Project Notebook = On-Read Aggregation

`juggle notebook show <project_id>` produces the project notebook by **on-read aggregation** — no
materialized project file, no sync job, zero staleness:

1. Resolve the project; emit a project header (name + objective).
2. Find the project's **open** topic nodes (kind='conversation', project_id=this, state not in
   {done, archived}).
3. Render each open topic's section (§6) and concatenate under the project header.

- **Closed/archived topics are excluded** — the project notebook is the *live* working set.
- On-read means it is always consistent with the graph; nothing to invalidate or rebuild.
- INBOX (`project_id` NULL) is a valid project target for aggregation, same as any project tag.

---

## 9. Lifecycle-Hook Behavior (enforcement = code, not prompts)

Enforcement lives in **code + lifecycle hooks**, never prompt-only — prompts can be forgotten; hooks
cannot. Two existing lifecycle points gain notebook behaviour:

### 9.1 `send-task` (dispatch time)

When a task is dispatched to an agent, the hook **automatically**:

1. **Ensures the node exists** for the work being dispatched (no manual notebook setup ceremony).
2. **Materializes / refreshes** the node's notebook file (§6.4).
3. **Injects into the agent prompt**: the node's notebook **path** + the **update protocol** —
   i.e. "your sub-task state is the graph: change it via graph ops; record narrative via
   `juggle notebook append <node_id> "…"`; do **not** hand-edit the notebook file."

This means every dispatched agent receives, deterministically, where its notebook is and how to keep
it current — without relying on the orchestrator remembering to say so.

### 9.2 `complete-agent` (completion time)

When an agent completes, the hook **automatically**:

1. **Appends a Log entry** capturing the result/handoff to the node's `node_notes`.
2. **Warns on left-behind WIP**: if any child sub-task is `[/]` (in progress) and **none** are `[x]`
   (verified/done), the agent claimed completion while leaving work in flight — emit a warning
   (surfaced as an action item / notification, consistent with how `complete-agent` already raises
   handoff/contract issues). This is a *signal*, not a hard refusal, in v1.

---

## 10. Out of Scope (v1 / YAGNI)

- **Cockpit notebook viewer modal** — no TUI panel/modal for notebooks in v1 (CLI render only).
- **Orphan-file GC** — no garbage collection of materialized files for deleted/archived nodes.
- **Auto-deriving `summary` from the notebook** — `node.summary` stays independent (§1).
- **Edit / delete of Log entries** — `node_notes` is strictly append-only in v1.
- **Notebook-driven task mutation** — no task create/check via notebook; graph ops only (§7.4).

---

## 11. Devil's Advocate

| # | Concern | Assessment / Mitigation |
|---|---|---|
| DA1 | **Hard dependency on P8.** Feature cannot start until collapse lands; this makes finishing P8 the critical path. | Accepted and stated loudly (§4). Building pre-P8 would mean dual-read logic P8 deletes — strictly worse. The dependency is a *sequencing* fact, not a design flaw. |
| DA2 | **`node_notes` is too simple (append-only, no edit/delete).** | Intentional. Append-only is the smallest store that delivers the value; edit/delete is real complexity (audit, ordering, concurrency) with no v1 use case. Deferred to §10, not designed-in. |
| DA3 | **Concurrency / clobber on the notebook file.** Two writers regenerating at once. | Eliminated by construction: the file is *generated, never edited* (§6.4). Concurrent regenerates converge to identical content; there is no human-authored content to lose. |
| DA4 | **Does this duplicate `summary` / vault `PROJECT.md` / `agent_runs`?** | No — each is a distinct artifact (§1 table). Notebook = live working state; summary = auto TL;DR; PROJECT.md = curated human doc; agent_runs = per-dispatch I/O. The notebook *complements* them. |
| DA5 | **Checklist drift from the graph.** | Impossible by design: checkboxes are *rendered* from `node.state` + `node_edges`; the notebook has no task write path (§7.4). One source of truth. |
| DA6 | **"Complement the graph_tasks DAG" framing is stale.** | Correct to drop it: once on unified nodes there is no separate `graph_tasks` DAG to complement — it is all one graph. The notebook renders that one graph. |

---

## 12. Agent-First Acceptance Criteria (per component)

Every component is specified so an agent can verify correctness **without a human**. "How does an
agent verify this?" is answered per component below.

| Component | Agent-verifiable acceptance criterion |
|---|---|
| **Render = pure function** | Seed a fixed node subtree in a tmp DB; render twice → byte-identical output. Assert the exact `## Tasks` block and each glyph for the seeded states. Deterministic, no human eyeballing. |
| **Glyph derivation** | Seed children covering each state + a dep-blocked case; `notebook show --json` → each task's `glyph`/`blocked_by` equals the §6.2 table. |
| **`node_notes` append + order** | `notebook append <id> "first"` then `"second"`; `notebook show <id> --json` → `log` has exactly 2 rows in append order (by `id`), correct `who`/`body`. Appending the same body twice yields **two** rows (append-only, not dedup). |
| **CLI `show --json` shape** | `notebook show <node_id> --json` parses to the §7.3 schema (keys present, types correct); `markdown` non-empty. |
| **Project aggregation (on-read)** | Seed a project with one open + one closed topic; `notebook show <project_id> --json` → `topics` includes the open one, excludes the closed one. No materialized project file is written. |
| **Materialized file** | After `notebook show <node_id>`, the file at the configured path exists and its contents equal the stdout markdown. Re-render after a graph change → file reflects new state (regenerated, not appended). |
| **`send-task` hook** | After a dispatch, the dispatched prompt (assert via the `agent_runs.input_prompt` ledger) contains the node's notebook path **and** the update-protocol text; the materialized file exists. |
| **`complete-agent` hook** | After `complete-agent`, `notebook show <id> --json` `log` has a new entry for the result. With children `[/]` and none `[x]`, completion emits the WIP warning (assert via the action-item / notification surface). |

---

## 13. Open Questions / Gaps

- **Failure-state glyph.** The approved design named four glyphs (`[ ] [/] [x] [⊘]`). Sub-tasks in
  `failed-exec` / `failed-integration` / `failed-verify` / `blocked-failed` have no assigned glyph.
  Proposed: a distinct `[!]` "failed" marker (with the failing reason named inline, like `[⊘]` names
  its blocker). Needs confirmation before it becomes canon.
- **Default notebook directory.** The design wrote `~/.juggle/notebooks/<node_id>.md`, but the repo's
  plugin data-dir convention is `CLAUDE_PLUGIN_DATA` (typically `~/.claude/juggle`). Recommend the
  default be `<plugin_data_dir>/notebooks/<node_id>.md` to stay on one volume with the DB; confirm the
  exact default path.
- **`who` vocabulary.** v1 uses free-text `who` with `agent`/`orch` conventions. Whether to constrain
  it (enum) or capture the specific agent/role id (e.g. `agent:coder`) is deferred — flag if richer
  attribution is wanted in v1.
- **Project notebook size.** A large project with many open topics produces a long aggregate. v1
  renders all open topics; whether to cap/paginate is out of scope but noted for future.
