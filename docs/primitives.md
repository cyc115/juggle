# Juggle autopilot primitives

The project-autopilot plan store is a three-tier hierarchy:

```
Project ──< Topic ──< Task
```

- **Project** (`projects`) — a long-lived body of work with an objective and
  out-of-scope definition.
- **Topic** (`graph_topics`) — one unit of agent work: ONE thread / agent /
  worktree, integrated once. A topic owns a DAG of tasks.
- **Task** (`graph_tasks`) — the atomic plan unit. Tasks hold the plan (prompt +
  `verify_cmd`); "done" is `state='verified'` + `verified_at`, never a thread
  status. Dependencies live in `graph_edges` (`task_id` → `depends_on_id`).

`task_transition` is the SOLE writer of task state (deterministic state machine).

## Naming: `node` → `task` (2026-06-13)

The plan primitive was originally called **node** (graph theory). It was renamed
to **task** so the name reflects its function. The rename is full, not cosmetic:

| Old | New |
| --- | --- |
| `graph_nodes` table | `graph_tasks` |
| `node_id` column (`graph_edges`, `agent_runs`) | `task_id` |
| `add-node` CLI | `add-task` |
| `--force-node` flag | `--force-task` |
| `create_node`, `get_node`, `node_transition`, … | `create_task`, `get_task`, `task_transition`, … |
| `GraphNode` | `GraphTask` |

### Migration

`Migration 38` (`dbops.migrations_graph._migrate_node_to_task`) renames the table
and the `node_id` columns on `graph_edges` and `agent_runs` in ONE idempotent,
backward-compatible step. It runs BEFORE the `CREATE TABLE IF NOT EXISTS`, and is
self-healing: because `init_db` creates an empty `graph_tasks` before migrations
run, a node-era DB reaching the migration with BOTH tables present is reconciled
(empty shell dropped + populated table renamed, or rows merged) rather than
stranding rows. `juggle doctor` detects a lingering `graph_nodes` table and runs
the migration on existing installs.

### Deprecated aliases (DO NOT REMOVE)

`add-node` and `--force-node` remain as hidden aliases of `add-task` /
`--force-task`. They are baked into the autopilot `UserPromptSubmit` hook,
`commands/*.md`, and the global `CLAUDE.md` — breaking them detonates autopilot.
