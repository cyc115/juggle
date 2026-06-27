"""dbops.migrations_recent — schema migrations 20-39 + 41.

Owns: the second half of the incremental migration chain (watchdog columns
onward), applied by ``dbops.migrations.run_migrations`` after migrations 1-19.
Migrations 35-37 + 39 (graph store + node->task rename) live in
``dbops.migrations_graph``; 38 (agent_runs ledger) is inline here.
Must not own: query or business logic — only schema evolution.
"""

from __future__ import annotations

import logging
import sqlite3

from dbops.schema import (
    CREATE_AGENT_COMPLETIONS,
    CREATE_AGENT_TOOL_EVENTS,
    CREATE_ERROR_EVENTS,
    CREATE_PROJECT_CORRECTIONS,
    CREATE_PROJECTS,
    CREATE_WATCHDOG_EVENTS,
    INBOX_PROJECT_ID,
    _now,
    _wheel_index,
)

_log = logging.getLogger(__name__)


def apply_recent_migrations(conn: sqlite3.Connection) -> None:
    """Apply incremental schema migrations 20-36 (idempotent)."""
    # Migration 20: all watchdog columns on agents
    agents_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()
    }
    try:
        for col, defn in [
            ("watchdog_retried", "INTEGER NOT NULL DEFAULT 0"),
            ("watchdog_threshold_minutes", "INTEGER"),
            ("model", "TEXT"),
            ("last_task", "TEXT"),
            ("busy_since", "TEXT"),
            ("last_send_task_pane_hash", "TEXT"),
            ("last_send_task_at", "TEXT"),
            ("last_activity_at", "TEXT"),
        ]:
            if col not in agents_cols:
                conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {defn}")
        conn.commit()
        _log.info("Migration 20: watchdog columns added to agents")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 20 (watchdog agent cols) skipped: %s", e)

    # Migration 21: agent_completions table + index
    try:
        conn.execute(CREATE_AGENT_COMPLETIONS)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_completions_role_date "
            "ON agent_completions(role, completed_at)"
        )
        conn.commit()
        _log.info("Migration 21: agent_completions table created")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 21 (agent_completions) skipped: %s", e)

    # Migration 22: watchdog_events table + threads dispatch payload columns
    try:
        conn.execute(CREATE_WATCHDOG_EVENTS)
        threads_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()
        }
        for col in (
            "last_dispatched_task",
            "last_dispatched_role",
            "last_dispatched_model",
        ):
            if col not in threads_cols:
                conn.execute(f"ALTER TABLE threads ADD COLUMN {col} TEXT")
        conn.commit()
        _log.info(
            "Migration 22: watchdog_events + threads dispatch payload columns created"
        )
    except sqlite3.OperationalError as e:
        _log.warning("Migration 22 (watchdog_events + threads) skipped: %s", e)

    # Migration 23: last_reflect_msg_count for reflect() gate
    threads_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    if "last_reflect_msg_count" not in threads_cols:
        try:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN last_reflect_msg_count INTEGER DEFAULT 0"
            )
            conn.commit()
            _log.info("Migration 23: last_reflect_msg_count column added")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 23 (last_reflect_msg_count) skipped: %s", e)

    # Migration 24: error_events for self-heal
    tables_now = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "error_events" not in tables_now:
        try:
            conn.execute(CREATE_ERROR_EVENTS)
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_error_events_sig "
                "ON error_events(signature_hash) WHERE status != 'resolved'"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_error_events_status "
                "ON error_events(status)"
            )
            conn.commit()
            _log.info("Migration 24: error_events table created")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 24 (error_events) skipped: %s", e)

    # Migration 25: agent_tool_events telemetry table
    if "agent_tool_events" not in tables_now:
        try:
            conn.execute(CREATE_AGENT_TOOL_EVENTS)
            conn.commit()
            _log.info("Migration 25: agent_tool_events table created")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 25 (agent_tool_events) skipped: %s", e)

    # Migration 26: projects table + INBOX seed + project_id on threads
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    cols_threads = {
        r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    if "projects" not in tables:
        conn.execute(CREATE_PROJECTS)
    now = _now()
    conn.execute(
        "INSERT OR IGNORE INTO projects (id, name, objective, status, created_at, last_active) VALUES (?,?,?,?,?,?)",
        (INBOX_PROJECT_ID, "Inbox", "Catch-all for unassigned threads", "active", now, now),
    )
    if "project_id" not in cols_threads:
        conn.execute(
            "ALTER TABLE threads ADD COLUMN project_id TEXT DEFAULT 'INBOX' REFERENCES projects(id)"
        )
        conn.execute("UPDATE threads SET project_id = 'INBOX' WHERE project_id IS NULL")

    # Migration 27: assigned_by on threads ('auto'|'human')
    cols_threads = {
        r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    if "assigned_by" not in cols_threads:
        try:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN assigned_by TEXT NOT NULL DEFAULT 'auto'"
            )
            conn.commit()
            _log.info("Migration 27: assigned_by column added to threads")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 27 (assigned_by) skipped: %s", e)

    # Migration 28: project_corrections append-only log
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "project_corrections" not in tables:
        try:
            conn.execute(CREATE_PROJECT_CORRECTIONS)
            conn.commit()
            _log.info("Migration 28: project_corrections table created")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 28 (project_corrections) skipped: %s", e)

    # Migration 29: summary + closed_at on projects (project close/open feature)
    proj_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()
    }
    try:
        if "summary" not in proj_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN summary TEXT DEFAULT ''")
        if "closed_at" not in proj_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN closed_at TEXT")
        conn.commit()
        _log.info("Migration 29: summary + closed_at added to projects")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 29 (projects summary/closed_at) skipped: %s", e)

    # Migration 30: match_profile + profile_synth_at + profile_dirty on projects
    proj_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()
    }
    try:
        if "match_profile" not in proj_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN match_profile TEXT DEFAULT ''")
        if "profile_synth_at" not in proj_cols:
            conn.execute("ALTER TABLE projects ADD COLUMN profile_synth_at TEXT")
        if "profile_dirty" not in proj_cols:
            conn.execute(
                "ALTER TABLE projects ADD COLUMN profile_dirty INTEGER NOT NULL DEFAULT 0"
            )
        conn.commit()
        _log.info("Migration 30: match_profile columns added to projects")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 30 (match_profile) skipped: %s", e)

    # Migration 31: assigned_confidence on threads
    threads_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    if "assigned_confidence" not in threads_cols:
        try:
            conn.execute(
                "ALTER TABLE threads ADD COLUMN assigned_confidence REAL DEFAULT NULL"
            )
            conn.commit()
            _log.info("Migration 31: assigned_confidence added to threads")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 31 (assigned_confidence) skipped: %s", e)

    # Migration 32: harness + oneshot_pid on agents
    agents_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()
    }
    try:
        for col, defn in [
            ("harness", "TEXT"),
            ("oneshot_pid", "INTEGER"),
        ]:
            if col not in agents_cols:
                conn.execute(f"ALTER TABLE agents ADD COLUMN {col} {defn}")
        conn.commit()
        _log.info("Migration 32: harness + oneshot_pid columns added to agents")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 32 (harness/oneshot_pid) skipped: %s", e)

    # Migration 33: repo_path on agents
    agents_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()
    }
    if "repo_path" not in agents_cols:
        try:
            conn.execute("ALTER TABLE agents ADD COLUMN repo_path TEXT")
            conn.commit()
            _log.info("Migration 33: repo_path column added to agents")
        except sqlite3.OperationalError as e:
            _log.warning("Migration 33 (repo_path) skipped: %s", e)

    # Migration 34: worktree columns on threads
    threads_cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()
    }
    try:
        for col, defn in [
            ("worktree_path", "TEXT"),
            ("worktree_branch", "TEXT"),
            ("main_repo_path", "TEXT"),
        ]:
            if col not in threads_cols:
                conn.execute(f"ALTER TABLE threads ADD COLUMN {col} {defn}")
        conn.commit()
        _log.info("Migration 34: worktree columns added to threads")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 34 (worktree) skipped: %s", e)

    # Migrations 35-37 + 39 (graph store + node->task rename) live in
    # dbops.migrations_graph (300-line gate).
    from dbops.migrations_graph import _rename_column, apply_graph_migrations, migrate_runs_vcs
    from dbops.schema_runs import CREATE_AGENT_RUNS, CREATE_AGENT_RUNS_INDEXES

    apply_graph_migrations(conn)

    # Migration 38: agent_runs ledger + current_run_id pointer. Created AFTER the
    # graph migrations, so the migration-39 rename of agent_runs.node_id->task_id
    # is re-run here (idempotent) now that the table is guaranteed present.
    try:
        conn.execute(CREATE_AGENT_RUNS)
        for _idx in CREATE_AGENT_RUNS_INDEXES:
            conn.execute(_idx)
        agents_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(agents)").fetchall()
        }
        if "current_run_id" not in agents_cols:
            conn.execute("ALTER TABLE agents ADD COLUMN current_run_id TEXT")
        _rename_column(conn, "agent_runs", "node_id", "task_id")
        conn.commit()
        _log.info("Migration 38: agent_runs ledger + current_run_id created")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 38 (agent_runs) skipped: %s", e)
    # Migration 40 (T-vcs-checkpoint): VCS cols on agent_runs (impl in graph mod).
    migrate_runs_vcs(conn)
    # Migration 41: drop 4 dead Hindsight columns from threads.
    # NOT run against prod automatically — apply via `juggle doctor` on main.
    run_migration_41(conn)

    # Migration 42 (T-slug-wheel): juggle_meta counter + slug-wheel indexes.
    # MUST run after 41, which rebuilds `threads` and re-creates the OLD global
    # unique index from a snapshot — we drop it here and install the partial /
    # covering indexes the wheel relies on.
    run_migration_slug_wheel(conn)

    # Migration 44 (P1 unified-topic-graph): create nodes + node_edges tables
    # and backfill from threads/graph_topics/graph_tasks/graph_edges. Purely
    # additive — old tables stay; no read path changes.
    from dbops.migrations_graph import apply_nodes_migration_44
    apply_nodes_migration_44(conn)

    # Migration 45 (selfheal-triage-v2 P1): drop status CHECK on error_events.
    _migrate_45_drop_status_check(conn)
    # Migrations 47-49 (selfheal-triage-v2 P2): group_key + audit + lease.
    apply_selfheal_p2_migrations(conn)

    # Migration 50 (unified-topic-graph P8 prep): additive nodes parity columns
    # (user_label/assigned_by/last_active_at) + kind-scoped slug index, then
    # backfill them from threads. ADDITIVE; applied via juggle doctor. Blocks the
    # P8 read-collapse; also fixes the Migration-44 last_active backfill-staleness.
    from dbops.migration_nodes_parity import migrate_50_nodes_parity, backfill_nodes_parity
    migrate_50_nodes_parity(conn)
    backfill_nodes_parity(conn)  # also runs backfill_graph_parity (P8 Q2/Q3)
    from dbops.migration_51_state_vocab import migrate_51_state_vocab  # P8 C3+R2-4
    migrate_51_state_vocab(conn)  # unify task vocab pending->open (FAIL-LOUD, before renamed engine)


# Migrations 41, 45 and 47-49 live in their own modules (loc_gate budget).
from dbops.migration_41_threads import run_migration_41  # noqa: E402
from dbops.migration_selfheal_status_check import migrate_45_drop_status_check as _migrate_45_drop_status_check  # noqa: E402,E501
from dbops.migrations_selfheal_p2 import apply_selfheal_p2_migrations  # noqa: E402


def run_migration_slug_wheel(conn: sqlite3.Connection) -> None:
    """Install the Topic Slug Wheel schema. Idempotent and guarded.

    - juggle_meta(key, value) durable key/value table (holds label_seq).
    - DROP the global unique index idx_threads_user_label.
    - REPAIR pre-existing duplicate live labels, then (re)ADD the partial unique
      idx_threads_live_label over ALL live states (active/running/background) so
      no two LIVE topics share a slug (2026-06-21: 'background' was omitted).
    - ADD covering idx_threads_label_created for newest-wins lookups.
    - Seed label_seq from the highest in-use wheel position (or 0 if none).

    DO NOT run against the shared production DB directly; apply via juggle doctor.
    """
    from dbops.slug_alloc import repair_duplicate_live_labels
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS juggle_meta "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute("DROP INDEX IF EXISTS idx_threads_user_label")
        conn.execute("DROP INDEX IF EXISTS idx_threads_live_label")
        repair_duplicate_live_labels(conn)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_threads_live_label "
            "ON threads(user_label) WHERE user_label IS NOT NULL "
            "AND status IN ('active','running','background')"
        )
        # Covering index for newest-wins lookup — only if created_at exists
        # (defensive: some hand-rolled/legacy schemas may lack it).
        tcols = {r[1] for r in conn.execute("PRAGMA table_info(threads)").fetchall()}
        if "created_at" in tcols:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_threads_label_created "
                "ON threads(user_label, created_at)"
            )
        # Seed label_seq once, from the highest in-use two-letter wheel position.
        row = conn.execute(
            "SELECT value FROM juggle_meta WHERE key = 'label_seq'"
        ).fetchone()
        if row is None:
            seen_idx = [
                i for r in conn.execute(
                    "SELECT DISTINCT user_label FROM threads "
                    "WHERE user_label IS NOT NULL"
                ).fetchall() if (i := _wheel_index(r[0])) is not None
            ]
            seed = (max(seen_idx) + 1) if seen_idx else 0
            conn.execute(
                "INSERT OR IGNORE INTO juggle_meta(key, value) "
                "VALUES ('label_seq', ?)",
                (str(seed),),
            )
        conn.commit()
        _log.info("Migration 42 (slug-wheel): juggle_meta + indexes installed")
    except sqlite3.OperationalError as e:
        _log.warning("Migration 42 (slug-wheel) skipped: %s", e)
