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


def test_get_thread_resolves_from_nodes_with_node_vocab(tmp_path):
    """2026-06-29 P8 Task 4.2 (final conv read-collapse): get_thread reads the
    authoritative kind='conversation' node and returns NODE vocab — state/title/
    last_active_at — with the legacy status/topic/last_active keys GONE (Q1, no
    shim). Pre-flip it hit `FROM threads` and a node-only seed found nothing."""
    db = _fresh(tmp_path)
    with db._connect() as conn:
        seed_node(conn, id="c1", kind="conversation", title="hello",
                  state="open", last_active_at="2026-06-29 01:00", user_label="A")
        conn.commit()
    t = db.get_thread("c1")
    assert t is not None
    assert t["state"] == "open" and t["title"] == "hello"
    assert t["last_active_at"] == "2026-06-29 01:00"
    # legacy aliases are gone — node vocab only (no dual-vocab shim):
    assert "status" not in t and "topic" not in t and "last_active" not in t


def test_get_thread_ignores_non_conversation_nodes(tmp_path):
    """A kind='task' node sharing the id space must NOT resolve as a thread."""
    db = _fresh(tmp_path)
    with db._connect() as conn:
        seed_node(conn, id="k1", kind="task", title="a task", state="open")
        conn.commit()
    assert db.get_thread("k1") is None


def test_get_all_threads_reads_conversation_nodes(tmp_path):
    """2026-06-29 P8 Task 4.2: get_all_threads enumerates kind='conversation'
    nodes (node vocab), not `threads`. Task/topic nodes are excluded."""
    db = _fresh(tmp_path)
    with db._connect() as conn:
        seed_node(conn, id="c1", kind="conversation", title="one", state="open",
                  created_at="2026-01-01 00:00")
        seed_node(conn, id="c2", kind="conversation", title="two", state="done",
                  created_at="2026-01-02 00:00")
        seed_node(conn, id="k1", kind="task", title="task", state="open")
        conn.commit()
    rows = db.get_all_threads()
    ids = [r["id"] for r in rows]
    assert ids == ["c1", "c2"]            # task excluded, ordered by created_at
    assert all("status" not in r and "topic" not in r for r in rows)


def test_get_thread_by_user_label_live_first_from_nodes(tmp_path):
    """2026-06-29 P8 Task 4.2: get_thread_by_user_label resolves a slug to the
    NEWEST live conversation node (state in open/running/background), then the
    newest terminal holder — reading `nodes`, not `threads`."""
    db = _fresh(tmp_path)
    with db._connect() as conn:
        seed_node(conn, id="old_live", kind="conversation", title="live",
                  state="open", user_label="Z", created_at="2026-01-01 00:00")
        seed_node(conn, id="new_dead", kind="conversation", title="dead",
                  state="archived", user_label="Z", created_at="2026-01-03 00:00")
        conn.commit()
    t = db.get_thread_by_user_label("z")   # case-insensitive; live wins over newer-dead
    assert t is not None and t["id"] == "old_live"


def test_get_threads_by_status_filters_node_state(tmp_path):
    """2026-06-29 P8 Task 4.2: get_threads_by_status now filters nodes.state
    (node vocab) — callers pass a node state value (e.g. 'running', 'done')."""
    db = _fresh(tmp_path)
    with db._connect() as conn:
        seed_node(conn, id="r1", kind="conversation", title="r", state="running")
        seed_node(conn, id="d1", kind="conversation", title="d", state="done")
        conn.commit()
    assert [t["id"] for t in db.get_threads_by_status("running")] == ["r1"]
    assert [t["id"] for t in db.get_threads_by_status("done")] == ["d1"]


def test_static_legacy_ref_floor_ratchet():
    """Ratchet: live legacy-table refs in shipped src must not climb back above
    the current floor (9). Lowered from the 2026-06-23 floor (123) → 107 (Task 3.1
    direct-read flips) → 103 (T-c3-reads: project-keyed conversation reads) → 63
    (2026-06-29 c4-topic-dag: the topic-tier + DAG + orphan readers flipped to
    nodes/node_edges, db_mirror.py + all its graph_topics-projection writes DELETED,
    and the topic claim/sweep/reconcile state writers routed through the nodes-
    authoritative state_write helper) → 60 (2026-06-29 c4-write-cut PARTIAL: add_node
    graph_tasks/graph_edges write-cut, and the sanctioned p8_reverse_backfill inverse
    excluded from the steady-state scan) → 47 (2026-06-29 c4-conv-reads: the
    get_thread/get_all_threads/get_thread_by_user_label/get_threads_by_status/
    get_archive_candidates + dedup readers flipped to kind='conversation' nodes,
    and the ~24 threads-family consumers adopted node vocab — status→state,
    topic→title, last_active→last_active_at; writes stay dual-write) → 9
    (2026-06-29 c4-writes-collapse: the LAST collapse before the legacy-table DROP —
    db_graph/db_topics create_task/create_topic/set_* + state_write stopped
    dual-writing graph_tasks/graph_topics; graph_status/cockpit/graph_load/cmd_graph
    + graph_dispatch project discovery read the node store; threads.create_thread/
    update_thread/set_thread_status/touch_last_active/archive/unarchive cut every
    `threads` write (nodes is the sole conversation store, slug live-set + cap +
    junk-topic resolve from nodes, idx_nodes_live_label enforces slug uniqueness);
    messages/projects/cockpit_model/monitor_daemon/slug_alloc flipped; the
    threads-index repair relocated into the excluded migration layer).

    The residual 9 are ALL in the dead juggle_migrate_lifecycle.py — the one-shot
    legacy lifecycle backfill the terminal-drop node (Task 6.3) deletes together
    with the DROP TABLE migration. No steady-state code path reaches a legacy table
    any longer. This pin may only ever be lowered."""
    from pathlib import Path
    from dbops.p8_readiness import scan_legacy_refs
    src_root = Path(__file__).resolve().parent.parent / "src"
    assert len(scan_legacy_refs(src_root)) <= 9
