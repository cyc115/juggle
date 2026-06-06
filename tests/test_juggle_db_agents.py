"""Tests for JuggleDB agents table and related methods."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


def test_agents_table_exists(db):
    with db._connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "agents" in tables


def test_create_agent_returns_uuid(db):
    import re

    agent_id = db.create_agent(role="coder", pane_id="%3")
    assert re.match(r"^[0-9a-f-]{36}$", agent_id)


def test_create_agent_defaults(db):
    agent_id = db.create_agent(role="researcher", pane_id="%1")
    agent = db.get_agent(agent_id)
    assert agent["role"] == "researcher"
    assert agent["pane_id"] == "%1"
    assert agent["status"] == "idle"
    assert agent["assigned_thread"] is None
    assert json.loads(agent["context_threads"]) == []


def test_get_agent_not_found(db):
    assert db.get_agent("nonexistent-uuid") is None


def test_get_all_agents_empty(db):
    assert db.get_all_agents() == []


def test_get_all_agents_returns_all(db):
    db.create_agent(role="coder", pane_id="%1")
    db.create_agent(role="planner", pane_id="%2")
    agents = db.get_all_agents()
    assert len(agents) == 2


def test_update_agent_status(db):
    agent_id = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(agent_id, status="busy")
    assert db.get_agent(agent_id)["status"] == "busy"


def test_update_agent_context_threads_list(db):
    agent_id = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(agent_id, context_threads=["thread-uuid-1", "thread-uuid-2"])
    agent = db.get_agent(agent_id)
    assert json.loads(agent["context_threads"]) == ["thread-uuid-1", "thread-uuid-2"]


def test_delete_agent(db):
    agent_id = db.create_agent(role="coder", pane_id="%1")
    db.delete_agent(agent_id)
    assert db.get_agent(agent_id) is None


def test_get_best_agent_none_when_empty(db):
    assert db.get_best_agent("thread-1") is None


def test_get_best_agent_returns_idle(db):
    agent_id = db.create_agent(role="researcher", pane_id="%1")
    result = db.get_best_agent("thread-1")
    assert result is not None
    assert result["id"] == agent_id


def test_get_best_agent_skips_busy(db):
    agent_id = db.create_agent(role="researcher", pane_id="%1")
    db.update_agent(agent_id, status="busy")
    assert db.get_best_agent("thread-1") is None


def test_get_best_agent_prefers_context_match(db):
    _a1 = db.create_agent(role="coder", pane_id="%1")
    a2 = db.create_agent(role="coder", pane_id="%2")
    # a2 has worked on thread-1 before
    db.update_agent(a2, context_threads=["thread-1"])
    result = db.get_best_agent("thread-1")
    assert result["id"] == a2


def test_get_best_agent_prefers_role_match(db):
    _a1 = db.create_agent(role="researcher", pane_id="%1")
    a2 = db.create_agent(role="coder", pane_id="%2")
    result = db.get_best_agent("thread-1", role="coder")
    assert result["id"] == a2


def test_get_best_agent_context_beats_role(db):
    a1 = db.create_agent(role="researcher", pane_id="%1")
    _a2 = db.create_agent(role="coder", pane_id="%2")
    # a1 has context for this thread (score=2), a2 has role match (score=1)
    db.update_agent(a1, context_threads=["thread-1"])
    result = db.get_best_agent("thread-1", role="coder")
    assert result["id"] == a1


def test_get_best_agent_signature_has_no_domain():
    """get_best_agent must not accept a domain kwarg after 1.21.0 cleanup."""
    import inspect
    from juggle_db import JuggleDB

    sig = inspect.signature(JuggleDB.get_best_agent)
    assert "domain" not in sig.parameters


# ── Fix 1: repo_path on agents ──────────────────────────────────────────────

def test_agent_repo_path_stored_on_create(db):
    agent_id = db.create_agent(role="coder", pane_id="%1", repo_path="/home/user/myproject")
    agent = db.get_agent(agent_id)
    assert agent["repo_path"] == "/home/user/myproject"


def test_agent_repo_path_nullable(db):
    agent_id = db.create_agent(role="coder", pane_id="%1")
    agent = db.get_agent(agent_id)
    assert agent["repo_path"] is None


def test_ranked_idle_agents_includes_repo_path(db):
    db.create_agent(role="coder", pane_id="%1", repo_path="/repo/a")
    agents = db.get_ranked_idle_agents("thread-1")
    assert agents[0].get("repo_path") == "/repo/a"
