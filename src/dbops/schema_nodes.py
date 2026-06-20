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

  -- Execution (kind='task' only; NULL for conversation/decision)
  verify_cmd      TEXT,
  worktree_path   TEXT,
  worktree_branch TEXT,
  main_repo_path  TEXT,

  -- Completion artifacts (task/research)
  handoff         TEXT,
  diffstat        TEXT,
  verified_at     TEXT,
  merged_sha      TEXT,

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
"""

CREATE_NODE_EDGES = """
CREATE TABLE IF NOT EXISTS node_edges (
  node_id         TEXT NOT NULL REFERENCES nodes(id),
  depends_on_id   TEXT NOT NULL REFERENCES nodes(id),
  PRIMARY KEY (node_id, depends_on_id)
);
"""

CREATE_NODES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes(project_id);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_state   ON nodes(state);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_kind    ON nodes(kind);",
    "CREATE INDEX IF NOT EXISTS idx_nodes_parent  ON nodes(parent_id);",
]
