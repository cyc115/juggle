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


def test_static_legacy_ref_floor_ratchet():
    """Ratchet: live legacy-table refs in shipped src must not climb back above
    the 2026-06-23 floor (123). The residual 123 are blocked (lossy status map,
    incomplete conversation dual-write, no graph-task-state dual-write, deferred
    db_mirror/migrate_lifecycle deletions). This pin may only ever be lowered."""
    from pathlib import Path
    from dbops.p8_readiness import scan_legacy_refs
    src_root = Path(__file__).resolve().parent.parent / "src"
    assert len(scan_legacy_refs(src_root)) <= 123
