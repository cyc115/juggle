import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import sqlite3
import time as _time
from collections import namedtuple
from datetime import datetime, timezone

import pytest
from rich.layout import Layout

from juggle_cockpit_model import snapshot, CockpitState
from juggle_cockpit_view import build_layout, render_into, pick_breakpoint

Size = namedtuple("Size", ["width", "height"])


def _make_in_memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE threads (
          id TEXT PRIMARY KEY, label TEXT, session_id TEXT NOT NULL DEFAULT '',
          topic TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
          summary TEXT DEFAULT '', key_decisions TEXT DEFAULT '[]',
          open_questions TEXT DEFAULT '[]', last_user_intent TEXT DEFAULT '',
          agent_task_id TEXT, agent_result TEXT,
          show_in_list INTEGER NOT NULL DEFAULT 1,
          summarized_msg_count INTEGER NOT NULL DEFAULT 0,
          title TEXT DEFAULT '', reviewed INTEGER DEFAULT 0,
          created_at TEXT NOT NULL, last_active TEXT NOT NULL
        );
        CREATE TABLE agents (
          id TEXT PRIMARY KEY, role TEXT NOT NULL, pane_id TEXT NOT NULL,
          assigned_thread TEXT, status TEXT NOT NULL DEFAULT 'idle',
          context_threads TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL, last_active TEXT NOT NULL
        );
        CREATE TABLE notifications (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          thread_id TEXT NOT NULL, message TEXT NOT NULL,
          delivered INTEGER DEFAULT 0, severity TEXT DEFAULT 'action',
          created_at TEXT NOT NULL
        );
        CREATE TABLE session (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO session(key, value) VALUES('active', '1');
        INSERT INTO session(key, value) VALUES('current_thread', 'thread-001');
    """)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("thread-001", "K", "", "cockpit UI refactor", "active",
         "", "[]", '[]', "", None, None, 1, 0, "cockpit", 0, now, now)
    )
    conn.execute(
        "INSERT INTO agents VALUES(?,?,?,?,?,?,?,?)",
        ("abcd1234-0000-0000-0000-000000000000", "coder", "juggle:1",
         "thread-001", "busy", "[]", now, now)
    )
    conn.commit()
    return conn


class _FakeDB:
    def __init__(self, conn):
        self._conn = conn
    def _connect(self):
        return self._conn


# ---------------------------------------------------------------------------
# tick() tests
# ---------------------------------------------------------------------------

from juggle_cockpit import tick


def test_tick_returns_tuple():
    conn = _make_in_memory_db()
    db = _FakeDB(conn)
    size = Size(140, 40)
    layout, bp = tick(db, size, None, None)
    assert isinstance(layout, Layout)
    assert bp in ("wide", "medium", "narrow")


def test_tick_reuses_layout_when_bp_unchanged():
    conn = _make_in_memory_db()
    db = _FakeDB(conn)
    size = Size(140, 40)
    layout1, bp1 = tick(db, size, None, None)
    layout2, bp2 = tick(db, size, layout1, bp1)
    assert layout1 is layout2  # same object reused
    assert bp1 == bp2


def test_tick_rebuilds_layout_when_bp_changes():
    conn = _make_in_memory_db()
    db = _FakeDB(conn)
    layout_wide, bp_wide = tick(db, Size(140, 40), None, None)
    layout_medium, bp_medium = tick(db, Size(90, 40), layout_wide, bp_wide)
    assert bp_wide == "wide"
    assert bp_medium == "medium"
    assert layout_wide is not layout_medium


def test_run_uses_rich_live():
    """run() must use Rich Live, not raw sys.stdout writes."""
    import inspect
    import juggle_cockpit
    src = inspect.getsource(juggle_cockpit.run)
    assert "Live" in src
    assert "sys.stdout.write" not in src


def test_snapshot_to_render_pipeline():
    """Full pipeline: DB → snapshot → render_into → no exception."""
    conn = _make_in_memory_db()
    db = _FakeDB(conn)
    state = snapshot(db)
    assert isinstance(state, CockpitState)
    layout = build_layout("wide")
    render_into(layout, state, "wide")
    assert layout["actions"].renderable is not None
