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


def test_static_legacy_ref_floor_ratchet():
    """Ratchet: live legacy-table refs in shipped src must not climb back above
    the 2026-06-23 floor (123). The residual 123 are blocked (lossy status map,
    incomplete conversation dual-write, no graph-task-state dual-write, deferred
    db_mirror/migrate_lifecycle deletions). This pin may only ever be lowered."""
    from pathlib import Path
    from dbops.p8_readiness import scan_legacy_refs
    src_root = Path(__file__).resolve().parent.parent / "src"
    assert len(scan_legacy_refs(src_root)) <= 123
