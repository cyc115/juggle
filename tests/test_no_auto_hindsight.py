"""
TDD: Verify removal of auto recall+retain hooks and thread.summary column.

These tests MUST FAIL before the implementation is applied.
"""

import argparse
import importlib
import os
import sqlite3
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

SRC_DIR = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(db_path=str(tmp_path / "test.db"))
    db.init_db()
    return db


def _monkeypatch_db(monkeypatch, db):
    import juggle_cli_common as common
    monkeypatch.setattr(common, "get_db", lambda: db)


# ---------------------------------------------------------------------------
# A. handle_user_prompt_submit spawns NO retain thread
# ---------------------------------------------------------------------------

def test_prompt_submit_no_retain_thread(tmp_path, monkeypatch):
    """handle_user_prompt_submit must not start any Hindsight retain thread."""
    db = _make_db(tmp_path)
    db.set_active(True)
    tid = db.create_thread("test topic", session_id="s1")
    db.set_current_thread(tid)

    import juggle_hooks_config
    monkeypatch.setattr(juggle_hooks_config, "is_active", lambda: True)
    monkeypatch.setattr(juggle_hooks_config, "get_db", lambda: db)
    monkeypatch.setenv("JUGGLE_IS_AGENT", "")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")

    import juggle_hooks_prompt as hp
    monkeypatch.setattr(hp, "is_active", lambda: True)
    monkeypatch.setattr(hp, "get_db", lambda: db)
    monkeypatch.setattr(hp, "_autopilot_context", lambda: "")
    monkeypatch.setattr(hp, "auto_approve_blocked_agents", lambda: None)
    monkeypatch.setattr(hp, "build_context_string", lambda: "")

    retain_calls = []

    def _fake_retain(*a, **kw):
        retain_calls.append((a, kw))

    # If _retain_conversation_turn exists, patch it
    if hasattr(hp, "_retain_conversation_turn"):
        monkeypatch.setattr(hp, "_retain_conversation_turn", _fake_retain)

    started_threads = []
    real_thread_init = threading.Thread.__init__

    def _track_thread(self, *a, target=None, **kw):
        started_threads.append(target)
        real_thread_init(self, *a, target=target, **kw)

    monkeypatch.setattr(threading.Thread, "__init__", _track_thread)

    with pytest.raises(SystemExit):
        hp.handle_user_prompt_submit({"prompt": "hello world - a long prompt text"})

    # No thread with a Hindsight retain target should be spawned
    retain_targets = [t for t in started_threads if t is not None and "retain" in getattr(t, "__name__", "")]
    assert retain_targets == [], f"Retain threads were spawned: {retain_targets}"


# ---------------------------------------------------------------------------
# A. handle_stop spawns NO retain thread
# ---------------------------------------------------------------------------

def test_handle_stop_no_retain_thread(tmp_path, monkeypatch):
    """handle_stop must not start any Hindsight retain thread."""
    db = _make_db(tmp_path)
    db.set_active(True)
    tid = db.create_thread("test topic", session_id="s1")
    db.set_current_thread(tid)

    import juggle_hooks_config
    monkeypatch.setattr(juggle_hooks_config, "is_active", lambda: True)
    monkeypatch.setattr(juggle_hooks_config, "get_db", lambda: db)

    import juggle_hooks_prompt as hp
    monkeypatch.setattr(hp, "is_active", lambda: True)
    monkeypatch.setattr(hp, "get_db", lambda: db)

    started_threads = []
    real_thread_init = threading.Thread.__init__

    def _track_thread(self, *a, target=None, **kw):
        started_threads.append(target)
        real_thread_init(self, *a, target=target, **kw)

    monkeypatch.setattr(threading.Thread, "__init__", _track_thread)

    # scan_class_b_fn no-op
    with pytest.raises(SystemExit):
        hp.handle_stop(
            {"last_assistant_message": "Done. Everything is working correctly."},
            lambda data: None,
        )

    retain_targets = [t for t in started_threads if t is not None and "retain" in getattr(t, "__name__", "")]
    assert retain_targets == [], f"Retain threads were spawned in handle_stop: {retain_targets}"


# ---------------------------------------------------------------------------
# B. cmd_create_thread triggers NO Hindsight recall
# ---------------------------------------------------------------------------

def test_create_thread_no_recall(tmp_path, monkeypatch):
    """cmd_create_thread must not call HindsightClient.recall or HindsightClient.reflect."""
    db = _make_db(tmp_path)
    db.set_active(True)
    _monkeypatch_db(monkeypatch, db)

    import juggle_cli_common as common
    monkeypatch.setattr(common, "_generate_title_for_thread", lambda *a, **kw: None)

    recall_calls = []
    reflect_calls = []

    class FakeHindsightClient:
        @classmethod
        def from_config(cls, *a, **kw):
            return cls()

        def recall(self, *a, **kw):
            recall_calls.append(a)
            return ""

        def reflect(self, *a, **kw):
            reflect_calls.append(a)
            return ""

    # Patch everywhere it could be imported from
    monkeypatch.setattr("juggle_cli_common._get_hindsight_client", lambda: FakeHindsightClient())

    from juggle_cmd_threads import cmd_create_thread

    args = argparse.Namespace(topic="improve dispatch")
    cmd_create_thread(args)

    assert recall_calls == [], f"Hindsight.recall was called: {recall_calls}"
    assert reflect_calls == [], f"Hindsight.reflect was called: {reflect_calls}"


# ---------------------------------------------------------------------------
# B. cmd_complete accepts --retain flag without error, no Hindsight write
# ---------------------------------------------------------------------------

def test_complete_agent_retain_flag_noop(tmp_path, monkeypatch):
    """complete-agent --retain must not crash and must NOT call Hindsight."""
    db = _make_db(tmp_path)
    db.set_active(True)
    _monkeypatch_db(monkeypatch, db)
    tid = db.create_thread("test topic", session_id="s1")
    db.update_thread(tid, status="running")
    db._set_session_key_external("session_id", "s1")

    retain_calls = []

    class FakeHindsightClient:
        def retain(self, *a, **kw):
            retain_calls.append(a)

    monkeypatch.setattr("juggle_cli_common._get_hindsight_client", lambda: FakeHindsightClient())
    monkeypatch.setattr("juggle_cmd_agents_common._get_hindsight_client", lambda: FakeHindsightClient())

    # Stub out integrate / graph calls
    monkeypatch.setattr("juggle_cmd_agents_complete._com._finalize_worktree", lambda t: (True, "ok"))
    monkeypatch.setattr("juggle_cmd_agents_complete._com.juggle_cmd_integrate._run_integrate",
                        lambda t, d: (True, "ok"))
    monkeypatch.setattr("juggle_cmd_agents_graph.enforce_handoff_contract", lambda *a, **kw: None)
    monkeypatch.setattr("juggle_cmd_agents_graph_topics.enforce_topic_gate", lambda *a, **kw: None)
    monkeypatch.setattr("juggle_cmd_agents_graph.close_adhoc_run", lambda *a, **kw: None)
    monkeypatch.setattr("juggle_cmd_agents_graph_topics.mark_graph_topic", lambda *a, **kw: None)

    from juggle_cmd_agents_complete import cmd_complete_agent

    args = argparse.Namespace(
        thread_id=tid,
        result_summary="done",
        retain_text="some non-obvious learning",
        open_questions=None,
        handoff=None,
        role=None,
    )
    cmd_complete_agent(args)

    assert retain_calls == [], f"Hindsight.retain was called from complete-agent: {retain_calls}"


# ---------------------------------------------------------------------------
# C. cmd_retain still writes to Hindsight (KEPT)
# ---------------------------------------------------------------------------

def test_cmd_retain_still_writes_hindsight(tmp_path, monkeypatch):
    """cmd_retain must still call Hindsight.retain (this path is kept)."""
    retain_calls = []

    class FakeHindsightClient:
        def retain(self, content, context=None):
            retain_calls.append((content, context))

    import juggle_cmd_context
    monkeypatch.setattr(juggle_cmd_context, "_get_hindsight_client", lambda: FakeHindsightClient())

    from juggle_cmd_context import cmd_retain

    args = argparse.Namespace(content="learned: always do X", context="learnings")
    cmd_retain(args)

    assert len(retain_calls) == 1
    assert "learned: always do X" in retain_calls[0][0]


# ---------------------------------------------------------------------------
# D. Migration 39: drops 4 cols, keeps summarized_msg_count + rows, idempotent
# ---------------------------------------------------------------------------

def _seed_old_schema(conn):
    """Create threads table with the 4 to-be-dropped columns + data."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY,
            label TEXT,
            user_label TEXT,
            topic TEXT,
            status TEXT DEFAULT 'active',
            summary TEXT DEFAULT '',
            memory_context TEXT DEFAULT '',
            memory_loaded INTEGER DEFAULT 0,
            last_reflect_msg_count INTEGER DEFAULT 0,
            summarized_msg_count INTEGER DEFAULT 0,
            created_at TEXT,
            last_active TEXT,
            session_id TEXT,
            show_in_list INTEGER DEFAULT 1
        );
        INSERT INTO threads (id, label, topic, status, summary, memory_context,
                             memory_loaded, last_reflect_msg_count, summarized_msg_count)
        VALUES ('uuid-1', 'A', 'topic A', 'active', 'old summary', 'ctx', 1, 5, 10);
        INSERT INTO threads (id, label, topic, status, summary, memory_context,
                             memory_loaded, last_reflect_msg_count, summarized_msg_count)
        VALUES ('uuid-2', 'B', 'topic B', 'done', '', '', 0, 0, 3);
    """)
    conn.commit()


def _run_migration_41(conn):
    """Import and run migration 41 against the given connection."""
    from dbops import migrations_recent
    migrations_recent.run_migration_41(conn)


def test_migration_41_drops_dead_columns(tmp_path):
    """Migration 41 must drop the 4 dead columns, keep summarized_msg_count and rows."""
    db_path = tmp_path / "mig_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _seed_old_schema(conn)

    _run_migration_41(conn)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()}
    assert "summary" not in cols, "summary column should be dropped"
    assert "memory_context" not in cols, "memory_context column should be dropped"
    assert "memory_loaded" not in cols, "memory_loaded column should be dropped"
    assert "last_reflect_msg_count" not in cols, "last_reflect_msg_count should be dropped"
    assert "summarized_msg_count" in cols, "summarized_msg_count must be preserved"

    rows = conn.execute("SELECT id, summarized_msg_count FROM threads ORDER BY id").fetchall()
    assert len(rows) == 2
    assert dict(rows[0])["summarized_msg_count"] == 10
    assert dict(rows[1])["summarized_msg_count"] == 3
    conn.close()


def test_migration_41_idempotent(tmp_path):
    """Running migration 41 twice must not error and must be a no-op on second run."""
    db_path = tmp_path / "mig_idem.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    _seed_old_schema(conn)

    _run_migration_41(conn)
    _run_migration_41(conn)  # second run — must not crash or corrupt

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(threads)").fetchall()}
    assert "summary" not in cols
    assert "summarized_msg_count" in cols
    conn.close()


# ---------------------------------------------------------------------------
# E. Grep-guard: dropped symbols must not appear in source
# ---------------------------------------------------------------------------

def test_no_retain_conversation_turn_in_source():
    """_retain_conversation_turn must not exist in juggle_hooks_prompt after removal."""
    source = (SRC_DIR / "juggle_hooks_prompt.py").read_text()
    assert "_retain_conversation_turn" not in source, \
        "_retain_conversation_turn still referenced in juggle_hooks_prompt.py"


def test_no_auto_recall_in_threads():
    """_auto_recall must not exist in juggle_cmd_threads after removal."""
    source = (SRC_DIR / "juggle_cmd_threads.py").read_text()
    assert "_auto_recall" not in source, \
        "_auto_recall still referenced in juggle_cmd_threads.py"


def test_no_recall_for_thread_in_context_startup():
    """_recall_for_thread must not exist in juggle_context_startup after removal."""
    source = (SRC_DIR / "juggle_context_startup.py").read_text()
    assert "_recall_for_thread" not in source, \
        "_recall_for_thread still in juggle_context_startup.py"


def test_no_cmd_recall_in_context():
    """cmd_recall / cmd_recall_if_cold / cmd_recall_bg must not exist in juggle_cmd_context."""
    source = (SRC_DIR / "juggle_cmd_context.py").read_text()
    assert "def cmd_recall(" not in source, "cmd_recall still in juggle_cmd_context.py"
    assert "def cmd_recall_if_cold(" not in source
    assert "def cmd_recall_bg(" not in source


def test_no_update_summary_in_threads():
    """cmd_update_summary must not exist in juggle_cmd_threads after removal."""
    source = (SRC_DIR / "juggle_cmd_threads.py").read_text()
    assert "def cmd_update_summary(" not in source, \
        "cmd_update_summary still in juggle_cmd_threads.py"


def test_no_hindsight_retain_in_complete_agent():
    """Auto-retain block (threading.Thread retain) must not exist in cmd_complete_agent."""
    source = (SRC_DIR / "juggle_cmd_agents_complete.py").read_text()
    assert "_do_retain" not in source, \
        "_do_retain (Hindsight retain thread) still in juggle_cmd_agents_complete.py"
