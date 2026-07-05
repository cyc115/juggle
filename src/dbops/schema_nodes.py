"""dbops.schema_nodes — DDL constants for the unified nodes/node_edges tables (P1).

Owns: CREATE TABLE strings for nodes + node_edges.
Must not own: migration logic, query helpers, or business logic.
"""
from __future__ import annotations

CREATE_NODES = """
CREATE TABLE IF NOT EXISTS nodes (
  -- Identity
  id              TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,

  -- Content
  title           TEXT NOT NULL,
  objective       TEXT NOT NULL DEFAULT '',

  -- State machine
  state           TEXT NOT NULL DEFAULT 'open',

  -- Structural
  project_id      TEXT REFERENCES projects(id),
  parent_id       TEXT REFERENCES nodes(id),

  -- Execution. verify_cmd is task-only and the kind discriminator is enforced
  -- (P8 M2): a non-task node can never carry one. worktree_*/main_repo_path are
  -- NOT constrained — a conversation node legitimately mirrors them from threads.
  verify_cmd      TEXT,
  worktree_path   TEXT,
  worktree_branch TEXT,
  main_repo_path  TEXT,

  -- Completion artifacts (task/research)
  handoff         TEXT,
  diffstat        TEXT,
  verified_at     TEXT,
  merged_sha      TEXT,
  submitted_rev   TEXT,
  pending_merged_sha  TEXT,
  pending_merged_repo TEXT,

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
  updated_at      TEXT NOT NULL,

  -- Parity columns (P8 H4): folded in from migration_nodes_parity (Migration 50)
  -- so a fresh DDL is complete and conv_node_mirror never meets a missing column.
  -- The Migration-50 ALTERs stay as idempotent no-ops for already-migrated DBs.
  -- Placed LAST to match the physical column order ALTER TABLE ADD COLUMN
  -- produces on already-migrated DBs (so `SELECT *` order is provenance-identical).
  user_label              TEXT,
  assigned_by             TEXT NOT NULL DEFAULT 'auto',
  last_active_at          TEXT,
  dispatch_thread_id      TEXT,

  -- Verify-fallback (self-heal): the bounded-retry counter + the prior
  -- verify_cmd failure output injected into the fresh re-dispatch prompt.
  -- Task-only in practice; Migration 57 appends these to already-migrated DBs,
  -- so they are placed LAST (before the CHECK) to keep `SELECT *` column order
  -- provenance-identical between fresh and migrated DBs.
  verify_retries          INTEGER NOT NULL DEFAULT 0,
  verify_failure          TEXT,

  -- Dispatch priority (T-fix-priority-dispatch-ordering): higher sorts ahead in
  -- the ready-dispatch order so fix/defect nodes outrank feature nodes filed
  -- earlier. Task-and-topic in practice; Migration 59 appends it to already-
  -- migrated DBs, so it is placed LAST (before the CHECK) to keep `SELECT *`
  -- column order provenance-identical between fresh and migrated DBs.
  priority                INTEGER NOT NULL DEFAULT 0,

  -- Fail envelope (irl-envelope T2): JSON classification of the most recent
  -- `juggle integrate` refusal for this task (class/reason/attempt/handled_by
  -- — see juggle_integrate_envelope.classify). Task-only in practice;
  -- Migration 63 appends it to already-migrated DBs, so it is placed LAST
  -- (before the CHECK) to keep `SELECT *` column order provenance-identical
  -- between fresh and migrated DBs.
  fail_envelope           TEXT,

  -- Loop-entity V1 (Phase 1): nodes.role (dispatch role, default 'coder') and
  -- nodes.delivery (completion contract, default 'merge') are ADDED BY Migration
  -- 72 — deliberately NOT carried here. Loop-entity V2 (Release 1 / P2):
  -- nodes.model (requested dispatch model, DEFAULT NULL — distinct from
  -- last_dispatched_model above) is ADDED BY Migration 74, same rationale. The
  -- CREATE_NODES DDL is frozen at Migration 63 (fail_envelope): every later column
  -- (66/67/70/71/72/74 + these) is migration-only, so a fresh DB acquires them via
  -- the SAME ALTER-append path as an already-migrated DB (init_db always runs
  -- run_migrations). Adding them here would place them mid-table on fresh DBs but
  -- end-of-table on migrated DBs, diverging `SELECT *` column order. All default
  -- to CURRENT behavior so existing graphs are byte-for-byte unchanged.

  -- Kind discriminator (P8 M2): ONE wide table holds every kind (NOT split
  -- per-kind). This CHECK enforces that verify_cmd — the execution-only column —
  -- is carried ONLY by a kind='task' node, so a conversation/topic/research/
  -- decision node can never be mistaken for an executable task. Existing DBs
  -- acquire it at the terminal-drop table rebuild (SQLite cannot ADD a CHECK via
  -- ALTER); fresh DBs get it here.
  CHECK (kind = 'task' OR verify_cmd IS NULL)
);
"""

# node_edges carries TWO typed relations, discriminated by ``kind`` (P8 M1/Q2):
#   kind='dep'      — task DAG dependency: node_id depends_on depends_on_id (both
#                     kind='task' nodes). ALL dependency traversal filters kind='dep'.
#   kind='dispatch' — the task→agent-thread binding: node_id (a task/topic node) is
#                     dispatched to depends_on_id (a kind='conversation' node). This
#                     replaces the legacy nodes.dispatch_thread_id column (retired in
#                     Migration 53). The agent-thread lookups filter kind='dispatch'.
# Migration 52 adds the column for already-migrated DBs (presence-guarded ALTER);
# it is folded into the DDL here so a fresh table is complete on its own.
CREATE_NODE_EDGES = """
CREATE TABLE IF NOT EXISTS node_edges (
  node_id         TEXT NOT NULL REFERENCES nodes(id),
  depends_on_id   TEXT NOT NULL REFERENCES nodes(id),
  kind            TEXT NOT NULL DEFAULT 'dep',
  PRIMARY KEY (node_id, depends_on_id)
);
"""

CREATE_NODES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes(project_id);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_state   ON nodes(state);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_kind    ON nodes(kind);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_parent  ON nodes(parent_id);",
]
# The partial UNIQUE index idx_nodes_live_label (live-label uniqueness, the
# node-store equivalent of idx_threads_live_label) is created by Migration 54 —
# NOT here — because it must run AFTER that migration reconciles state and repairs
# any duplicate live labels (a UNIQUE index over duplicate rows raises
# IntegrityError, which Migration 44's index loop does not catch). init_db always
# runs migrations, so fresh DBs acquire it via Migration 54 too.
