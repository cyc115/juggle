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
    return db_path


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
