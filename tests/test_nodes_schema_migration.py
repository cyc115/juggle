"""Tests for the nodes/node_edges schema + migration 44 (P1 unified topic-graph).

All tests run against a fresh TEMP sqlite DB — never against prod.

Regression pin (2026-06-20): P1 additive migration — nodes + node_edges tables
are created and backfilled from threads/graph_topics/graph_tasks/graph_edges.
Old tables must remain untouched and the migration must be idempotent.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest


# ---------------------------------------------------------------------------
# Helpers: build a minimal DB that mirrors prod structure
# ---------------------------------------------------------------------------

def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _make_db(tmp_path) -> sqlite3.Connection:
    """Create a minimal SQLite DB with old tables seeded with representative rows."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS threads (
            id              TEXT PRIMARY KEY,
            session_id      TEXT NOT NULL DEFAULT '',
            topic           TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'active',
            summary         TEXT DEFAULT '',
            key_decisions   TEXT DEFAULT '[]',
            open_questions  TEXT DEFAULT '[]',
            last_user_intent TEXT DEFAULT '',
            agent_task_id   TEXT,
            agent_result    TEXT,
            show_in_list    INTEGER NOT NULL DEFAULT 1,
            summarized_msg_count INTEGER NOT NULL DEFAULT 0,
            title           TEXT DEFAULT '',
            created_at      TEXT NOT NULL,
            last_active     TEXT NOT NULL,
            last_dispatched_task  TEXT,
            last_dispatched_role  TEXT,
            last_dispatched_model TEXT,
            worktree_path         TEXT,
            worktree_branch       TEXT,
            main_repo_path        TEXT
        );

        CREATE TABLE IF NOT EXISTS graph_topics (
            id          TEXT PRIMARY KEY,
            project_id  TEXT NOT NULL,
            title       TEXT NOT NULL,
            objective   TEXT NOT NULL DEFAULT '',
            state       TEXT NOT NULL DEFAULT 'pending',
            thread_id   TEXT,
            handoff     TEXT,
            diffstat    TEXT,
            verified_at TEXT,
            merged_sha  TEXT,
            is_mirror   INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS graph_tasks (
            id          TEXT PRIMARY KEY,
            project_id  TEXT NOT NULL,
            title       TEXT NOT NULL,
            prompt      TEXT NOT NULL,
            verify_cmd  TEXT,
            state       TEXT NOT NULL DEFAULT 'pending',
            thread_id   TEXT,
            handoff     TEXT,
            diffstat    TEXT,
            verified_at TEXT,
            topic_id    TEXT,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS graph_edges (
            task_id       TEXT NOT NULL,
            depends_on_id TEXT NOT NULL,
            PRIMARY KEY (task_id, depends_on_id)
        );
    """)

    now = _now()

    # Insert a project
    conn.execute("INSERT INTO projects VALUES ('proj-1', 'Test Project')")

    # Thread: active conversation
    conn.execute("""
        INSERT INTO threads (id, topic, status, last_user_intent, created_at, last_active)
        VALUES ('th-active', 'Active convo', 'active', 'do stuff', ?, ?)
    """, (now, now))

    # Thread: closed conversation
    conn.execute("""
        INSERT INTO threads (id, topic, status, created_at, last_active)
        VALUES ('th-closed', 'Closed convo', 'closed', ?, ?)
    """, (now, now))

    # Thread: failed conversation
    conn.execute("""
        INSERT INTO threads (id, topic, status, created_at, last_active)
        VALUES ('th-failed', 'Failed convo', 'failed', ?, ?)
    """, (now, now))

    # Thread: archived conversation
    conn.execute("""
        INSERT INTO threads (id, topic, status, created_at, last_active)
        VALUES ('th-archived', 'Archived convo', 'archived', ?, ?)
    """, (now, now))

    # Thread: background (agent dispatched)
    conn.execute("""
        INSERT INTO threads (id, topic, status, worktree_path, worktree_branch,
                            main_repo_path, created_at, last_active)
        VALUES ('th-bg', 'Background convo', 'background', '/wt/path', 'cyc_bg',
                '/repo', ?, ?)
    """, (now, now))

    # graph_topic (is_mirror=0, task-tier, pending state)
    conn.execute("""
        INSERT INTO graph_topics (id, project_id, title, objective, state,
                                  is_mirror, created_at, updated_at)
        VALUES ('topic-1', 'proj-1', 'Big feature', 'Fix login', 'pending', 0, ?, ?)
    """, (now, now))

    # graph_topic (is_mirror=0, running state, has merged_sha)
    conn.execute("""
        INSERT INTO graph_topics (id, project_id, title, objective, state,
                                  merged_sha, is_mirror, created_at, updated_at)
        VALUES ('topic-2', 'proj-1', 'Done feature', 'Implement X', 'verified',
                'abc123', 0, ?, ?)
    """, (now, now))

    # graph_topic (is_mirror=1 = mirror/conversation)
    conn.execute("""
        INSERT INTO graph_topics (id, project_id, title, objective, state,
                                  is_mirror, thread_id, created_at, updated_at)
        VALUES ('topic-mirror', 'proj-1', 'Mirror topic', '', 'running', 1,
                'th-bg', ?, ?)
    """, (now, now))

    # graph_tasks (topic_id set = sub-task)
    conn.execute("""
        INSERT INTO graph_tasks (id, project_id, title, prompt, state,
                                 topic_id, verify_cmd, created_at, updated_at)
        VALUES ('task-1', 'proj-1', 'Sub task A', 'Do A', 'pending',
                'topic-1', 'pytest -k A', ?, ?)
    """, (now, now))

    conn.execute("""
        INSERT INTO graph_tasks (id, project_id, title, prompt, state,
                                 topic_id, created_at, updated_at)
        VALUES ('task-2', 'proj-1', 'Sub task B', 'Do B', 'running',
                'topic-1', ?, ?)
    """, (now, now))

    # graph_tasks: flat task (topic_id IS NULL, legacy pre-3-tier)
    conn.execute("""
        INSERT INTO graph_tasks (id, project_id, title, prompt, state,
                                 topic_id, created_at, updated_at)
        VALUES ('task-flat', 'proj-1', 'Flat task', 'Do flat thing', 'pending',
                NULL, ?, ?)
    """, (now, now))

    # graph_edges: task-2 depends on task-1
    conn.execute("""
        INSERT INTO graph_edges (task_id, depends_on_id)
        VALUES ('task-2', 'task-1')
    """)

    conn.commit()
    return conn


def _run_migration(conn: sqlite3.Connection) -> None:
    """Run the nodes migration (44) against the given connection."""
    from dbops.migrations_nodes import apply_nodes_migration
    apply_nodes_migration(conn)


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


def test_nodes_table_created(tmp_path):
    """Migration 44 creates the nodes table with all required columns."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
    required = {
        "id", "kind", "title", "objective", "state", "project_id", "parent_id",
        "verify_cmd", "worktree_path", "worktree_branch", "main_repo_path",
        "handoff", "diffstat", "verified_at", "merged_sha",
        "agent_task_id", "agent_result", "last_dispatched_task",
        "last_dispatched_role", "last_dispatched_model",
        "session_id", "summary", "key_decisions", "open_questions",
        "last_user_intent", "summarized_msg_count", "show_in_list",
        "created_at", "updated_at",
    }
    assert required <= cols, f"Missing columns: {required - cols}"


def test_node_edges_table_created(tmp_path):
    """Migration 44 creates the node_edges table."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "node_edges" in tables

    cols = {r[1] for r in conn.execute("PRAGMA table_info(node_edges)").fetchall()}
    assert {"node_id", "depends_on_id"} <= cols


# ---------------------------------------------------------------------------
# Row count / row mapping tests
# ---------------------------------------------------------------------------


def test_migration_row_count(tmp_path):
    """nodes COUNT = threads + graph_topics(is_mirror=0) + graph_tasks."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    n_threads = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    n_topics_task = conn.execute(
        "SELECT COUNT(*) FROM graph_topics WHERE is_mirror=0"
    ).fetchone()[0]
    n_tasks = conn.execute("SELECT COUNT(*) FROM graph_tasks").fetchone()[0]
    # is_mirror=1 rows → conversation nodes (counted separately from threads)
    n_topics_conv = conn.execute(
        "SELECT COUNT(*) FROM graph_topics WHERE is_mirror=1"
    ).fetchone()[0]
    expected = n_threads + n_topics_task + n_tasks + n_topics_conv
    actual = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    assert actual == expected, f"Expected {expected} nodes, got {actual}"


def test_migration_no_pending_state(tmp_path):
    """After migration, no nodes should have state='pending'."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    count = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE state='pending'"
    ).fetchone()[0]
    assert count == 0, f"{count} nodes still have state='pending'"


def test_migration_conversation_kind(tmp_path):
    """All thread rows + is_mirror=1 topics become kind='conversation' nodes."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    # Every thread ID should appear as a conversation node
    thread_ids = {r[0] for r in conn.execute("SELECT id FROM threads").fetchall()}
    conv_ids = {r[0] for r in conn.execute(
        "SELECT id FROM nodes WHERE kind='conversation'"
    ).fetchall()}
    assert thread_ids <= conv_ids, f"Missing thread IDs in conversations: {thread_ids - conv_ids}"


def test_migration_task_kind(tmp_path):
    """graph_topics(is_mirror=0) and graph_tasks → kind='task' nodes."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    task_ids = {r[0] for r in conn.execute("SELECT id FROM nodes WHERE kind='task'").fetchall()}

    topic_task_ids = {r[0] for r in conn.execute(
        "SELECT id FROM graph_topics WHERE is_mirror=0"
    ).fetchall()}
    gtask_ids = {r[0] for r in conn.execute("SELECT id FROM graph_tasks").fetchall()}

    assert topic_task_ids <= task_ids
    assert gtask_ids <= task_ids


def test_migration_state_mapping_threads(tmp_path):
    """threads.status values are mapped to correct node.state per §4.3."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    mapping = {
        "th-active": "open",
        "th-closed": "done",
        "th-failed": "failed-exec",
        "th-archived": "archived",
        # R2-1: background is first-class & bijective — Migration 44 now backfills via
        # the canonical STATUS_TO_STATE, so 'background' maps to itself (was 'running').
        "th-bg": "background",
    }
    for tid, expected_state in mapping.items():
        row = conn.execute("SELECT state FROM nodes WHERE id=?", (tid,)).fetchone()
        assert row is not None, f"Node {tid} not found"
        assert row[0] == expected_state, f"Thread {tid}: expected {expected_state}, got {row[0]}"


def test_migration_pending_becomes_open_for_tasks(tmp_path):
    """graph_topics and graph_tasks with state='pending' get state='open'."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    # topic-1 was pending → should be open
    row = conn.execute("SELECT state FROM nodes WHERE id='topic-1'").fetchone()
    assert row is not None
    assert row[0] == "open"

    # task-1 was pending → should be open
    row = conn.execute("SELECT state FROM nodes WHERE id='task-1'").fetchone()
    assert row is not None
    assert row[0] == "open"


def test_migration_task_parent_id(tmp_path):
    """graph_tasks with topic_id get parent_id = that topic's id in nodes."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    row = conn.execute("SELECT parent_id FROM nodes WHERE id='task-1'").fetchone()
    assert row is not None
    assert row[0] == "topic-1"

    row = conn.execute("SELECT parent_id FROM nodes WHERE id='task-2'").fetchone()
    assert row is not None
    assert row[0] == "topic-1"


def test_migration_flat_task_null_parent(tmp_path):
    """graph_tasks with topic_id IS NULL (flat tasks) get parent_id=NULL."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    row = conn.execute("SELECT parent_id FROM nodes WHERE id='task-flat'").fetchone()
    assert row is not None
    assert row[0] is None


def test_migration_merged_sha_preserved(tmp_path):
    """topic nodes with merged_sha have that value copied to nodes.merged_sha."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    row = conn.execute("SELECT merged_sha FROM nodes WHERE id='topic-2'").fetchone()
    assert row is not None
    assert row[0] == "abc123"


def test_migration_node_edges_count(tmp_path):
    """node_edges COUNT == graph_edges COUNT."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    n_edges = conn.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]
    n_node_edges = conn.execute("SELECT COUNT(*) FROM node_edges").fetchone()[0]
    assert n_node_edges == n_edges


def test_migration_node_edges_mapping(tmp_path):
    """graph_edges.(task_id, depends_on_id) → node_edges.(node_id, depends_on_id)."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    row = conn.execute(
        "SELECT node_id, depends_on_id FROM node_edges WHERE node_id='task-2'"
    ).fetchone()
    assert row is not None
    assert row[0] == "task-2"
    assert row[1] == "task-1"


def test_migration_verify_cmd_preserved(tmp_path):
    """graph_tasks.verify_cmd is copied into nodes.verify_cmd."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    row = conn.execute("SELECT verify_cmd FROM nodes WHERE id='task-1'").fetchone()
    assert row is not None
    assert row[0] == "pytest -k A"


def test_migration_objective_mapping(tmp_path):
    """graph_tasks.prompt → nodes.objective; graph_topics.objective → nodes.objective."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    # task-1 from graph_tasks: prompt='Do A'
    row = conn.execute("SELECT objective FROM nodes WHERE id='task-1'").fetchone()
    assert row is not None
    assert row[0] == "Do A"

    # topic-1 from graph_topics: objective='Fix login'
    row = conn.execute("SELECT objective FROM nodes WHERE id='topic-1'").fetchone()
    assert row is not None
    assert row[0] == "Fix login"

    # th-active from threads: last_user_intent='do stuff'
    row = conn.execute("SELECT objective FROM nodes WHERE id='th-active'").fetchone()
    assert row is not None
    assert row[0] == "do stuff"


# ---------------------------------------------------------------------------
# Idempotency test
# ---------------------------------------------------------------------------


def test_migration_idempotent(tmp_path):
    """Running migration twice yields same nodes count (no duplicates)."""
    conn = _make_db(tmp_path)
    _run_migration(conn)
    count_after_first = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    edge_count_first = conn.execute("SELECT COUNT(*) FROM node_edges").fetchone()[0]

    _run_migration(conn)
    count_after_second = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    edge_count_second = conn.execute("SELECT COUNT(*) FROM node_edges").fetchone()[0]

    assert count_after_first == count_after_second, "Second run duplicated nodes"
    assert edge_count_first == edge_count_second, "Second run duplicated node_edges"


# ---------------------------------------------------------------------------
# Old tables preserved test
# ---------------------------------------------------------------------------


def test_old_tables_still_present(tmp_path):
    """After migration, all old tables remain unchanged."""
    conn = _make_db(tmp_path)
    _run_migration(conn)

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    for old_table in ("threads", "graph_topics", "graph_tasks", "graph_edges"):
        assert old_table in tables, f"Old table {old_table} was removed!"


def test_old_table_rows_unchanged(tmp_path):
    """Thread and graph row counts are unchanged after migration."""
    conn = _make_db(tmp_path)

    before = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("threads", "graph_topics", "graph_tasks", "graph_edges")
    }

    _run_migration(conn)

    for t, count in before.items():
        after = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        assert after == count, f"Old table {t} changed: was {count}, now {after}"


# ---------------------------------------------------------------------------
# D2 regression pins: backfill robustness + no silent empty-nodes
# (2026-06-20: migration 44 backfill silently no-op'd on minimal threads schema)
# ---------------------------------------------------------------------------


def _make_minimal_threads_db(tmp_path) -> sqlite3.Connection:
    """Minimal threads table missing optional columns (summary, key_decisions, etc.).

    Simulates an older/migrated DB where threads lacks nullable metadata columns
    that the full canonical schema has. Migration 44 must still backfill thread
    rows into nodes using only the columns that exist.
    """
    db_path = tmp_path / "minimal.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE threads (
            id         TEXT PRIMARY KEY,
            topic      TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            last_active TEXT NOT NULL
        );
        CREATE TABLE graph_topics (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            title TEXT NOT NULL, objective TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'ready',
            thread_id TEXT, handoff TEXT, diffstat TEXT,
            verified_at TEXT, merged_sha TEXT, is_mirror INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE graph_tasks (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            title TEXT NOT NULL, prompt TEXT NOT NULL DEFAULT '',
            verify_cmd TEXT, state TEXT NOT NULL DEFAULT 'ready',
            thread_id TEXT, handoff TEXT, diffstat TEXT, verified_at TEXT,
            topic_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE graph_edges (
            task_id TEXT NOT NULL, depends_on_id TEXT NOT NULL,
            PRIMARY KEY (task_id, depends_on_id)
        );
    """)
    now = _now()
    conn.execute(
        "INSERT INTO threads (id, topic, status, created_at, last_active) VALUES (?, ?, ?, ?, ?)",
        ("th-min-1", "Minimal thread", "active", now, now),
    )
    conn.commit()
    return conn


def test_backfill_threads_minimal_schema_still_populates_nodes(tmp_path):
    """REGRESSION PIN (2026-06-20 D2): migration 44 against a minimal threads
    table (missing summary, key_decisions, etc.) must still insert thread rows
    into nodes — it must NOT silently no-op and leave nodes empty.

    Pre-fix: _backfill_threads SELECT'd all columns including 'summary'; on a
    minimal schema this threw OperationalError, which was caught+rolled back,
    leaving nodes empty with only a WARNING log.
    """
    conn = _make_minimal_threads_db(tmp_path)
    _run_migration(conn)

    count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    assert count > 0, (
        "nodes table is EMPTY after migration 44 against minimal threads schema — "
        "backfill silently no-op'd (D2 regression)"
    )

    # The thread row must appear as a conversation node with correct fields
    row = conn.execute(
        "SELECT id, kind, title, state FROM nodes WHERE id='th-min-1'"
    ).fetchone()
    assert row is not None, "th-min-1 not backfilled into nodes"
    assert row["kind"] == "conversation"
    assert row["state"] == "open"  # 'active' → 'open'


def test_backfill_threads_full_schema_populates_nodes(tmp_path):
    """REGRESSION PIN (2026-06-20 D2): migration 44 against the full canonical
    threads schema (with summary, key_decisions, etc.) must populate nodes.
    This pins that the robust column-introspection fix doesn't break the
    happy path for fully-migrated production DBs.
    """
    conn = _make_db(tmp_path)
    _run_migration(conn)

    count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    assert count > 0, "nodes table is EMPTY after migration 44 against full schema"

    # Full schema: summary and last_user_intent should be preserved
    row = conn.execute(
        "SELECT summary, objective FROM nodes WHERE id='th-active'"
    ).fetchone()
    assert row is not None, "th-active not backfilled"
    assert row["objective"] == "do stuff"  # last_user_intent mapped to objective


def test_backfill_threads_id_topic_only_schema(tmp_path):
    """REGRESSION PIN (2026-06-20): migration 44 against an absolute-minimal
    threads table with only (id, topic) — no status, created_at, last_active —
    must NOT raise and must backfill a conversation node with state='open'
    (NULL status → 'open' via _THREAD_STATUS_MAP fallback).

    Pre-fix: status/created_at/last_active were hardcoded in select_cols so
    the SELECT raised sqlite3.OperationalError: no such column: status on any
    pre-migration DB lacking those columns (e.g. test_db_graph.py fixture).
    """
    db_path = tmp_path / "idtopic.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE threads (id TEXT PRIMARY KEY, topic TEXT NOT NULL);
        CREATE TABLE graph_topics (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            title TEXT NOT NULL, objective TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'ready',
            thread_id TEXT, handoff TEXT, diffstat TEXT,
            verified_at TEXT, merged_sha TEXT, is_mirror INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE graph_tasks (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
            title TEXT NOT NULL, prompt TEXT NOT NULL DEFAULT '',
            verify_cmd TEXT, state TEXT NOT NULL DEFAULT 'ready',
            thread_id TEXT, handoff TEXT, diffstat TEXT, verified_at TEXT,
            topic_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE graph_edges (
            task_id TEXT NOT NULL, depends_on_id TEXT NOT NULL,
            PRIMARY KEY (task_id, depends_on_id)
        );
    """)
    conn.execute("INSERT INTO threads (id, topic) VALUES ('th-bare', 'Bare thread')")
    conn.commit()

    _run_migration(conn)

    row = conn.execute(
        "SELECT id, kind, state FROM nodes WHERE id='th-bare'"
    ).fetchone()
    assert row is not None, "th-bare not backfilled into nodes (D2 regression)"
    assert row["kind"] == "conversation"
    assert row["state"] == "open"  # NULL status → 'open' via fallback


# ---------------------------------------------------------------------------
# Parametrized missing-column pin (2026-06-20): closes whole class of
# migration-44 OperationalError: no such column: <x> failures
# ---------------------------------------------------------------------------

_THREADS_ALL_OPTIONAL_COLS = [
    "topic", "status", "created_at", "last_active",
    "session_id", "summary", "key_decisions", "open_questions",
    "last_user_intent", "agent_task_id", "agent_result",
    "show_in_list", "summarized_msg_count",
    "last_dispatched_task", "last_dispatched_role", "last_dispatched_model",
    "worktree_path", "worktree_branch", "main_repo_path",
]

_THREADS_FULL_DDL = """
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    summary TEXT DEFAULT '',
    key_decisions TEXT DEFAULT '[]',
    open_questions TEXT DEFAULT '[]',
    last_user_intent TEXT DEFAULT '',
    agent_task_id TEXT,
    agent_result TEXT,
    show_in_list INTEGER NOT NULL DEFAULT 1,
    summarized_msg_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT '1970-01-01T00:00:00',
    last_active TEXT NOT NULL DEFAULT '1970-01-01T00:00:00',
    last_dispatched_task TEXT,
    last_dispatched_role TEXT,
    last_dispatched_model TEXT,
    worktree_path TEXT,
    worktree_branch TEXT,
    main_repo_path TEXT
"""

_SUPPORT_TABLES_DDL = """
    CREATE TABLE IF NOT EXISTS graph_topics (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
        title TEXT NOT NULL, objective TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'ready',
        thread_id TEXT, handoff TEXT, diffstat TEXT,
        verified_at TEXT, merged_sha TEXT, is_mirror INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS graph_tasks (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
        title TEXT NOT NULL, prompt TEXT NOT NULL DEFAULT '',
        verify_cmd TEXT, state TEXT NOT NULL DEFAULT 'ready',
        thread_id TEXT, handoff TEXT, diffstat TEXT, verified_at TEXT,
        topic_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS graph_edges (
        task_id TEXT NOT NULL, depends_on_id TEXT NOT NULL,
        PRIMARY KEY (task_id, depends_on_id)
    );
"""


def _threads_ddl_minus(missing_col: str) -> str:
    """Return threads DDL with one column removed."""
    lines = [l for l in _THREADS_FULL_DDL.splitlines()
             if missing_col not in l or l.strip().startswith("--")]
    # Strip trailing comma from last non-empty line to avoid syntax error
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            lines[i] = lines[i].rstrip().rstrip(",")
            break
    return "\n".join(lines)


@pytest.mark.parametrize("missing_col", _THREADS_ALL_OPTIONAL_COLS + [None])
def test_backfill_threads_missing_col_parametrized(tmp_path, missing_col):
    """REGRESSION PIN (2026-06-20): migration 44 must not raise
    sqlite3.OperationalError regardless of which non-id column is absent
    from threads, including topic and the truly-minimal threads(id) table.

    Symptom: test_doctor_preserves_archived_labels and test_db_graph fixtures
    create threads tables without certain columns; migration 44 previously
    hardcoded those columns in SELECT, causing OperationalError.
    """
    if missing_col is None:
        # truly minimal: threads(id) only
        ddl = "id TEXT PRIMARY KEY"
        label = "id_only"
    else:
        ddl = _threads_ddl_minus(missing_col)
        label = missing_col

    db_path = tmp_path / f"missing_{label}.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(f"CREATE TABLE threads ({ddl})")
    conn.executescript(_SUPPORT_TABLES_DDL)
    has_topic = missing_col != "topic" and missing_col is not None
    if has_topic:
        conn.execute("INSERT INTO threads (id, topic) VALUES ('th-missing', 'test topic')")
    else:
        conn.execute("INSERT INTO threads (id) VALUES ('th-missing')")
    conn.commit()

    # Must not raise
    _run_migration(conn)

    row = conn.execute(
        "SELECT id, kind, title, state FROM nodes WHERE id='th-missing'"
    ).fetchone()
    assert row is not None, f"th-missing not backfilled (missing col: {label})"
    assert row["kind"] == "conversation"
    assert row["state"] == "open"
    assert row["title"]  # title falls back to id when topic is absent


# ---------------------------------------------------------------------------
# P8 c5-ddl (2026-06-27): CREATE_NODES is complete on its own (H4) + the nodes
# slug-uniqueness index keeps 'background' in its predicate (R2-1 slug guard,
# folded in from c3-write-cut).
# ---------------------------------------------------------------------------


def test_create_nodes_is_complete():
    """2026-06-27 P8 H4: a fresh nodes table from CREATE_NODES alone (no
    migrations) must contain every column conv_node_mirror writes — the parity
    columns are folded into the DDL so the mirror no longer needs to swallow a
    missing-column OperationalError."""
    from dbops.schema_nodes import CREATE_NODES
    conn = sqlite3.connect(":memory:")
    conn.execute(CREATE_NODES)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    for c in ("user_label", "assigned_by", "last_active_at", "dispatch_thread_id",
              "session_id", "summarized_msg_count", "show_in_list"):
        assert c in cols, f"CREATE_NODES missing {c}"


def test_nodes_slug_index_predicate_covers_background(tmp_path):
    """2026-06-27 P8 R2-1 (folded from c3-write-cut): the nodes slug-uniqueness
    index predicate MUST include the live 'background' state. RED on the prior
    state IN ('open','running') predicate that dropped 'background' — the exact
    2026-06-21 recycled-slug incident, now on the nodes seam."""
    from juggle_db import JuggleDB
    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()
    with db._connect() as conn:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_nodes_user_label'"
        ).fetchone()
    assert row is not None, "idx_nodes_user_label missing"
    assert "background" in row[0], (
        "background dropped from nodes slug-index predicate — a live background "
        "agent's slug could be recycled onto a new live conversation"
    )


def test_nodes_index_rejects_duplicate_live_background_slug(tmp_path):
    """2026-06-27 P8 R2-1 (folded from c3-write-cut): two LIVE conversation
    nodes — one 'background', one 'open' — must not share a user_label. The
    partial unique index forbids it. RED on the 2-state predicate (a 'background'
    node was unindexed, so its slug could be recycled to a new 'open' node)."""
    from juggle_db import JuggleDB
    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()
    now = "2026-06-27T00:00:00"
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO nodes (id, kind, title, state, user_label, created_at, updated_at) "
            "VALUES ('n-bg','conversation','bg','background','ZZ',?,?)",
            (now, now),
        )
        conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        with db._connect() as conn:
            conn.execute(
                "INSERT INTO nodes (id, kind, title, state, user_label, created_at, updated_at) "
                "VALUES ('n-open','conversation','open','open','ZZ',?,?)",
                (now, now),
            )
            conn.commit()
