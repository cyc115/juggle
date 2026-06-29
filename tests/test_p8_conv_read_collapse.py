"""P8 Wave-3 conversation read-collapse pins.

Scope of THIS pass (2026-06-23): only the status-agnostic, id-only conversation
reads are collapsed onto `nodes` — the reads whose consumed columns (id only)
are losslessly mirrored by the dual-write. Status-bearing reads are NOT flipped
(the threads.status -> node.state map is lossy: background/running and
closed/done both collapse, and several project writes bypass the mirror), so
those remain on `threads`. See the floor analysis in the dispatch result.

Each test seeds a kind='conversation' node WITHOUT a legacy `threads` row, then
asserts the flipped read resolves from `nodes`. Pre-collapse these reads hit
`FROM threads` and find nothing.
"""
from __future__ import annotations

import pytest

from juggle_db import JuggleDB
from helpers.node_seed import seed_node


def _fresh(tmp_path):
    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    return db


def test_resolve_thread_id_prefix_resolves_from_nodes(tmp_path):
    """juggle_cli_common._resolve_thread hex-prefix path reads `nodes`."""
    from juggle_cli_common import _resolve_thread
    db = _fresh(tmp_path)
    hex_id = "abc123abc123def0"          # hex, >= 6 chars so the prefix path fires
    with db._connect() as conn:
        seed_node(conn, id=hex_id, kind="conversation", title="t")
        conn.commit()
    assert _resolve_thread(db, "abc123") == hex_id


def test_resolve_thread_id_prefix_ignores_non_conversation_nodes(tmp_path):
    """A kind='task' node must not be resolvable as a thread id prefix."""
    from juggle_cli_common import _resolve_thread
    db = _fresh(tmp_path)
    with db._connect() as conn:
        seed_node(conn, id="abc123abc123def0", kind="task", title="task")
        conn.commit()
    with pytest.raises(SystemExit):
        _resolve_thread(db, "abc123")


def test_autofix_schedule_thread_resolves_latest_conversation_node(tmp_path):
    """schedules.autofix._find_or_create_schedule_thread reads `nodes`."""
    from schedules.autofix import _find_or_create_schedule_thread
    db = _fresh(tmp_path)
    with db._connect() as conn:
        seed_node(conn, id="c1", kind="conversation", title="older",
                  created_at="2026-01-01 00:00")
        seed_node(conn, id="c2", kind="conversation", title="newer",
                  created_at="2026-01-02 00:00")
        conn.commit()
    assert _find_or_create_schedule_thread(db) == "c2"


def test_dogfood_prior_thread_check_reads_nodes(tmp_path):
    """2026-06-27 P8 Task 3.1: schedules.dogfood._check_prior_dogfood_thread reads
    the conversation `nodes` (kind='conversation', title LIKE 'dogfood-%'), not
    threads.topic. Pre-flip this hits `FROM threads` and finds nothing."""
    from schedules.dogfood import _check_prior_dogfood_thread
    db = _fresh(tmp_path)
    with db._connect() as conn:
        seed_node(conn, id="d1", kind="conversation", title="dogfood-2026-06-27",
                  state="open")
        conn.commit()
    assert _check_prior_dogfood_thread(db) == "dogfood-2026-06-27"


def test_dogfood_prior_thread_check_excludes_terminal_nodes(tmp_path):
    """2026-06-27 P8 Task 3.1: a closed (state='done') dogfood conversation must be
    excluded — the legacy `status NOT IN ('closed',...)` maps to `state NOT IN
    ('done',...)` via the bijective node-state vocab."""
    from schedules.dogfood import _check_prior_dogfood_thread
    db = _fresh(tmp_path)
    with db._connect() as conn:
        seed_node(conn, id="d1", kind="conversation", title="dogfood-done",
                  state="done")
        conn.commit()
    assert _check_prior_dogfood_thread(db) is None


def test_dogfood_active_session_reads_nodes(tmp_path):
    """2026-06-27 P8 Task 3.1: schedules.dogfood._check_active_session reads the
    most-recent live conversation node (state='open'), not threads.status='active'."""
    from datetime import datetime, timezone
    from schedules.dogfood import _check_active_session
    db = _fresh(tmp_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    with db._connect() as conn:
        seed_node(conn, id="a1", kind="conversation", title="live", state="open",
                  last_active_at=now_iso)
        conn.commit()
    assert _check_active_session(db) is True


def test_dogfood_schedule_thread_resolves_from_nodes(tmp_path):
    """2026-06-27 P8 Task 3.1: schedules.dogfood._find_or_create_schedule_thread
    reads conversation `nodes` (title LIKE 'schedule%', else newest), not threads."""
    from schedules.dogfood import _find_or_create_schedule_thread
    db = _fresh(tmp_path)
    with db._connect() as conn:
        seed_node(conn, id="s1", kind="conversation", title="schedule-weekly",
                  state="open")
        conn.commit()
    assert _find_or_create_schedule_thread(db) == "s1"


def test_cleanup_orphaned_threads_reads_running_nodes(tmp_path):
    """2026-06-27 P8 Task 3.1: juggle_cmd_threads._cleanup_orphaned_threads scans
    `nodes` (kind='conversation', state='running') with no busy agent, not
    threads.status='running'. Pre-flip it hits `FROM threads` and finds nothing,
    so no orphan action item is filed."""
    from juggle_cmd_threads import _cleanup_orphaned_threads
    db = _fresh(tmp_path)
    with db._connect() as conn:
        seed_node(conn, id="r1", kind="conversation", title="stuck-topic",
                  state="running", user_label="Q")
        conn.commit()
    _cleanup_orphaned_threads(db)
    items = db.get_open_action_items()
    assert any(it["thread_id"] == "r1" and "stuck-topic" in it["message"]
               for it in items)


def test_conv_create_writes_exactly_one_authoritative_node(tmp_path):
    """2026-06-27 P8 c3-write-cut: a conversation create writes exactly ONE
    authoritative row in `nodes` (kind='conversation')."""
    db = _fresh(tmp_path)
    tid = db.create_thread("alpha", session_id="s")
    with db._connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE id=? AND kind='conversation'", (tid,)
        ).fetchone()[0]
    assert n == 1


def test_conv_mirror_fails_loud_on_missing_column():
    """2026-06-27 P8 c3-write-cut/H4: a missing nodes COLUMN must FAIL LOUD (a real
    schema gap), not be swallowed by a blanket except. A missing nodes TABLE
    (pre-Migration-44) is still tolerated (returns without writing)."""
    import sqlite3
    from dbops.conv_node_mirror import mirror_conv_insert
    # (a) nodes table ABSENT -> tolerated, no raise.
    conn = sqlite3.connect(":memory:")
    mirror_conv_insert(conn, "t1", topic="x", session_id="s",
                       user_label="AA", now="2026-06-27 00:00")
    # (b) nodes table present but missing columns the mirror writes -> RAISES.
    conn.execute("CREATE TABLE nodes (id TEXT, kind TEXT, title TEXT)")
    with pytest.raises(sqlite3.OperationalError):
        mirror_conv_insert(conn, "t2", topic="x", session_id="s",
                           user_label="AA", now="2026-06-27 00:00")


def test_static_legacy_ref_floor_ratchet():
    """Ratchet: live legacy-table refs in shipped src must not climb back above
    the current floor (60). Lowered from the 2026-06-23 floor (123) → 107 (Task 3.1
    direct-read flips) → 103 (T-c3-reads: project-keyed conversation reads) → 63
    (2026-06-29 c4-topic-dag: the topic-tier + DAG + orphan readers flipped to
    nodes/node_edges, db_mirror.py + all its graph_topics-projection writes DELETED,
    and the topic claim/sweep/reconcile state writers routed through the nodes-
    authoritative state_write helper) → 60 (2026-06-29 c4-write-cut PARTIAL: add_node
    graph_tasks/graph_edges write-cut, and the sanctioned p8_reverse_backfill inverse
    excluded from the steady-state scan).

    The residual 60 are NOT reachable to 0 by this node alone: the graph-family
    (~22: db_graph/db_topics _TASK_ONLY/_TOPIC_ONLY discriminator subqueries +
    create_task/create_topic/set_* writes, graph_status/cockpit/graph_load/
    graph_dispatch) is blocked on the topic/task kind discriminator that node c6
    (Task 6.2 / M2) owns — a topic vs a bare task cannot be separated by
    `parent_id IS NULL` alone (2026-06-29 incident). The threads-family (~38:
    threads.py + projects/slug_alloc/messages/cockpit_model/monitor_daemon/
    watchdog/cmd_agents_lifecycle get_thread/get_all_threads consumers, plus the
    dead juggle_migrate_lifecycle.py the terminal drop deletes) is the conversation
    collapse — independent of the discriminator but reverted once (a300e30) for a
    KeyError cascade across ~30 consumers/71 tests. This pin may only ever be lowered."""
    from pathlib import Path
    from dbops.p8_readiness import scan_legacy_refs
    src_root = Path(__file__).resolve().parent.parent / "src"
    assert len(scan_legacy_refs(src_root)) <= 60
