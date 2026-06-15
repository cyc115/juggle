# Graph Mirrors Threads — Option 1 Implementation Plan

**Date:** 2026-06-14  
**Branch:** cyc_ZO  
**Status:** Approved for implementation

## Problem

Juggle's graph panel shows only explicitly created autopilot tasks. Threads assigned to a project have no presence in the graph, making project progress opaque — the graph and the thread list are disconnected views.

## Solution: Option 1 — Graph Mirrors Threads

Every project's graph ALWAYS EXISTS and reflects ALL its threads as TRACKER nodes (`is_mirror=1` in `graph_topics`). Mirror nodes are purely projections — they never change dispatch behavior. The graph becomes the single source of truth for project-level thread visibility.

## Schema

**Migration 42:** Add `is_mirror INTEGER NOT NULL DEFAULT 0` to `graph_topics`.

- Additive, idempotent (ALTER TABLE ADD COLUMN, caught on duplicate).
- Existing rows: SQLite default fills `is_mirror=0`.

## New Module: `src/dbops/db_mirror.py`

### `mirror_upsert_thread(db, thread_id, project_id) → str`
- Creates or updates a mirror topic for `thread_id` in `project_id`.
- `is_mirror=1`, title = thread.topic, state mirrors thread lifecycle.
- Thread status → mirror topic state:
  - `active` → `running`
  - `idle` → `pending`
  - `done` → `verified`
  - `archived` → call `mirror_delete_thread` (shouldn't reach here normally)
- Writes state directly via SQL (no `topic_transition` — bypasses G1/state machine).
- Single-writer: delete-before-insert on project reassignment (prevents orphan mirrors).
- Mirror topic ID: `~{thread_id}` (deterministic, traceable).

### `mirror_delete_thread(db, thread_id) → None`
- Removes all `is_mirror=1` rows for `thread_id` from `graph_topics`.

### `backfill_mirror_topics(db) → int`
- Idempotent: creates mirror topics for all non-archived threads with a non-INBOX project.
- Returns count of threads processed.
- G2-safe: caller must be orchestrator. Does NOT migrate the shared prod DB.

### `reconcile(db, project_id) → dict`
- Sync mirror topics for one project: upsert missing, delete for archived threads.

## Guard Bypass: `check_task_guard` in `juggle_cmd_agents_graph.py`

A mirror topic (`is_mirror=1`) is a TRACKER, not a work item. `check_task_guard` must bypass (return None) for mirror topics in BOTH branches:

1. **Tick-owned branch**: `if bound.get("is_mirror"): return None` — before the tick-owned state check.
2. **Armed-project branch**: implicit — a thread with a mirror topic is always `bound`, so the armed-project branch is never reached.

## `topic_ready_eligible` Excludes `is_mirror=1`

Add `AND COALESCE(t.is_mirror, 0) = 0` to the WHERE clause in `db_topics.topic_ready_eligible`. The watchdog tick NEVER claims/dispatches mirror topics.

## `topic_counts` Excludes `is_mirror=1`

Same filter in `db_topics.topic_counts`. Ensures the "14/14 done" tally counts only real work topics (P2 invariant preserved).

## Lifecycle Hooks

| Event | Action |
|-------|--------|
| Thread assigned to project (auto via `assign_project_background`) | `mirror_upsert_thread(db, thread_id, project_id)` |
| Thread assigned to project (human via `_assign_thread_to_project`) | `mirror_upsert_thread(db, thread_id, project_id)` |
| Thread re-assigned (project change) | `mirror_upsert_thread` delete-before-insert handles orphan prevention |
| Thread archived (`cmd_archive_thread`) | `mirror_delete_thread(db, thread_id)` |
| Thread assigned to INBOX | `mirror_delete_thread(db, thread_id)` |

## Project Create

After `db.create_project`, call `backfill_mirror_topics` for the new project (no-op for empty project, but activates mirror machinery).

## Doctor Integration

`cmd_doctor` calls `backfill_mirror_topics(db)` (orchestrator-only, G2-safe) to create mirror topics for all existing project-assigned threads that predate this feature.

## Cockpit Changes

**Scope: GraphDag / progress-count / mirror-cell rendering ONLY. Do NOT touch the status bar.**

### `juggle_cockpit_graph_layout.py`
Add `is_mirror: bool = False` to `GraphTask`.

### `juggle_cockpit_graph_dag.py`
- Query `is_mirror` from `graph_topics`.
- Pass `is_mirror=bool(r["is_mirror"])` to `GraphTask` constructor.

### `juggle_cockpit_graph_panel.py`
- **Progress counts**: exclude mirror tasks (`is_mirror=True`) from `counts_from_states` and `_progress_bar`.
- **Mirror cell rendering**: in `_cell_text`, if `task.is_mirror`, apply `Style(dim=True)`.

## DA Resolutions

- **P2 14/14 tally preserved**: `topic_counts` and progress bars filter `is_mirror=0` only.
- **Backfill race mitigated**: strict deploy order — migration → backfill at doctor startup.
- **Junk/auto-classifier threads**: each spawns a dimmed mirror node (accepted as known noise).
- **Project-reassign orphan prevention**: single-writer delete-before-insert in `mirror_upsert_thread`.

## Critical Constraints

- Do NOT run migration 42 against `~/.claude/juggle/juggle.db` from tests. Tests use temp DBs.
- Mirror nodes must be PURELY a projection — guard bypass + ready-exclusion guarantee dispatch is unchanged.
- Cockpit: do NOT touch the status bar (concurrent thread ZK owns it).
