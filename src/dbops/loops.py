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

    def list_loops(self) -> list[dict]:
        """All loops (active + paused) for the cockpit render band — a paused
        (circuit-broken) loop must still surface, unlike list_active_loops which
        the fire path uses to skip inert loops."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM loops ORDER BY created_at"
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

    def advance_next_run_cas(self, loop_id: str, *, expected, new: str) -> int:
        """Compare-and-swap the loop's ``next_run`` (mirrors dbops.state_write.cas_state).

        ``UPDATE ... WHERE next_run IS <expected>`` — advances the timer to ``new``
        ONLY if it still reads ``expected``. Returns the rowcount (0 = lost race /
        already advanced this window). Phase 5 advances next_run BEFORE instantiating
        the iteration so a crash between fire and advance can never double-fire
        (critique §Axis-7b): the winning tick owns the window; any concurrent/retried
        tick reads the moved next_run and loses the CAS (rowcount 0)."""
        with self._connect() as conn:
            if expected is None:
                cur = conn.execute(
                    "UPDATE loops SET next_run = ?, updated_at = ? "
                    "WHERE id = ? AND next_run IS NULL",
                    (new, _now(), loop_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE loops SET next_run = ?, updated_at = ? "
                    "WHERE id = ? AND next_run = ?",
                    (new, _now(), loop_id, expected),
                )
            conn.commit()
            return cur.rowcount

    def bump_consecutive_failures(self, loop_id: str) -> int:
        """Atomically increment and return the loop's consecutive-failure count
        (UPDATE...RETURNING, same pattern as advance_run_seq). The circuit-breaker
        trips when this reaches ``max_consecutive_failures``."""
        with self._connect() as conn:
            row = conn.execute(
                "UPDATE loops SET consecutive_failures = consecutive_failures + 1, "
                "updated_at = ? WHERE id = ? RETURNING consecutive_failures",
                (_now(), loop_id),
            ).fetchone()
            conn.commit()
            if row is None:
                raise ValueError(f"loop not found: {loop_id!r}")
            return row[0]

    def reset_consecutive_failures(self, loop_id: str) -> None:
        """Zero the consecutive-failure counter (a good iteration clears it — the
        breaker trips on CONSECUTIVE failures only)."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE loops SET consecutive_failures = 0, updated_at = ? WHERE id = ?",
                (_now(), loop_id),
            )
            conn.commit()

    def pause_loop(self, loop_id: str) -> None:
        """Set status='paused' (circuit-breaker trip). A paused loop is inert —
        list_active_loops excludes it, so fire_due_loops never fires it again until
        the orchestrator resumes it."""
        self.set_loop_status(loop_id, "paused")

    def delete_loop(self, loop_id: str) -> None:
        """Hard-remove the loop row (``schedule:delete --purge``). Non-recoverable:
        the SOFT delete path (``pause_loop`` + project-close) keeps the row so the
        loop can be restored; purge destroys the loop entity outright."""
        with self._connect() as conn:
            conn.execute("DELETE FROM loops WHERE id = ?", (loop_id,))
            conn.commit()

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


def live_generation_like(conn, project_id: str) -> str | None:
    """LIKE-pattern scoping a LOOP project's task-node reads to its CURRENT run_seq
    generation (``<loop_id>-r<seq>-%``), or ``None`` when ``project_id`` is not a
    loop project (loop-entity V2 §7.2, 2026-07-05).

    A loop's ONE stable topic accumulates a new run-namespaced task generation per
    fire, so every loop-progress rollup must scope to the live generation — the same
    key ``iteration_outcome`` uses (``juggle_loop_fire``) — else it reads the whole
    accumulated history (the ``168/170`` noise). Pure read over the caller's ``conn``;
    fail-soft to ``None`` on a pre-migration DB (no ``loops`` table) so non-loop and
    legacy callers keep today's unscoped behaviour.
    """
    try:
        row = conn.execute(
            "SELECT id, run_seq FROM loops WHERE project_id = ?", (project_id,)
        ).fetchone()
    except Exception:
        return None
    if row is None:
        return None
    return f"{row[0]}-r{row[1]}-%"
