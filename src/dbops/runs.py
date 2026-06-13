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
        repo_path: str | None = None,
        vcs_type: str | None = None,
        before_sha: str | None = None,
        was_dirty: bool | None = None,
    ) -> int:
        """Insert a new ledger row in status 'dispatched'. Returns run_id.

        The trailing VCS-provenance kwargs (repo_path/vcs_type/before_sha/
        was_dirty) are captured at the dispatch choke point; they default None so
        non-VCS callers and pre-existing tests are unaffected.
        """
        now = _now()
        dirty = None if was_dirty is None else (1 if was_dirty else 0)
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO agent_runs (thread_id, project_id, topic_id, task_id, "
                "agent_id, role, model, harness, input_prompt, status, dispatched_at, "
                "repo_path, vcs_type, before_sha, was_dirty) "
                "VALUES (?,?,?,?,?,?,?,?,?, 'dispatched', ?,?,?,?,?)",
                (thread_id, project_id, topic_id, task_id, agent_id, role, model,
                 harness, input_prompt, now, repo_path, vcs_type, before_sha, dirty),
            )
            conn.commit()
            return int(cur.lastrowid)

    def close_run(
        self, thread_id: str, output: str | None, diffstat: str | None,
        status: str = "completed",
    ) -> None:
        """Close the NEWEST open ('dispatched') run for thread_id.

        Best-effort captures after_sha = HEAD of the run's repo at completion so
        `juggle runs restore` can report whether the run advanced HEAD. Covers
        cmd_complete AND every mark_graph_* path (all funnel through here)."""
        now = _now()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, repo_path, vcs_type FROM agent_runs WHERE thread_id=? "
                "AND status='dispatched' ORDER BY id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            if not row:
                return
            after_sha = None
            try:
                import vcs as _vcs

                backend = _vcs.get_backend(row["vcs_type"])
                if backend and row["repo_path"]:
                    after_sha = backend.head(row["repo_path"])
            except Exception:  # noqa: BLE001
                after_sha = None
            conn.execute(
                "UPDATE agent_runs SET output=?, diffstat=?, status=?, completed_at=?, "
                "after_sha=? WHERE id=?",
                (output, diffstat, status, now, after_sha, row["id"]),
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
