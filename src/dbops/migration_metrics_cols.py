"""dbops.migration_metrics_cols — presence-guarded token/prompt columns on
agent_runs (2026-06-30 orchestration-metrics Task 1). Extracted from
migrations_recent so the general registry stays within its LOC budget."""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)

_METRIC_COLS = [
    ("input_tokens", "INTEGER"), ("output_tokens", "INTEGER"),
    ("cache_read_tokens", "INTEGER"), ("cache_write_tokens", "INTEGER"),
    ("session_id", "TEXT"), ("prompt_fingerprint", "TEXT"),
    ("prompt_version", "TEXT"), ("prompt_bytes", "INTEGER"),
    ("agent_cwd", "TEXT"),
]


def apply_metrics_columns(conn: sqlite3.Connection) -> None:
    """Add the 8 token/prompt columns to agent_runs if absent. Idempotent."""
    try:
        have = {r["name"] for r in conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
        for col, defn in _METRIC_COLS:
            if col not in have:
                conn.execute(f"ALTER TABLE agent_runs ADD COLUMN {col} {defn}")
        conn.commit()
        _log.info("Migration (metrics): agent_runs token/prompt columns added")
    except sqlite3.OperationalError as e:
        _log.warning("Migration (metrics agent_runs cols) skipped: %s", e)
