"""dbops.loops — Loop entity CRUD + atomic run-seq mixin (loop-entity V1, P1).

Owns: create/get/list/status/stamp on the ``loops`` table and the atomic
``advance_run_seq`` counter (the id-namespace that keeps a re-fired iteration's
node ids from colliding with the guarded-upsert refusal).
Must not own: firing/scheduling logic (Phase 5), template validation (Phase 4).

Mirrors dbops.projects.ProjectsMixin shape (label-wheel allocation, thin
per-method connections). advance_run_seq mirrors migration_seq.reserve_next's
single-statement UPDATE...RETURNING under SQLite's own file lock — safe across
concurrent connections/processes against the same DB file.
"""
from __future__ import annotations

from dbops.schema import _now


class LoopsMixin:
    """Mixin for loop-entity CRUD and the atomic run-seq counter."""

    def _next_loop_label(self, used: set) -> str:
        i = 1
        while True:
            label = f"L{i}"
            if label not in used:
                return label
            i += 1

    def create_loop(
        self,
        project_id: str,
        cadence: str = "",
        *,
        next_run: str | None = None,
        thread_id: str | None = None,
        max_consecutive_failures: int = 3,
    ) -> str:
        """Insert a new loop (status='active', run_seq=0) and return its L-label."""
        with self._connect() as conn:
            used = {r[0] for r in conn.execute("SELECT id FROM loops").fetchall()}
            loop_id = self._next_loop_label(used)
            now = _now()
            conn.execute(
                "INSERT INTO loops (id, project_id, thread_id, cadence, status, "
                "run_seq, next_run, last_run_at, consecutive_failures, "
                "max_consecutive_failures, created_at, updated_at) "
                "VALUES (?,?,?,?,'active',0,?,NULL,0,?,?,?)",
                (loop_id, project_id, thread_id, cadence, next_run,
                 max_consecutive_failures, now, now),
            )
            conn.commit()
        return loop_id

    def get_loop(self, loop_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM loops WHERE id = ?", (loop_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_active_loops(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM loops WHERE status = 'active' ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def advance_run_seq(self, loop_id: str) -> int:
        """Atomically bump and return the loop's run_seq (strictly increasing).

        Single-statement UPDATE...RETURNING under SQLite's file lock — safe
        across concurrent callers (mirrors migration_seq.reserve_next). Returns
        the NEW value; the caller namespaces iteration node ids as
        ``<L#>-r<run_seq>-<topic>`` so re-fires never collide with the
        guarded-upsert refusal (juggle_graph_load.py)."""
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE loops SET run_seq = run_seq + 1, updated_at = ? "
                "WHERE id = ? RETURNING run_seq",
                (_now(), loop_id),
            ).fetchone()
            conn.commit()
            if row is None:
                raise ValueError(f"loop not found: {loop_id!r}")
            return row[0]

    def set_loop_status(self, loop_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE loops SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), loop_id),
            )
            conn.commit()

    def stamp_last_run(self, loop_id: str, ts: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE loops SET last_run_at = ?, updated_at = ? WHERE id = ?",
                (ts or _now(), _now(), loop_id),
            )
            conn.commit()
