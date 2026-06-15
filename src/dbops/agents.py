"""dbops.agents — Agent pool and watchdog-events mixin for JuggleDB.

Owns: create/get/update/delete agents, agent scoring/ranking, completion
tracking, tool-usage telemetry, watchdog event log.
Must not own: thread CRUD, project ops, notifications, session state.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from dbops.schema import CREATE_AGENT_TOOL_EVENTS


class AgentsMixin:
    """Mixin for agent-pool CRUD, scoring, completions, telemetry, and watchdog events."""

    # ---------------------------------------------------------------
    # Agent CRUD
    # ---------------------------------------------------------------

    def create_agent(
        self,
        role: str,
        pane_id: str,
        harness: str | None = None,
        repo_path: str | None = None,
    ) -> str:
        """Create a new agent record. Returns the agent UUID."""
        new_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agents
                  (id, role, pane_id, assigned_thread, status, context_threads, created_at, last_active, harness, repo_path)
                VALUES (?, ?, ?, NULL, 'idle', '[]', ?, ?, ?, ?)
                """,
                (new_id, role, pane_id, now, now, harness, repo_path),
            )
            conn.commit()
        return new_id

    def get_agent(self, agent_id: str) -> dict | None:
        """Look up an agent by UUID. Returns None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agents WHERE id = ?", (agent_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_all_agents(self) -> list[dict]:
        """Return all agents ordered by creation time."""
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM agents ORDER BY created_at").fetchall()
            return [dict(row) for row in rows]

    def update_agent(self, agent_id: str, **kwargs):
        """Update any column(s) on an agent row. Serializes list values to JSON."""
        if not kwargs:
            return
        serialized = {
            k: json.dumps(v) if isinstance(v, list) else v for k, v in kwargs.items()
        }
        set_clause = ", ".join(f"{col} = ?" for col in serialized)
        values = list(serialized.values()) + [agent_id]
        with self._connect() as conn:
            conn.execute(
                f"UPDATE agents SET {set_clause} WHERE id = ?",
                values,
            )
            conn.commit()

    def delete_agent(self, agent_id: str):
        """Delete an agent record."""
        with self._connect() as conn:
            conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            conn.commit()

    # ---------------------------------------------------------------
    # Agent scoring / ranking
    # ---------------------------------------------------------------

    def get_best_agent(self, thread_id: str, role: str | None = None) -> dict | None:
        """Return the best idle agent for a given thread using scoring.

        Scoring (higher = better):
          +3 if agent is currently assigned to this thread
          +2 if thread_id is in agent's context_threads (has existing context)
          +1 if agent's role matches the requested role

        Ties broken by most recent last_active.
        Returns None if no idle agents exist.
        Only considers agents with status='idle' AND assigned_thread IS NULL.
        """
        idle = [
            a for a in self.get_all_agents()
            if a["status"] == "idle" and a.get("assigned_thread") is None
        ]
        if not idle:
            return None

        def _score(agent: dict) -> tuple:
            context = json.loads(agent.get("context_threads") or "[]")
            s = 0
            if thread_id in context:
                s += 2
            if role and agent["role"] == role:
                s += 1
            return (s, agent["last_active"])

        return max(idle, key=_score)

    def get_ranked_idle_agents(
        self, thread_id: str, role: str | None = None
    ) -> list[dict]:
        """Return all idle agents sorted by best-first scoring.

        Scoring:
          +2 if thread_id is in agent's context_threads
          +1 if agent's role matches the requested role
        Ties broken by most recent last_active.
        Returns empty list if no idle agents exist.
        Only considers agents with status='idle' AND assigned_thread IS NULL.
        """
        idle = [
            a for a in self.get_all_agents()
            if a["status"] == "idle" and a.get("assigned_thread") is None
        ]
        if not idle:
            return []

        def _score(agent: dict) -> tuple:
            context = json.loads(agent.get("context_threads") or "[]")
            s = 0
            if thread_id in context:
                s += 2
            if role and agent["role"] == role:
                s += 1
            return (s, agent["last_active"])

        return sorted(idle, key=_score, reverse=True)

    def cas_assign_agent(self, agent_id: str, thread_id: str) -> bool:
        """Atomically assign an agent to a thread using a guarded UPDATE.

        Only succeeds when the agent row still has status='idle' AND
        assigned_thread IS NULL at commit time.  Returns True on success,
        False if another caller already took the agent (rowcount == 0).
        Callers that receive False must spawn a fresh agent instead.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE agents
                   SET status='busy', assigned_thread=?, last_active=?, busy_since=?
                 WHERE id=? AND status='idle' AND assigned_thread IS NULL
                """,
                (thread_id, now, now, agent_id),
            )
            conn.commit()
        return cur.rowcount > 0

    def get_agent_by_thread(self, thread_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agents WHERE assigned_thread = ? AND status = 'busy' LIMIT 1",
                (thread_id,),
            ).fetchone()
            return dict(row) if row else None

    # ---------------------------------------------------------------
    # Agent completion tracking
    # ---------------------------------------------------------------

    def insert_agent_completion(self, role: str, duration_secs: float) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO agent_completions (role, duration_secs, completed_at) VALUES (?, ?, ?)",
                (role, duration_secs, now),
            )
            conn.commit()

    def get_median_duration_secs(
        self, role: str, days: int = 30, min_samples: int = 10
    ) -> float | None:
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT duration_secs FROM agent_completions "
                "WHERE role = ? AND completed_at > ? ORDER BY duration_secs",
                (role, cutoff),
            ).fetchall()
        vals = [r["duration_secs"] for r in rows]
        if len(vals) < min_samples:
            return None
        mid = len(vals) // 2
        if len(vals) % 2 == 0:
            return (vals[mid - 1] + vals[mid]) / 2.0
        return vals[mid]

    # ---------------------------------------------------------------
    # Tool-usage telemetry
    # ---------------------------------------------------------------

    def record_agent_tool_use(
        self,
        role: str,
        tool_name: str,
        mode: str = "normal",
        last_input: str | None = None,
    ) -> None:
        """Increment the usage counter for (role, tool_name, mode).

        This runs on the agent's PreToolUse critical path (the tool call waits
        for the hook), so it is engineered to NEVER stall an agent:
          * a 250 ms busy-timeout caps any wait when the shared DB's single WAL
            writer is held by another agent/the orchestrator — past that the
            write raises `database is locked`, the caller drops the sample, and
            the tool proceeds. Telemetry is lossy-tolerant; responsiveness wins.
          * `CREATE TABLE` is attempted only if the insert hits a missing table
            (once, on a pre-migration DB) — it stays off the hot path otherwise.
        Upsert keeps the table aggregated (one row per tuple) so volume is O(1).
        """
        now = datetime.now(timezone.utc).isoformat()
        params = (role, tool_name, mode, now, now, last_input)
        insert = (
            "INSERT INTO agent_tool_events "
            "(role, tool_name, mode, count, first_seen, last_seen, last_input) "
            "VALUES (?, ?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(role, tool_name, mode) DO UPDATE SET "
            "count = count + 1, last_seen = excluded.last_seen, "
            "last_input = excluded.last_input"
        )
        conn = sqlite3.connect(str(self.db_path), timeout=0.25)
        try:
            try:
                conn.execute(insert, params)
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    conn.execute(CREATE_AGENT_TOOL_EVENTS)
                    conn.execute(insert, params)
                else:
                    raise  # e.g. "database is locked" → caller drops the sample
            conn.commit()
        finally:
            conn.close()

    def get_agent_tool_usage(self, role: str | None = None) -> list[dict]:
        """Return aggregated tool-usage rows, optionally filtered to one role."""
        with self._connect() as conn:
            conn.execute(CREATE_AGENT_TOOL_EVENTS)
            if role:
                rows = conn.execute(
                    "SELECT role, tool_name, mode, count, first_seen, last_seen, "
                    "last_input FROM agent_tool_events WHERE role = ? "
                    "ORDER BY count DESC, tool_name",
                    (role,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT role, tool_name, mode, count, first_seen, last_seen, "
                    "last_input FROM agent_tool_events ORDER BY role, count DESC, tool_name"
                ).fetchall()
            return [dict(r) for r in rows]

    def reset_agent_tool_usage(self) -> int:
        """Delete all agent_tool_events rows; return number removed."""
        with self._connect() as conn:
            conn.execute(CREATE_AGENT_TOOL_EVENTS)
            cur = conn.execute("DELETE FROM agent_tool_events")
            conn.commit()
            return cur.rowcount

    # ---------------------------------------------------------------
    # Watchdog events
    # ---------------------------------------------------------------

    def add_watchdog_event(
        self,
        agent_id: str,
        thread_id: str | None,
        event_type: str,
        snapshot_path: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO watchdog_events (agent_id, thread_id, event_type, snapshot_path, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (agent_id, thread_id, event_type, snapshot_path, now),
            )
            conn.commit()

    def cleanup_watchdog_events(self, days: int = 30) -> int:
        from datetime import timedelta

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM watchdog_events WHERE created_at < ?", (cutoff,)
            )
            conn.commit()
        return cur.rowcount

    def get_watchdog_events(self, agent_id: str) -> list[dict]:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT * FROM watchdog_events WHERE agent_id=? ORDER BY created_at",
                (agent_id,),
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
