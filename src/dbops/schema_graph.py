"""dbops.schema_graph — autopilot graph/topic store DDL (CREATE_* constants).

Owns: the CREATE TABLE strings for the project-autopilot plan store
(graph_tasks / graph_edges / graph_topics). Extracted from dbops.schema to
keep both modules within the 300-line architecture gate. Re-exported from
dbops.schema for back-compat, so ``from dbops.schema import CREATE_GRAPH_TASKS``
keeps working.
"""
from __future__ import annotations

# Autopilot plan store (2026-06-10 rev 2): tasks hold the PLAN; done is
# state='verified' + verified_at, never thread.status; edges reference tasks.
CREATE_GRAPH_TASKS = """
CREATE TABLE IF NOT EXISTS graph_tasks (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES projects(id),
  title       TEXT NOT NULL,
  prompt      TEXT NOT NULL,
  verify_cmd  TEXT,
  state       TEXT NOT NULL DEFAULT 'pending',
  thread_id   TEXT,
  handoff     TEXT,
  diffstat    TEXT,
  verified_at TEXT,
  created_at  TEXT NOT NULL, updated_at TEXT NOT NULL);
"""
CREATE_GRAPH_EDGES = """
CREATE TABLE IF NOT EXISTS graph_edges (
  task_id       TEXT NOT NULL REFERENCES graph_tasks(id),
  depends_on_id TEXT NOT NULL REFERENCES graph_tasks(id),
  PRIMARY KEY (task_id, depends_on_id));
"""

# 3-tier hierarchy (R9, 2026-06-11): a Topic owns a task-DAG; ONE thread/agent/
# worktree per topic; integrate runs once per topic. Topics reuse the task
# state machine (dbops.db_topics imports db_graph._TRANSITIONS).
CREATE_GRAPH_TOPICS = """
CREATE TABLE IF NOT EXISTS graph_topics (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL REFERENCES projects(id),
  title       TEXT NOT NULL,
  objective   TEXT NOT NULL DEFAULT '',
  state       TEXT NOT NULL DEFAULT 'pending',
  thread_id   TEXT,
  handoff     TEXT,
  diffstat    TEXT,
  verified_at TEXT,
  merged_sha  TEXT,
  created_at  TEXT NOT NULL, updated_at TEXT NOT NULL);
"""
