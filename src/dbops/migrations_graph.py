"""dbops.migrations_graph — autopilot graph/topic store migrations (35-37, 39).

Owns: schema evolution for the project-autopilot plan store
(graph_tasks / graph_edges / graph_topics). Extracted from
dbops.migrations_recent to keep both modules within the 300-line architecture
gate. Called by ``dbops.migrations_recent.apply_recent_migrations``.
Must not own: query or business logic — only schema evolution.
"""

from __future__ import annotations

import logging
import sqlite3

from dbops.schema_graph import (
    CREATE_GRAPH_EDGES,
    CREATE_GRAPH_TASKS,
    CREATE_GRAPH_TOPICS,
)

_log = logging.getLogger(__name__)

# RENAME COLUMN landed in SQLite 3.25.0 (2018). Older engines need a rebuild.
_HAS_RENAME_COLUMN = sqlite3.sqlite_version_info >= (3, 25, 0)


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _drop_old_task_indexes(conn: sqlite3.Connection) -> None:
    # Stale indexes follow a renamed table but keep the old name; drop so
    # migration 35 can recreate them under idx_graph_tasks_*.
    for idx in (
        "idx_graph_nodes_project_state",
        "idx_graph_nodes_thread",
        "idx_graph_nodes_topic",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {idx}")


def _merge_nodes_into_tasks(conn: sqlite3.Connection) -> None:
    """Both graph_nodes and graph_tasks exist (init_db creates an empty
    graph_tasks before migrations run). Reconcile WITHOUT losing rows: the
    two tables share column NAMES but the historical column ORDER may differ,
    so copy by explicit name intersection rather than ``SELECT *``."""
    n_nodes = conn.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0]
    n_tasks = conn.execute("SELECT COUNT(*) FROM graph_tasks").fetchone()[0]
    if n_nodes and not n_tasks:
        conn.execute("DROP TABLE graph_tasks")  # empty shell — rename the real one
        conn.execute("ALTER TABLE graph_nodes RENAME TO graph_tasks")
        _drop_old_task_indexes(conn)
    elif n_nodes:
        shared = sorted(_cols(conn, "graph_nodes") & _cols(conn, "graph_tasks"))
        cols = ", ".join(shared)
        conn.execute(
            f"INSERT OR IGNORE INTO graph_tasks ({cols}) SELECT {cols} FROM graph_nodes"
        )
        conn.execute("DROP TABLE graph_nodes")
    else:
        conn.execute("DROP TABLE graph_nodes")  # stale empty leftover


def _rename_column(conn: sqlite3.Connection, table: str, old: str, new: str) -> None:
    """Rename ``old`` -> ``new`` on ``table``; rebuild for SQLite < 3.25.

    Idempotent: a no-op when ``old`` is absent (already renamed)."""
    cols = _cols(conn, table)
    if old not in cols or new in cols:
        return
    if _HAS_RENAME_COLUMN:
        conn.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")
        return
    # Fallback: rebuild the table copying every column, renaming the one.
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    src_names = [r[1] for r in info]
    dst_defs = []
    for r in info:
        name = new if r[1] == old else r[1]
        coltype = r[2] or ""
        notnull = " NOT NULL" if r[3] else ""
        default = f" DEFAULT {r[4]}" if r[4] is not None else ""
        pk = " PRIMARY KEY" if r[5] else ""
        dst_defs.append(f"{name} {coltype}{notnull}{default}{pk}".strip())
    tmp = f"{table}__rename_tmp"
    conn.execute(f"CREATE TABLE {tmp} ({', '.join(dst_defs)})")
    conn.execute(
        f"INSERT INTO {tmp} SELECT {', '.join(src_names)} FROM {table}"
    )
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")


def _migrate_node_to_task(conn: sqlite3.Connection) -> None:
    """Migration 39 (2026-06-13, T-rename-node-to-task): rename the project-graph
    primitive ``node`` -> ``task`` on an EXISTING DB.

    Renames ``graph_nodes`` -> ``graph_tasks`` and ``node_id`` -> ``task_id`` on
    ``graph_edges`` and ``agent_runs`` IN THE SAME migration. Idempotent and
    backward-compatible: fresh DBs (no ``graph_nodes``) skip straight through and
    are created with the new names by migration 35 below. Self-healing: ``init_db``
    creates an empty ``graph_tasks`` BEFORE migrations run, so if both tables are
    present this reconciles them (drop the empty shell + rename, or merge rows)
    rather than stranding the populated ``graph_nodes``. NO-OPS on prod, which an
    earlier WIP agent already migrated to the renamed schema."""
    try:
        tables = _tables(conn)
        if "graph_nodes" in tables:
            if "graph_tasks" not in tables:
                conn.execute("ALTER TABLE graph_nodes RENAME TO graph_tasks")
                _drop_old_task_indexes(conn)
            else:
                _merge_nodes_into_tasks(conn)
        tables = _tables(conn)
        if "graph_edges" in tables:
            _rename_column(conn, "graph_edges", "node_id", "task_id")
        if "agent_runs" in tables:
            _rename_column(conn, "agent_runs", "node_id", "task_id")
        conn.commit()
        _log.info("Migration 39: graph_nodes->graph_tasks, node_id->task_id")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 39 (node->task rename) skipped: %s", e)


def migrate_runs_vcs(conn: sqlite3.Connection) -> None:
    """Migration 40 (T-vcs-checkpoint): VCS provenance columns on agent_runs.

    Adds repo_path/vcs_type/before_sha/after_sha/was_dirty so `juggle runs
    restore` can checkout a task's pre-run commit. Called from migrations_recent
    AFTER Migration 38 creates agent_runs. IDEMPOTENT: each ADD COLUMN is guarded
    by a PRAGMA column-existence check, so it converges on a fresh dev DB AND on
    prod, where the 5 columns already exist (applied out-of-band during the
    shared-DB incident). Migration 39 is the node->task rename above."""
    try:
        runs_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(agent_runs)").fetchall()
        }
        for col, defn in [
            ("repo_path", "TEXT"),
            ("vcs_type", "TEXT"),
            ("before_sha", "TEXT"),
            ("after_sha", "TEXT"),
            ("was_dirty", "INTEGER"),
        ]:
            if col not in runs_cols:
                conn.execute(f"ALTER TABLE agent_runs ADD COLUMN {col} {defn}")
        conn.commit()
        _log.info("Migration 40: VCS provenance columns added to agent_runs")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 40 (agent_runs vcs) skipped: %s", e)


def apply_graph_migrations(conn: sqlite3.Connection) -> None:
    """Apply migrations 35-37 + 39 (graph_tasks / graph_edges / graph_topics + rename)."""
    # Migration 39 runs FIRST: rename an existing node-era DB to task naming
    # before the CREATE-IF-NOT-EXISTS below would otherwise leave it stranded.
    _migrate_node_to_task(conn)

    # Migration 35: graph_tasks + graph_edges plan store for project autopilot
    # (design 2026-06-10 rev 2 — tasks hold the plan, threads only execute)
    try:
        conn.execute(CREATE_GRAPH_TASKS)
        conn.execute(CREATE_GRAPH_EDGES)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_tasks_project_state "
            "ON graph_tasks(project_id, state)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_tasks_thread "
            "ON graph_tasks(thread_id) WHERE thread_id IS NOT NULL"
        )
        conn.commit()
        _log.info("Migration 35: graph_tasks + graph_edges tables created")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 35 (graph_tasks/graph_edges) skipped: %s", e)

    # Migration 36: graph_tasks.diffstat — pre-merge diffstat captured inside
    # _run_integrate for dependent-task hydration (autopilot Phase 3, DA M4;
    # the branch+worktree are deleted on merge, so capture must be pre-merge)
    try:
        conn.execute("ALTER TABLE graph_tasks ADD COLUMN diffstat TEXT")
        conn.commit()
        _log.info("Migration 36: graph_tasks.diffstat column added")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 36 (graph_tasks.diffstat) skipped: %s", e)

    # Migration 37: graph_topics + graph_tasks.topic_id (3-tier R9, 2026-06-11).
    # Backfill wraps each flat task in a synthetic single-task topic ADOPTING
    # state/thread_id/updated_at — in-flight graphs keep running, and the
    # stale-sweep clock is preserved (updated_at copied, never now()).
    try:
        conn.execute(CREATE_GRAPH_TOPICS)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_topics_project_state "
            "ON graph_topics(project_id, state)"
        )
        try:
            conn.execute(
                "ALTER TABLE graph_tasks ADD COLUMN topic_id TEXT "
                "REFERENCES graph_topics(id)"
            )
        except sqlite3.OperationalError:
            pass  # column exists — idempotent re-run
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_tasks_topic "
            "ON graph_tasks(topic_id)"
        )
        task_ids = {r[0] for r in conn.execute("SELECT id FROM graph_tasks")}
        rows = conn.execute(
            "SELECT * FROM graph_tasks WHERE topic_id IS NULL"
        ).fetchall()
        for n in rows:
            tid = f"T-{n['id']}"
            if tid in task_ids:  # task literally named 'T-<x>' exists
                tid = f"T#{n['id']}"
            conn.execute(
                "INSERT OR IGNORE INTO graph_topics (id, project_id, title, "
                "objective, state, thread_id, handoff, diffstat, verified_at, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (tid, n["project_id"], n["title"], "", n["state"],
                 n["thread_id"], n["handoff"], n["diffstat"], n["verified_at"],
                 n["created_at"], n["updated_at"]),
            )
            conn.execute(
                "UPDATE graph_tasks SET topic_id=? WHERE id=?", (tid, n["id"])
            )
        conn.commit()
        _log.info("Migration 37: graph_topics created, %d task(s) backfilled",
                  len(rows))
    except sqlite3.OperationalError as e:
        _log.warning("Migration 37 (graph_topics) skipped: %s", e)

    migrate_is_mirror(conn)


def migrate_is_mirror(conn: sqlite3.Connection) -> None:
    """Migration 42 (2026-06-14, T-mirror): add is_mirror to graph_topics.

    Additive + idempotent: ALTER TABLE ADD COLUMN raises on a duplicate column
    name which we catch and swallow. Existing rows get is_mirror=0 via DEFAULT.
    """
    try:
        conn.execute(
            "ALTER TABLE graph_topics ADD COLUMN is_mirror INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()
        _log.info("Migration 42: graph_topics.is_mirror column added")
    except sqlite3.OperationalError as e:
        _log.debug("Migration 42 (graph_topics.is_mirror) skipped: %s", e)
