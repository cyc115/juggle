"""Per-agent-session counter for the verify-loop backstop (TODO L13).

Backs the harness cap on repeated BACKGROUND full-suite/verify spawns. Kept in
its own focused store (the split-schema convention — cf. schema_runs/schema_nodes)
rather than the at-ceiling dbops/agents.py.

Mirrors ``record_agent_tool_use``: the bump runs on the agent's PreToolUse
critical path, so it uses a 250 ms busy-timeout (never stalls the agent) and
lazily ``CREATE TABLE`` only when an insert hits a missing table on a
pre-migration DB. The counter only grows within one agent's session — a fresh
agent gets a new ``session_id`` and starts at 1.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

CREATE_AGENT_VERIFY_SPAWNS = """
CREATE TABLE IF NOT EXISTS agent_verify_spawns (
  session_id  TEXT PRIMARY KEY,
  count       INTEGER NOT NULL DEFAULT 1,
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL
);
"""


def bump_verify_spawn(db_path, session_id: str) -> int:
    """Increment and return the background suite-spawn count for ``session_id``.

    Returns the NEW count (1 on the first spawn). Fail-loud on real errors, but
    a ``database is locked`` (busy-timeout exceeded) propagates so the caller can
    fail OPEN — telemetry/responsiveness wins over a perfect count.
    """
    now = datetime.now(timezone.utc).isoformat()
    insert = (
        "INSERT INTO agent_verify_spawns (session_id, count, first_seen, last_seen) "
        "VALUES (?, 1, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "count = count + 1, last_seen = excluded.last_seen"
    )
    conn = sqlite3.connect(str(db_path), timeout=0.25)
    try:
        try:
            conn.execute(insert, (session_id, now, now))
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                conn.execute(CREATE_AGENT_VERIFY_SPAWNS)
                conn.execute(insert, (session_id, now, now))
            else:
                raise  # e.g. "database is locked" → caller fails open
        conn.commit()
        row = conn.execute(
            "SELECT count FROM agent_verify_spawns WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    finally:
        conn.close()
