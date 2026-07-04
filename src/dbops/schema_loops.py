"""dbops.schema_loops — DDL constant for the loops table (loop-entity V1, P1).

Owns: the CREATE TABLE string for the loop entity (a recurring, self-scheduling
project driver). Extracted into its own single-purpose module (mirrors
schema_nodes / schema_graph) so dbops.schema stays inside the architecture LOC
gate. Re-exported from dbops.schema for back-compat.

Every column defaults to inert/current behavior:
  - status                   'active'  (a paused loop never fires)
  - run_seq                  0         (id-namespace counter, bumped at fire time)
  - consecutive_failures     0         circuit-breaker counter (Phase 5)
  - max_consecutive_failures 3         breaker threshold (Phase 5)

run_seq + the breaker columns are reserved HERE (Phase 1) per the plan's
open-question #2 so Phase 5 adds no further migration.
"""
from __future__ import annotations

CREATE_LOOPS = """
CREATE TABLE IF NOT EXISTS loops (
  id                       TEXT PRIMARY KEY,
  project_id               TEXT NOT NULL REFERENCES projects(id),
  thread_id                TEXT,
  cadence                  TEXT NOT NULL DEFAULT '',
  status                   TEXT NOT NULL DEFAULT 'active',
  run_seq                  INTEGER NOT NULL DEFAULT 0,
  next_run                 TEXT,
  last_run_at              TEXT,
  consecutive_failures     INTEGER NOT NULL DEFAULT 0,
  max_consecutive_failures INTEGER NOT NULL DEFAULT 3,
  created_at               TEXT NOT NULL,
  updated_at               TEXT NOT NULL
);
"""

CREATE_LOOPS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_loops_status ON loops(status);",
    "CREATE INDEX IF NOT EXISTS idx_loops_project ON loops(project_id);",
]
