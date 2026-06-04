"""Tests for project close/open/list feature."""
import sys
from pathlib import Path
from unittest.mock import patch
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def make_db(tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(str(tmp_path / "test.db"))
    db.init_db()
    return db


# --- Migration: closed_at + summary on projects ---

def test_migration_adds_closed_at_column(tmp_path):
    db = make_db(tmp_path)
    with db._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    assert "closed_at" in cols


def test_migration_adds_summary_column_to_projects(tmp_path):
    db = make_db(tmp_path)
    with db._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    assert "summary" in cols


def test_migration_idempotent_closed_at(tmp_path):
    from juggle_db import JuggleDB
    path = str(tmp_path / "test.db")
    db1 = JuggleDB(path); db1.init_db()
    db2 = JuggleDB(path); db2.init_db()
    with db2._connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(projects)").fetchall()}
    assert "closed_at" in cols
    assert "summary" in cols


# --- close_project ---

def test_close_project_sets_status_closed(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    db.close_project(pid, "project summary", {})
    assert db.get_project(pid)["status"] == "closed"


def test_close_project_sets_closed_at(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    db.close_project(pid, "summary", {})
    assert db.get_project(pid)["closed_at"] is not None


def test_close_project_writes_project_summary(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    db.close_project(pid, "the project summary", {})
    assert db.get_project(pid)["summary"] == "the project summary"


def test_close_project_hides_threads(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    tid = db.create_thread("topic", session_id="s1")
    db.update_thread(tid, project_id=pid)
    db.close_project(pid, "summary", {tid: "thread summary"})
    assert db.get_thread(tid)["show_in_list"] == 0


def test_close_project_writes_thread_summary(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    tid = db.create_thread("topic", session_id="s1")
    db.update_thread(tid, project_id=pid)
    db.close_project(pid, "project summary", {tid: "my thread summary"})
    assert db.get_thread(tid)["summary"] == "my thread summary"


def test_close_project_inbox_guard_raises(tmp_path):
    db = make_db(tmp_path)
    with pytest.raises(Exception):
        db.close_project("INBOX", "summary", {})


def test_close_project_releases_busy_agents(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    tid = db.create_thread("topic", session_id="s1")
    db.update_thread(tid, project_id=pid)
    agent_id = db.create_agent(role="coder", pane_id="p1")
    db.update_agent(agent_id, status="busy", assigned_thread=tid)
    db.close_project(pid, "summary", {tid: "thread summary"})
    agent = db.get_agent(agent_id)
    assert agent["status"] == "idle"
    assert agent["assigned_thread"] is None


def test_close_project_only_releases_project_agents(tmp_path):
    """Agent on a different project's thread must not be released."""
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    pid2 = db.create_project(name="Other", objective="Other")
    tid_other = db.create_thread("other topic", session_id="s1")
    db.update_thread(tid_other, project_id=pid2)
    agent_id = db.create_agent(role="coder", pane_id="p2")
    db.update_agent(agent_id, status="busy", assigned_thread=tid_other)
    db.close_project(pid, "summary", {})
    assert db.get_agent(agent_id)["status"] == "busy"


# --- open_project ---

def test_open_project_restores_status_active(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    db.close_project(pid, "summary", {})
    db.open_project(pid)
    assert db.get_project(pid)["status"] == "active"


def test_open_project_clears_closed_at(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    db.close_project(pid, "summary", {})
    db.open_project(pid)
    assert db.get_project(pid)["closed_at"] is None


def test_open_project_restores_thread_visibility(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    tid = db.create_thread("topic", session_id="s1")
    db.update_thread(tid, project_id=pid)
    db.close_project(pid, "summary", {tid: "thread summary"})
    db.open_project(pid)
    assert db.get_thread(tid)["show_in_list"] == 1


# --- list_projects_with_state ---

def test_list_projects_with_state_includes_closed(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    db.close_project(pid, "done", {})
    ids = [p["id"] for p in db.list_projects_with_state()]
    assert pid in ids


def test_list_projects_with_state_includes_active(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Active", objective="Still going")
    ids = [p["id"] for p in db.list_projects_with_state()]
    assert pid in ids


def test_list_projects_with_state_has_required_fields(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    db.close_project(pid, "done summary", {})
    p = next(x for x in db.list_projects_with_state() if x["id"] == pid)
    for field in ("id", "name", "status", "summary", "last_active", "closed_at", "thread_count"):
        assert field in p, f"missing field: {field}"


def test_list_projects_with_state_thread_count(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    tid = db.create_thread("topic", session_id="s1")
    db.update_thread(tid, project_id=pid)
    p = next(x for x in db.list_projects_with_state() if x["id"] == pid)
    assert p["thread_count"] >= 1


# --- get_active_projects excludes closed ---

def test_get_active_projects_excludes_closed(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Closed Work", objective="Do work")
    db.close_project(pid, "summary", {})
    active_ids = [p["id"] for p in db.get_active_projects()]
    assert pid not in active_ids


def test_get_active_projects_includes_active(tmp_path):
    db = make_db(tmp_path)
    pid = db.create_project(name="Active Work", objective="Do work")
    active_ids = [p["id"] for p in db.get_active_projects()]
    assert pid in active_ids


# --- summarize_project ---

def test_summarize_project_calls_llm_per_thread_then_project(tmp_path):
    from juggle_project_summary import summarize_project
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    tid1 = db.create_thread("topic A", session_id="s1")
    tid2 = db.create_thread("topic B", session_id="s1")
    db.update_thread(tid1, project_id=pid)
    db.update_thread(tid2, project_id=pid)
    db.add_message(tid1, "user", "hello from A")
    db.add_message(tid2, "user", "hello from B")

    calls = []
    def mock_llm(prompt):
        calls.append(prompt)
        return "mocked summary"

    proj_summary, thread_summaries = summarize_project(db, pid, llm_fn=mock_llm)
    # 2 per-thread + 1 overall = 3 calls
    assert len(calls) == 3
    assert isinstance(proj_summary, str) and len(proj_summary) > 0
    assert tid1 in thread_summaries
    assert tid2 in thread_summaries


def test_summarize_project_returns_llm_output_as_thread_summary(tmp_path):
    from juggle_project_summary import summarize_project
    db = make_db(tmp_path)
    pid = db.create_project(name="Work", objective="Do work")
    tid = db.create_thread("topic A", session_id="s1")
    db.update_thread(tid, project_id=pid)
    db.add_message(tid, "user", "some content")

    responses = iter(["thread summary X", "overall project Y"])
    def mock_llm(prompt):
        return next(responses)

    proj_summary, thread_summaries = summarize_project(db, pid, llm_fn=mock_llm)
    assert thread_summaries[tid] == "thread summary X"
    assert proj_summary == "overall project Y"


def test_summarize_project_no_threads(tmp_path):
    from juggle_project_summary import summarize_project
    db = make_db(tmp_path)
    pid = db.create_project(name="Empty", objective="Nothing yet")

    calls = []
    def mock_llm(prompt):
        calls.append(prompt)
        return "empty project summary"

    proj_summary, thread_summaries = summarize_project(db, pid, llm_fn=mock_llm)
    assert thread_summaries == {}
    assert isinstance(proj_summary, str)
    # With no threads, should still produce project summary (1 LLM call)
    assert len(calls) == 1
