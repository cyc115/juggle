"""Bug2 (2026-07-20): recycled-label collision beyond resolve_thread_id_for_spool.

Migration 75 only enforces the partial-unique index on user_label among
NON-ARCHIVED nodes — archived siblings keep sharing a label with whichever
non-archived node currently holds it (T-slug-wheel: slugs persist on archive,
no recycling-by-erasure). Any label->node resolver that does not order
live-state-first before falling back to recency silently resolves to a STALE
ARCHIVED node instead of the one live node.

This mirrors the SAME root cause already fixed in
juggle_spool_cli_common.py::resolve_thread_id_for_spool (Bug#1 attempt#5) and
pins it across every other label->node resolver in the tree:

  - juggle_cli_common.py::_resolve_thread
  - juggle_cmd_integrate.py::_resolve_thread (delegates to the above)
  - juggle_cockpit_helpers.py::_resolve_thread_by_label (in-memory variant)
  - dbops/threads.py::get_thread_by_user_label (the canonical DB chokepoint)

The live node is created FIRST (earliest) and the archived nodes are created
AFTER it, so a naive `ORDER BY created_at DESC` (or a bare `LIMIT 1` with no
ordering, or an in-memory `next()` linear scan) resolves to the wrong,
more-recently-created ARCHIVED node — only an explicit live-state-first
ordering picks the correct live holder.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import juggle_cli_common as cli_common
import juggle_cmd_integrate as cmd_integrate
from juggle_cockpit_helpers import _resolve_actions_by_thread_label, _resolve_thread_by_label
from juggle_db import JuggleDB

LABEL = "ZZ"


@pytest.fixture
def collision(tmp_path, monkeypatch):
    """1 live node + 3 archived nodes, all sharing user_label='ZZ'; live created
    earliest."""
    db_path = tmp_path / "test.db"
    db = JuggleDB(str(db_path))
    db.init_db()

    live = db.create_thread("Live holder", session_id="s1")

    archived = []
    for i in range(3):
        tid = db.create_thread(f"Old holder {i}", session_id="s1")
        db.archive_thread(tid)
        archived.append(tid)

    with db._connect() as conn:
        for node_id in [live, *archived]:
            conn.execute(
                "UPDATE nodes SET user_label = ? WHERE id = ? AND kind='conversation'",
                (LABEL, node_id),
            )
        conn.commit()

    monkeypatch.setattr("juggle_cli_common.DB_PATH", db_path, raising=False)
    monkeypatch.setattr("juggle_cli_common._db_path", lambda: db_path, raising=False)

    return db, live, archived


def test_db_get_thread_by_user_label_prefers_live(collision):
    db, live, _archived = collision
    resolved = db.get_thread_by_user_label(LABEL)
    assert resolved["id"] == live


def test_cli_common_resolve_thread_prefers_live(collision):
    db, live, _archived = collision
    resolved = cli_common._resolve_thread(db, LABEL)
    assert resolved == live


def test_cmd_integrate_resolve_thread_prefers_live(collision):
    db, live, _archived = collision
    resolved = cmd_integrate._resolve_thread(db, LABEL)
    assert resolved == live


def test_cockpit_resolve_thread_by_label_prefers_live(collision):
    db, live, archived = collision
    # Archived entries listed BEFORE the live entry — a naive `next()` linear
    # scan would return the first (archived) match instead of the live one.
    threads = [
        {"id": tid, "user_label": LABEL, "state": "archived"} for tid in archived
    ] + [{"id": live, "user_label": LABEL, "state": "open"}]

    resolved = _resolve_thread_by_label(threads, LABEL)
    assert resolved is not None
    assert resolved["id"] == live


# ---------------------------------------------------------------------------
# Bug 5877 (2026-07-31): action lookup/ack must span ALL same-label nodes,
# not just the one `_resolve_thread_by_label` picks. A label names a
# conversation, and every archived sibling that ever held a recycled label
# keeps it — actions can be attached to any of them.
# ---------------------------------------------------------------------------


def test_resolve_actions_by_thread_label_finds_actions_on_archived_only_siblings():
    """All-archived siblings (no live node) -- action on an arbitrary archived
    sibling must still be found. This is bug 5877 exactly: 26 SR nodes, all
    archived, action 5877 attached to one of them."""
    threads = [
        {"id": "arch-1", "user_label": LABEL, "state": "archived"},
        {"id": "arch-2", "user_label": LABEL, "state": "archived"},
        {"id": "arch-3", "user_label": LABEL, "state": "archived"},
    ]
    open_actions = [{"id": 5877, "thread_id": "arch-2", "message": "do thing"}]

    result = _resolve_actions_by_thread_label(threads, open_actions, LABEL)

    assert [a["id"] for a in result] == [5877]


def test_resolve_actions_by_thread_label_spans_multiple_same_label_siblings():
    """Actions spread across MULTIPLE same-label siblings are all returned together."""
    threads = [
        {"id": "arch-1", "user_label": LABEL, "state": "archived"},
        {"id": "arch-2", "user_label": LABEL, "state": "archived"},
    ]
    open_actions = [
        {"id": 1, "thread_id": "arch-1", "message": "a"},
        {"id": 2, "thread_id": "arch-2", "message": "b"},
        {"id": 3, "thread_id": "other", "message": "unrelated"},
    ]

    result = _resolve_actions_by_thread_label(threads, open_actions, LABEL)

    assert {a["id"] for a in result} == {1, 2}


def test_resolve_actions_by_thread_label_live_and_archived_both_returned():
    """Regression: when a live node exists alongside archived siblings, actions
    on BOTH are still returned -- the fix must not narrow existing behavior."""
    threads = [
        {"id": "arch-1", "user_label": LABEL, "state": "archived"},
        {"id": "live-1", "user_label": LABEL, "state": "open"},
    ]
    open_actions = [
        {"id": 1, "thread_id": "arch-1", "message": "a"},
        {"id": 2, "thread_id": "live-1", "message": "b"},
    ]

    result = _resolve_actions_by_thread_label(threads, open_actions, LABEL)

    assert {a["id"] for a in result} == {1, 2}


def test_resolve_actions_by_thread_label_no_matching_node_returns_empty():
    """A label with no matching node still returns []."""
    threads = [{"id": "arch-1", "user_label": LABEL, "state": "archived"}]
    open_actions = [{"id": 1, "thread_id": "arch-1", "message": "a"}]

    assert _resolve_actions_by_thread_label(threads, open_actions, "QQ") == []
