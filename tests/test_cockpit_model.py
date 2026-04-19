import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest
from juggle_cockpit_model import Topic, Action, Agent, Notification, CockpitState
import time

def test_topic_dataclass():
    t = Topic(id="K", label="K", status="current", age_secs=60, is_current=True)
    assert t.id == "K"
    assert t.is_current is True

def test_action_dataclass():
    a = Action(id="q1", topic_id="K", text="approve plan", tier=2, age_secs=300)
    assert a.tier == 2

def test_agent_dataclass():
    ag = Agent(id_short="ab12", role="coder", status="busy", topic_id="K", age_secs=720)
    assert ag.status == "busy"

def test_notification_dataclass():
    n = Notification(text="planner finished", kind="complete", age_secs=30)
    assert n.kind == "complete"

def test_cockpit_state_dataclass():
    state = CockpitState(
        topics=[Topic(id="K", label="K", status="current", age_secs=60, is_current=True)],
        actions=[],
        agents=[],
        notifications=[],
        fetched_at=time.time(),
    )
    assert len(state.topics) == 1

def test_frozen_immutable():
    t = Topic(id="K", label="K", status="current", age_secs=60, is_current=True)
    with pytest.raises(Exception):
        t.id = "Z"  # frozen dataclass must raise


# ---------------------------------------------------------------------------
# format_age tests
# ---------------------------------------------------------------------------
from juggle_cockpit_model import format_age

def test_format_age_seconds():
    assert format_age(45) == "45s"

def test_format_age_minutes():
    assert format_age(125) == "2m"

def test_format_age_hours():
    assert format_age(7200) == "2h"

def test_format_age_days():
    assert format_age(86400 * 2) == "2d"

def test_format_age_none():
    assert format_age(None) == "—"

def test_format_age_zero():
    assert format_age(0) == "0s"

def test_format_age_boundary_60():
    assert format_age(60) == "1m"

def test_format_age_boundary_3600():
    assert format_age(3600) == "1h"

def test_format_age_boundary_86400():
    assert format_age(86400) == "1d"


# ---------------------------------------------------------------------------
# priority_tier tests
# ---------------------------------------------------------------------------
from juggle_cockpit_model import priority_tier

def test_priority_tier_blocker():
    assert priority_tier(
        agent_result="⚠️ BLOCKER: needs auth token",
        status="active",
        last_active_age_secs=100,
        is_current=False,
    ) == 0

def test_priority_tier_review():
    assert priority_tier(
        agent_result="Plan complete.",
        status="done",
        last_active_age_secs=600,
        is_current=False,
        reviewed=False,
    ) == 1

def test_priority_tier_background():
    assert priority_tier(
        agent_result=None,
        status="background",
        last_active_age_secs=200,
        is_current=False,
    ) == 2

def test_priority_tier_current():
    assert priority_tier(
        agent_result=None,
        status="active",
        last_active_age_secs=10,
        is_current=True,
    ) == 3

def test_priority_tier_idle_by_age():
    assert priority_tier(
        agent_result=None,
        status="active",
        last_active_age_secs=8000,
        is_current=False,
    ) == 5

def test_priority_tier_done():
    assert priority_tier(
        agent_result=None,
        status="done",
        last_active_age_secs=100,
        is_current=False,
    ) == 6

def test_priority_tier_done_already_reviewed():
    assert priority_tier(
        agent_result="Plan complete.",
        status="done",
        last_active_age_secs=100,
        is_current=False,
        reviewed=True,
    ) == 6


# ---------------------------------------------------------------------------
# snapshot tests
# ---------------------------------------------------------------------------
import sqlite3
import time as _time
from datetime import datetime, timezone
from juggle_cockpit_model import snapshot, CockpitState

def _make_in_memory_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE threads (
          id TEXT PRIMARY KEY, label TEXT, user_label TEXT,
          session_id TEXT NOT NULL DEFAULT '',
          topic TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
          summary TEXT DEFAULT '', key_decisions TEXT DEFAULT '[]',
          open_questions TEXT DEFAULT '[]', last_user_intent TEXT DEFAULT '',
          agent_task_id TEXT, agent_result TEXT,
          show_in_list INTEGER NOT NULL DEFAULT 1,
          summarized_msg_count INTEGER NOT NULL DEFAULT 0,
          title TEXT DEFAULT '', reviewed INTEGER DEFAULT 0,
          created_at TEXT NOT NULL, last_active TEXT NOT NULL,
          last_active_at TEXT
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
        CREATE TABLE notifications_v2 (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          thread_id TEXT, message TEXT NOT NULL,
          created_at TEXT NOT NULL, session_id TEXT NOT NULL
        );
        CREATE TABLE action_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          thread_id TEXT, message TEXT NOT NULL,
          type TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal',
          created_at TEXT NOT NULL, dismissed_at TEXT
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO settings(key, value) VALUES('thread_auto_archive_ttl_secs', '86400');
        CREATE TABLE session (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO session(key, value) VALUES('active', '1');
        INSERT INTO session(key, value) VALUES('current_thread', 'thread-001');
        INSERT INTO session(key, value) VALUES('session_id', 'test-session-001');
    """)
    now = datetime.now(timezone.utc).isoformat()
    now_min = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    conn.execute(
        "INSERT INTO threads(id, label, user_label, session_id, topic, status, summary, "
        "key_decisions, open_questions, last_user_intent, agent_task_id, agent_result, "
        "show_in_list, summarized_msg_count, title, reviewed, created_at, last_active, last_active_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("thread-001", "K", "K", "", "cockpit UI refactor", "active",
         "", "[]", "[]", "", None, None, 1, 0, "cockpit", 0, now, now, now_min)
    )
    conn.execute(
        "INSERT INTO agents VALUES(?,?,?,?,?,?,?,?)",
        ("abcd1234-0000-0000-0000-000000000000", "coder", "juggle:1",
         "thread-001", "busy", "[]", now, now)
    )
    conn.execute(
        "INSERT INTO notifications(thread_id, message, delivered, severity, created_at) VALUES(?,?,?,?,?)",
        ("thread-001", "plan v3 ready", 0, "info", now)
    )
    conn.execute(
        "INSERT INTO notifications_v2(thread_id, message, created_at, session_id) VALUES(?,?,?,?)",
        ("thread-001", "plan v3 ready", now_min, "test-session-001")
    )
    conn.commit()
    return conn

class _FakeDB:
    def __init__(self, conn):
        self._conn = conn
    def _connect(self):
        return self._conn

def test_snapshot_returns_cockpit_state():
    conn = _make_in_memory_db()
    db = _FakeDB(conn)
    state = snapshot(db)
    assert isinstance(state, CockpitState)

def test_snapshot_topics_populated():
    conn = _make_in_memory_db()
    db = _FakeDB(conn)
    state = snapshot(db)
    assert len(state.topics) == 1
    assert state.topics[0].label == "K"
    assert state.topics[0].is_current is True

def test_snapshot_agents_populated():
    conn = _make_in_memory_db()
    db = _FakeDB(conn)
    state = snapshot(db)
    assert len(state.agents) == 1
    assert state.agents[0].role == "coder"
    assert state.agents[0].status == "busy"

def test_snapshot_notifications_populated():
    conn = _make_in_memory_db()
    db = _FakeDB(conn)
    state = snapshot(db)
    assert len(state.notifications) == 1
    assert "plan v3 ready" in state.notifications[0].text

def test_snapshot_fetched_at_is_recent():
    conn = _make_in_memory_db()
    db = _FakeDB(conn)
    before = _time.time()
    state = snapshot(db)
    after = _time.time()
    assert before <= state.fetched_at <= after
