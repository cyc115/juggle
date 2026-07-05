"""juggle_smoke_seed — isolated DB seeding for the cockpit viewport smoke
harness (extracted from juggle_smoke to keep that module under the 300-line LOC
gate). Two seeds: the default topic-thread fill (seed_smoke_db) and the armed
wave-DAG fixture that the --smoke-plan Plan-view matrix drives (seed_smoke_graph_db).

The smoke matrix must NEVER touch the shared production DB: in an agent/worktree
context a migration of the shared DB is refused (SharedDBMigrationRefused) and
the cockpit crashes at startup, blanking every viewport. Both seeds build an
isolated, populated DB so the render is deterministic.
"""
from __future__ import annotations

from pathlib import Path


def hermetic_smoke_env(run_dir: str | Path) -> dict[str, str]:
    """Env overlay that makes a child cockpit's smoke run HERMETIC.

    The cockpit's on_mount ensure-exists a DETACHED watchdog. Seeded against a
    temp smoke DB (esp. the --smoke-plan graph fixture) that watchdog behaves as
    a REAL orchestrator: it dispatches the fixture's placeholder nodes as tmux
    coder agents, creating real worktrees/branches off the PROD repo, and writes
    agent_complete/graph_mark_task events to the GLOBAL spool where the PROD
    watchdog drains and dead-letters them (2026-07-04 incident: leaked daemon,
    cyc_AA..cyc_AE orphan branches, "SystemExit code=1" dead-letter storm).

    This overlay closes both seams so a smoke run can never reach production:
      * JUGGLE_WATCHDOG_DISABLE_SPAWN=1 — ``ensure_watchdog`` never launches a
        real detached daemon, so nothing dispatches (no tmux server, no repo
        worktrees, no branches).
      * JUGGLE_SPOOL_DIR — a per-run spool dir honored by both the writer and
        the drain (see ``juggle_spool_paths.spool_dir``), so any spool event
        lands here and can never reach the prod drain.
    """
    spool = Path(run_dir) / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    return {
        "JUGGLE_WATCHDOG_DISABLE_SPAWN": "1",
        "JUGGLE_SPOOL_DIR": str(spool),
    }


def seed_smoke_db(db_path: str, n_threads: int = 30) -> str:
    """Seed an isolated juggle.db with `n_threads` topics so the cockpit body
    panes fill (or check_real_estate flags the mostly-blank layout).

    Temporarily lifts the module-level MAX_THREADS cap (the documented test env
    exports JUGGLE_MAX_THREADS=10) so all are created. Returns `db_path`.
    """
    import dbops.schema as _schema
    import dbops.threads as _threads
    import juggle_db
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    old_max = juggle_db.MAX_THREADS
    new_max = max(old_max, n_threads)
    juggle_db.MAX_THREADS = _schema.MAX_THREADS = _threads.MAX_THREADS = new_max
    try:
        for i in range(n_threads):
            db.create_thread(f"smoke-topic-{i:02d}", session_id="s0")
    finally:
        juggle_db.MAX_THREADS = _schema.MAX_THREADS = _threads.MAX_THREADS = old_max
    _seed_smoke_loops(db)
    return db_path


def _seed_smoke_loops(db, n: int = 8) -> None:
    """Populate the isolated smoke DB with a handful of loops (V2 §4) so the
    Agents-panel Loops band renders across every viewport — including a paused
    loop, a never-run loop, a failing loop, and enough loops to exercise the
    at-scale collapse-to-summary (§8). kind='loop' projects stay out of the
    P-slot headers (snapshot query excludes them)."""
    from dbops.schema import _now

    now = _now()
    with db._connect() as conn:
        for i in range(n):
            pid = f"SLP{i}"
            conn.execute(
                "INSERT INTO projects(id,name,objective,success_criteria,out_of_scope,"
                "status,kind,created_at,last_active) VALUES(?,?,?,?,?,'active','loop',?,?)",
                (pid, f"smoke-loop-{i:02d}", "", "[]", "", now, now),
            )
            status = "paused" if i % 4 == 3 else "active"
            consecutive = i % 3               # 0,1,2 → never/ok/failing mix
            last_run = now if i % 2 == 0 else None
            conn.execute(
                "INSERT INTO loops(id,project_id,thread_id,cadence,status,run_seq,"
                "next_run,last_run_at,consecutive_failures,max_consecutive_failures,"
                "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,3,?,?)",
                (f"SL{i}", pid, None, "every 6h", status, i, now, last_run,
                 consecutive, now, now),
            )
        conn.commit()


def seed_smoke_graph_db(db_path: str) -> str:
    """Seed an isolated DB with ONE armed project whose topic DAG has real
    dependency waves — so pressing g→l during smoke opens a populated Plan view
    (layered future DAG) to validate across viewports. Shape: a ready root, a
    wide fan-out wave, and a convergence tail (the wide fan-in the old railroad
    IndexError'd on)."""
    from datetime import datetime, timezone

    from dbops import db_graph as g
    from juggle_db import JuggleDB
    from juggle_graph_dispatch import ARMED_PROJECT_KEY

    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    now = datetime.now(timezone.utc).isoformat()

    def _dep(conn, node_id, depends_on_id):
        conn.execute(
            "INSERT OR IGNORE INTO node_edges(node_id, depends_on_id, kind) "
            "VALUES(?,?,'dep')", (node_id, depends_on_id),
        )

    with db._connect() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,status,created_at,last_active) "
            "VALUES(?,?,?,?,?)", ("P", "Smoke Plan", "active", now, now),
        )
        conn.commit()
    # 26-wide fan-out: tall enough that the wave-column grid fills even the
    # 130-row portrait profile (each card ~5 rows), so real-estate never flags
    # a mostly-blank layout on a very tall terminal.
    wide_n = 26
    g.create_task(db, task_id="root", project_id="P", title="Ready Root", prompt="root")
    for i in range(wide_n):
        g.create_task(db, task_id=f"w{i}", project_id="P", title=f"Wide {i}", prompt="w")
    for i in range(4):
        g.create_task(db, task_id=f"tail{i}", project_id="P", title=f"Tail {i}", prompt="t")
    with db._connect() as conn:
        for i in range(wide_n):
            _dep(conn, f"w{i}", "root")
        for i in range(4):
            _dep(conn, f"tail{i}", "w0")
            _dep(conn, f"tail{i}", "w1")
        conn.commit()
    db.set_setting(ARMED_PROJECT_KEY, "P")
    return db_path
