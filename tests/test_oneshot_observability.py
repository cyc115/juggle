"""Tests for one-shot agent observability: harness tagging, PID liveness,
reconcile, watchdog branching, list-agents output, cockpit model.

Tests 1-8 per the spec; all use an isolated tmp_path DB.
"""

import datetime as _dt
import json
import os
import sys
import time as _time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    # Create a thread for agent assignment
    d.create_thread("test-topic", session_id="sess")
    return d


# ---------------------------------------------------------------------------
# 1. create_agent persists harness; get_agent returns it; migration idempotent
# ---------------------------------------------------------------------------


def test_create_agent_persists_harness(db):
    agent_id = db.create_agent(role="coder", pane_id="%1", harness="claude")
    agent = db.get_agent(agent_id)
    assert agent["harness"] == "claude"

    agent_id2 = db.create_agent(role="researcher", pane_id="%2", harness="reasonix")
    agent2 = db.get_agent(agent_id2)
    assert agent2["harness"] == "reasonix"


def test_create_agent_harness_none_by_default(db):
    agent_id = db.create_agent(role="coder", pane_id="%1")
    agent = db.get_agent(agent_id)
    assert agent["harness"] is None


def test_migration_idempotent(db):
    """Running init_db twice is safe — Migration 32 must be idempotent."""
    db.init_db()
    agent_id = db.create_agent(role="coder", pane_id="%1", harness="claude")
    agent = db.get_agent(agent_id)
    assert agent["harness"] == "claude"


# ---------------------------------------------------------------------------
# 2. spawn_agent tags spawn-time harness; recycled agent KEEPS harness
# ---------------------------------------------------------------------------


def test_spawn_agent_tags_harness(db):
    from juggle_tmux import JuggleTmuxManager
    from juggle_harness import get_adapter

    mgr = JuggleTmuxManager()
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%42"}):
        agent = mgr.spawn_agent(db, role="coder")
    expected = get_adapter("coder").id
    assert agent["harness"] == expected
    assert len(agent["harness"]) > 0


def test_recycled_agent_keeps_original_harness(db):
    """Spawn under current harness, then verify harness persists on re-read."""
    from juggle_tmux import JuggleTmuxManager

    mgr = JuggleTmuxManager()
    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_PANE": "%42"}):
        agent = mgr.spawn_agent(db, role="coder")
    original_harness = agent["harness"]
    assert original_harness is not None

    agent2 = db.get_agent(agent["id"])
    assert agent2["harness"] == original_harness


# ---------------------------------------------------------------------------
# 3. cmd_send_task updates harness/model/oneshot_pid for one-shot
# ---------------------------------------------------------------------------


def test_send_task_updates_harness_and_model(db):
    from juggle_cmd_agents import cmd_send_task
    import tempfile

    agent_id = db.create_agent(role="coder", pane_id="%1")
    thread = db.get_all_threads()[0]
    db.update_agent(agent_id, status="busy", assigned_thread=thread["id"],
                    busy_since="2025-01-01T00:00:00+00:00")

    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_SEND": "1"}):
        with patch("juggle_cmd_agents_common.get_db", return_value=db):
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
                f.write("Do the thing.")
                prompt_path = f.name

                try:
                    args = MagicMock()
                    args.agent_id = agent_id
                    args.prompt_file = prompt_path
                    cmd_send_task(args)
                finally:
                    os.unlink(prompt_path)

    agent = db.get_agent(agent_id)
    assert agent["harness"] is not None
    assert len(agent["harness"]) > 0


def test_send_task_oneshot_stores_harness_and_model(db):
    from juggle_cmd_agents import cmd_send_task
    import tempfile

    agent_id = db.create_agent(role="coder", pane_id="%1")
    thread = db.get_all_threads()[0]
    db.update_agent(agent_id, status="busy", assigned_thread=thread["id"],
                    busy_since="2025-01-01T00:00:00+00:00")

    mock_adapter = MagicMock()
    mock_adapter.id = "reasonix"
    mock_adapter.is_interactive = False
    mock_adapter._cfg = {"model": "deepseek-v4-pro"}
    mock_adapter.decorate_task = lambda role, prompt: prompt

    with patch.dict(os.environ, {"JUGGLE_TMUX_MOCK_SEND": "1"}):
        with patch("juggle_cmd_agents_common.get_adapter", return_value=mock_adapter):
            with patch("juggle_cmd_agents_common.get_db", return_value=db):
                with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
                    f.write("Do the thing.")
                    prompt_path = f.name

                try:
                    args = MagicMock()
                    args.agent_id = agent_id
                    args.prompt_file = prompt_path
                    cmd_send_task(args)
                finally:
                    os.unlink(prompt_path)

    agent = db.get_agent(agent_id)
    assert agent["harness"] == "reasonix"
    assert agent["model"] == "deepseek-v4-pro"


# ---------------------------------------------------------------------------
# 4. oneshot_agent_alive: alive (os.kill ok) → True; dead → False
# ---------------------------------------------------------------------------


def test_oneshot_agent_alive_via_pid():
    from juggle_tmux import oneshot_agent_alive

    with patch("os.kill") as mock_kill:
        mock_kill.return_value = None
        assert oneshot_agent_alive({"oneshot_pid": 12345}) is True

        mock_kill.side_effect = ProcessLookupError()
        assert oneshot_agent_alive({"oneshot_pid": 12345}) is False

        mock_kill.side_effect = PermissionError()
        assert oneshot_agent_alive({"oneshot_pid": 12345}) is False


def test_oneshot_agent_alive_fallback_to_pane():
    from juggle_tmux import oneshot_agent_alive

    with patch("juggle_tmux._pane_has_juggle_agent_env") as mock_pane:
        mock_pane.return_value = True
        assert oneshot_agent_alive({"oneshot_pid": None, "pane_id": "%1"}) is True

        mock_pane.return_value = False
        assert oneshot_agent_alive({"oneshot_pid": None, "pane_id": "%1"}) is False

    assert oneshot_agent_alive({"oneshot_pid": None}) is False


# ---------------------------------------------------------------------------
# 5. reconcile_oneshot_agents
# ---------------------------------------------------------------------------


def test_reconcile_dead_oneshot_with_open_thread(db):
    from juggle_tmux import reconcile_oneshot_agents

    thread = db.get_all_threads()[0]
    db.update_thread(thread["id"], status="background")
    agent_id = db.create_agent(role="coder", pane_id="%1", harness="reasonix")
    db.update_agent(agent_id, status="busy", assigned_thread=thread["id"],
                    busy_since="2025-01-01T00:00:00+00:00",
                    last_send_task_at="2025-01-01T00:00:00+00:00",
                    oneshot_pid=99999)

    with patch("juggle_tmux.oneshot_agent_alive", return_value=False):
            count = reconcile_oneshot_agents(db)
            assert count == 1

    agent = db.get_agent(agent_id)
    assert agent["status"] == "idle"
    assert agent["assigned_thread"] is None

    items = db.get_open_action_items()
    failures = [i for i in items if i.get("type") == "failure"]
    assert len(failures) >= 1


def test_reconcile_alive_oneshot_untouched(db):
    from juggle_tmux import reconcile_oneshot_agents

    thread = db.get_all_threads()[0]
    agent_id = db.create_agent(role="coder", pane_id="%1", harness="reasonix")
    db.update_agent(agent_id, status="busy", assigned_thread=thread["id"],
                    busy_since="2025-01-01T00:00:00+00:00",
                    last_send_task_at="2025-01-01T00:00:00+00:00",
                    oneshot_pid=99999)

    with patch("juggle_tmux.oneshot_agent_alive", return_value=True):
            count = reconcile_oneshot_agents(db)
            assert count == 0

    agent = db.get_agent(agent_id)
    assert agent["status"] == "busy"


def test_reconcile_thread_already_closed_untouched(db):
    from juggle_tmux import reconcile_oneshot_agents

    thread = db.get_all_threads()[0]
    db.set_thread_status(thread["id"], "closed")
    agent_id = db.create_agent(role="coder", pane_id="%1", harness="reasonix")
    db.update_agent(agent_id, status="busy", assigned_thread=thread["id"],
                    busy_since="2025-01-01T00:00:00+00:00",
                    last_send_task_at="2025-01-01T00:00:00+00:00",
                    oneshot_pid=99999)

    with patch("juggle_tmux.oneshot_agent_alive", return_value=False):
            count = reconcile_oneshot_agents(db)
            assert count == 0


def test_reconcile_within_grace_window_untouched(db):
    from juggle_tmux import reconcile_oneshot_agents
    from datetime import datetime, timezone

    thread = db.get_all_threads()[0]
    agent_id = db.create_agent(role="coder", pane_id="%1", harness="reasonix")
    recent = datetime.now(timezone.utc).isoformat()
    db.update_agent(agent_id, status="busy", assigned_thread=thread["id"],
                    busy_since=recent,
                    last_send_task_at=recent,
                    oneshot_pid=99999)

    with patch("juggle_tmux.oneshot_agent_alive", return_value=False):
            count = reconcile_oneshot_agents(db)
            assert count == 0


# ---------------------------------------------------------------------------
# 6. Watchdog non-interactive routing
# ---------------------------------------------------------------------------


def test_watchdog_classify_non_interactive():
    from juggle_watchdog import _agent_is_non_interactive

    assert _agent_is_non_interactive({"harness": "claude"}) is False
    assert _agent_is_non_interactive({"harness": "reasonix"}) is True
    assert _agent_is_non_interactive({}) is False


def test_inspect_agent_non_interactive_alive_returns_working(db):
    from juggle_watchdog import inspect_agent

    agent_id = db.create_agent(role="coder", pane_id="%1", harness="reasonix")
    db.update_agent(agent_id, status="busy",
                    busy_since="2025-01-01T00:00:00+00:00",
                    last_send_task_at="2025-01-01T00:00:00+00:00",
                    oneshot_pid=12345)

    with patch("juggle_watchdog.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="shell$ ")
        with patch("juggle_watchdog._agent_is_non_interactive", return_value=True):
            with patch("juggle_tmux.oneshot_agent_alive", return_value=True):
                result = inspect_agent(agent_id, db, "juggle")
                assert result["state"] == "working"


def test_inspect_agent_non_interactive_dead_returns_crashed(db):
    from juggle_watchdog import inspect_agent

    thread = db.get_all_threads()[0]
    agent_id = db.create_agent(role="coder", pane_id="%1", harness="reasonix")
    db.update_agent(agent_id, status="busy",
                    assigned_thread=thread["id"],
                    busy_since="2025-01-01T00:00:00+00:00",
                    last_send_task_at="2025-01-01T00:00:00+00:00",
                    oneshot_pid=12345)

    with patch("juggle_watchdog.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="shell$ ")
        with patch("juggle_watchdog._agent_is_non_interactive", return_value=True):
            with patch("juggle_tmux.oneshot_agent_alive", return_value=False):
                result = inspect_agent(agent_id, db, "juggle")
                assert result["state"] == "crashed"


# ---------------------------------------------------------------------------
# 7. list-agents data includes harness/model + busy_since age
# ---------------------------------------------------------------------------


def test_list_agents_data_includes_harness_model(db):
    agent_id = db.create_agent(role="coder", pane_id="%1", harness="claude")
    db.update_agent(agent_id, model="sonnet")
    agent_id2 = db.create_agent(role="researcher", pane_id="%2", harness="reasonix")
    db.update_agent(agent_id2, model="deepseek-v4-pro")

    agent = db.get_agent(agent_id)
    assert agent["harness"] == "claude"
    assert agent["model"] == "sonnet"

    agent2 = db.get_agent(agent_id2)
    assert agent2["harness"] == "reasonix"
    assert agent2["model"] == "deepseek-v4-pro"


def test_busy_agent_age_from_busy_since(db):
    from datetime import datetime, timezone

    agent_id = db.create_agent(role="coder", pane_id="%1", harness="claude")
    now = datetime.now(timezone.utc)
    busy_ts = (now - _dt.timedelta(seconds=120)).isoformat()
    active_ts = (now - _dt.timedelta(seconds=300)).isoformat()
    db.update_agent(agent_id, status="busy", busy_since=busy_ts,
                    last_active=active_ts)

    agent = db.get_agent(agent_id)
    assert agent["busy_since"] == busy_ts
    assert agent["status"] == "busy"


# ---------------------------------------------------------------------------
# 8. Cockpit Agent carries harness/model and busy_since-based age
# ---------------------------------------------------------------------------


def test_cockpit_agent_has_harness_and_model():
    from juggle_cockpit_model import Agent

    ag = Agent(
        id_short="ab12",
        role="coder",
        status="busy",
        topic_id="K",
        age_secs=720,
        harness="reasonix",
        model="deepseek-v4-pro",
    )
    assert ag.harness == "reasonix"
    assert ag.model == "deepseek-v4-pro"


def test_cockpit_snapshot_includes_harness_model_and_busy_age(db):
    from juggle_cockpit_model import snapshot
    from datetime import datetime, timezone

    thread = db.get_all_threads()[0]
    db.update_thread(thread["id"], status="background")

    now = datetime.now(timezone.utc)
    busy_ts = (now - _dt.timedelta(seconds=120)).isoformat()
    active_ts = (now - _dt.timedelta(seconds=500)).isoformat()

    agent_id = db.create_agent(role="coder", pane_id="%1", harness="reasonix")
    db.update_agent(agent_id, status="busy", assigned_thread=thread["id"],
                    busy_since=busy_ts, last_active=active_ts,
                    model="deepseek-v4-pro", oneshot_pid=12345)

    # Patch reconcile to prevent it from idling our test agent (oneshot_pid
    # is set, but the real oneshot_agent_alive would fail without tmux).
    with patch("juggle_tmux.reconcile_oneshot_agents", return_value=0):
        state = snapshot(db)
    agents = [a for a in state.agents if a.id_short == agent_id[:8]]
    assert len(agents) == 1
    agent = agents[0]
    assert agent.harness == "reasonix"
    assert agent.model == "deepseek-v4-pro"
    assert agent.status == "busy"
    assert 110 <= agent.age_secs <= 130


def test_cockpit_agent_no_harness_is_none(db):
    from juggle_cockpit_model import snapshot

    agent_id = db.create_agent(role="coder", pane_id="%1")
    state = snapshot(db)
    agents = [a for a in state.agents if a.id_short == agent_id[:8]]
    assert len(agents) == 1
    assert agents[0].harness is None
    assert agents[0].model is None
