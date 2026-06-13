"""dbops.runs — durable agent I/O ledger mixin for JuggleDB.

Owns: insert/close/supersede/fail + query/prune over the append-only
``agent_runs`` table. Each row pairs a dispatch's INPUT (the full sent prompt)
with its OUTPUT (handoff/result + diffstat), keyed by thread_id (universal) plus
project/topic/task ids so the orchestrator can answer "what was the input and
output for any project / topic / task / thread / agent".
Must not own: dispatch/completion glue (lives in the cmd layer), graph/topic
state semantics, or agent-pool CRUD.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunsMixin:
    """Mixin for the append-only agent_runs ledger."""

    # ------------------------------------------------------------------
    # Writers
    # ------------------------------------------------------------------

    def insert_agent_run(
        self,
        thread_id: str,
        input_prompt: str,
        agent_id: str | None,
        role: str | None,
        model: str | None,
        harness: str | None,
        project_id: str | None,
        topic_id: str | None,
        task_id: str | None,
    ) -> int:
        """Insert a new ledger row in status 'dispatched'. Returns run_id."""
        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO agent_runs (thread_id, project_id, topic_id, task_id, "
                "agent_id, role, model, harness, input_prompt, status, dispatched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?, 'dispatched', ?)",
                (thread_id, project_id, topic_id, task_id, agent_id, role, model,
                 harness, input_prompt, now),
            )
            conn.commit()
            return int(cur.lastrowid)

    def close_run(
        self, thread_id: str, output: str | None, diffstat: str | None,
        status: str = "completed",
    ) -> None:
        """Close the NEWEST open ('dispatched') run for thread_id."""
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM agent_runs WHERE thread_id=? AND status='dispatched' "
                "ORDER BY id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            if not row:
                return
            conn.execute(
                "UPDATE agent_runs SET output=?, diffstat=?, status=?, completed_at=? "
                "WHERE id=?",
                (output, diffstat, status, now, row["id"]),
            )
            conn.commit()

    def supersede_open_runs(self, thread_id: str) -> None:
        """Mark any open ('dispatched') runs for thread_id as 'superseded'."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE agent_runs SET status='superseded', completed_at=? "
                "WHERE thread_id=? AND status='dispatched'",
                (_now(), thread_id),
            )
            conn.commit()

    def fail_open_runs(
        self, thread_id: str | None = None, agent_id: str | None = None
    ) -> None:
        """Mark open runs for a thread or agent as 'failed' (watchdog kill path)."""
        if not thread_id and not agent_id:
            return
        col, val = ("thread_id", thread_id) if thread_id else ("agent_id", agent_id)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE agent_runs SET status='failed', completed_at=? "
                f"WHERE {col}=? AND status='dispatched'",
                (_now(), val),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_runs(
        self,
        project_id: str | None = None,
        topic_id: str | None = None,
        task_id: str | None = None,
        thread_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Return runs (newest-first), filtered by any provided key."""
        clauses, params = [], []
        for col, val in (
            ("project_id", project_id), ("topic_id", topic_id),
            ("task_id", task_id), ("thread_id", thread_id),
        ):
            if val is not None:
                clauses.append(f"{col}=?")
                params.append(val)
        sql = "SELECT * FROM agent_runs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_run(self, run_id: int) -> dict | None:
        """Return a single run by id, or None."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_runs WHERE id=?", (run_id,)
            ).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # Prune
    # ------------------------------------------------------------------

    def prune_runs(self, older_than_days: int) -> int:
        """Delete rows dispatched more than older_than_days ago. Returns count."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=older_than_days)
        ).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM agent_runs WHERE dispatched_at < ?", (cutoff,)
            )
            conn.commit()
            return cur.rowcount
