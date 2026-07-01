"""dbops.schema_runs — durable agent I/O ledger DDL (CREATE_* constants).

Owns: the CREATE TABLE/INDEX strings for the append-only ``agent_runs`` ledger,
which pairs each agent dispatch's INPUT (the full sent prompt) with its OUTPUT
(handoff/result + diffstat), keyed by thread_id (universal) plus
project/topic/task ids. Extracted from dbops.schema to keep both modules within
the 300-line architecture gate. Re-exported from dbops.schema for back-compat.
"""
from __future__ import annotations

# agent_runs (2026-06-13): append-only. One row per dispatch; status walks
# dispatched → completed|failed|superseded. thread_id is the universal key;
# project_id defaults to INBOX (non-project) via the bound thread.
CREATE_AGENT_RUNS = """
CREATE TABLE IF NOT EXISTS agent_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id     TEXT NOT NULL REFERENCES threads(id),
  project_id    TEXT,
  topic_id      TEXT,
  task_id       TEXT,
  agent_id      TEXT,
  role          TEXT,
  model         TEXT,
  harness       TEXT,
  input_prompt  TEXT NOT NULL,
  output        TEXT,
  diffstat      TEXT,
  status        TEXT NOT NULL,
  dispatched_at TEXT NOT NULL,
  completed_at  TEXT,
  repo_path     TEXT,
  vcs_type      TEXT,
  before_sha    TEXT,
  after_sha     TEXT,
  was_dirty     INTEGER,
  input_tokens       INTEGER,
  output_tokens      INTEGER,
  cache_read_tokens  INTEGER,
  cache_write_tokens INTEGER,
  session_id         TEXT,
  prompt_fingerprint TEXT,
  prompt_version     TEXT,
  prompt_bytes       INTEGER,
  agent_cwd          TEXT
);
"""

CREATE_AGENT_RUNS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_thread ON agent_runs(thread_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_project ON agent_runs(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_topic ON agent_runs(topic_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_runs_task ON agent_runs(task_id)",
)
