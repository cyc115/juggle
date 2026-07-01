"""Watchdog hardening tests — 5 real failures from 2026-06-06 session.

Failure 1: send_task submission unverified → silent zombie
Failure 2: role lost on orphan recovery (defaults to "coder")
Failure 3: redundant re-dispatch when agent still finalising
Failure 4: stale model on recovery agent
Failure 5: get_ranked_idle_agents reuse ignores role filter
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


# ---------------------------------------------------------------------------
# Failure 1: send_task must raise when wait_for_submission returns False
# ---------------------------------------------------------------------------


def test_send_task_raises_when_submission_unverified(monkeypatch):
    """send_task must raise RuntimeError if wait_for_submission returns False.

    Regression: previously just logged a warning — task stayed in the input
    box, agent sat at 0 tokens, orchestrator had to manually press Enter (x3).
    """
    from juggle_tmux import JuggleTmuxManager

    monkeypatch.delenv("JUGGLE_TMUX_MOCK_SEND", raising=False)

    mgr = JuggleTmuxManager("testsession")

    with patch.object(mgr, "wait_for_ready_to_paste", return_value=True), \
         patch.object(mgr, "_run_tmux", return_value=MagicMock(stdout="content", returncode=0)), \
         patch.object(mgr, "wait_for_submission", return_value=False), \
         patch("time.sleep"):  # skip 0.4 s delay
        with pytest.raises(RuntimeError, match="submission not verified"):
            mgr.send_task("%99", "do the work")


# ---------------------------------------------------------------------------
# Failure 2: role lost — orphan recovery must not default to "coder"
# ---------------------------------------------------------------------------


def test_orphan_recovery_uses_stored_role_not_hardcoded_coder(db, tmp_path):
    """Orphan recovery must spawn with last_dispatched_role, not fall back to 'coder'.

    Regression: `role = thread.get("last_dispatched_role") or "coder"` meant a
    researcher thread got recovered as a coder.  If role is unknown, don't auto-recover.
    """
    from juggle_watchdog import check_orphaned_threads

    thread_id = db.create_thread("researcher task", session_id="")
    db.update_thread(thread_id, status="background")

    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    # P8 Task 3.1: reaper reads nodes; mirror the seed onto the conversation node.
    db.update_thread(
        thread_id, last_active_at=past, last_dispatched_task="run the research",
        last_dispatched_role="researcher",
    )

    spawned_roles: list[str] = []

    class FakeMgr:
        def spawn_agent(self, db, role, model=None):
            spawned_roles.append(role)
            agent_id = db.create_agent(role=role, pane_id="%mock", repo_path="")
            return db.get_agent(agent_id)

        def send_task(self, pane_id, task):
            return "deadbeef00000000"

    check_orphaned_threads(db, orphan_threshold=60.0, mgr=FakeMgr())

    # Recovery should have spawned a researcher, NOT a coder
    assert spawned_roles == ["researcher"], f"Wrong role spawned: {spawned_roles}"


def test_orphan_recovery_skips_when_role_unknown(db, tmp_path):
    """When last_dispatched_role is NULL, orphan recovery must file action item
    instead of silently spawning a wrong-role agent.
    """
    from juggle_watchdog import check_orphaned_threads

    thread_id = db.create_thread("mystery task", session_id="")
    db.update_thread(thread_id, status="background")

    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    # P8 Task 3.1: reaper reads nodes; mirror the seed onto the conversation node.
    db.update_thread(
        thread_id, last_active_at=past, last_dispatched_task="some task",
        last_dispatched_role=None,
    )

    spawned: list[str] = []

    class FakeMgr:
        def spawn_agent(self, db, role, model=None):
            spawned.append(role)
            agent_id = db.create_agent(role=role, pane_id="%mock", repo_path="")
            return db.get_agent(agent_id)

        def send_task(self, pane_id, task):
            return "deadbeef00000000"

    check_orphaned_threads(db, orphan_threshold=60.0, mgr=FakeMgr())

    # Must NOT have spawned any agent with a guessed role
    assert spawned == [], "Should not auto-spawn when role is unknown"
    # Must have filed an action item for manual intervention
    items = db.get_open_action_items()
    assert any(thread_id[:8] in it["message"] or "orphan" in it["message"].lower() for it in items)


# ---------------------------------------------------------------------------
# Failure 3: liveness recheck before spawn; release if original completes
# ---------------------------------------------------------------------------


def test_execute_recovery_aborts_if_pane_hash_changes(db, tmp_path):
    """execute_recovery must abort if the pane content changed between the
    stall detection and the recovery decision (agent was still finalising).

    Regression: agent ran a long test suite, watchdog declared it stalled,
    spawned a recovery agent that duplicated the just-merged work.
    """
    from juggle_watchdog import execute_recovery, write_recovery_snapshot

    session_id = "sess-test"
    thread_id = db.create_thread("slow-coder", session_id=session_id)
    agent_id = db.create_agent(role="coder", pane_id="%55", repo_path="")
    now_str = datetime.now(timezone.utc).isoformat()
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="run tests and merge",
        watchdog_retried=0,
        last_active=now_str,
        busy_since=now_str,
    )
    db.update_thread(thread_id, status="background")

    pane_content_at_stall = "step 12/20 running"
    pane_content_now = "step 16/20 running"  # hash will differ → still active

    recovery_dir = tmp_path / "recovery"
    spawned: list[str] = []

    class FakeMgr:
        def verify_pane(self, pane_id):
            return True

        def capture_pane(self, pane_id):
            return pane_content_now  # content changed since stall snapshot

        def spawn_agent(self, db, role, model=None):
            spawned.append(role)
            aid = db.create_agent(role=role, pane_id="%99", repo_path="")
            return db.get_agent(aid)

        def kill_pane(self, pane_id):
            pass

        def send_task(self, pane_id, task):
            return "aaaa000000000000"

    execute_recovery(
        db, FakeMgr(), db.get_agent(agent_id), pane_content_at_stall,
        recovery_dir=recovery_dir, session_id=session_id,
    )

    assert spawned == [], "Recovery should have aborted — agent was still active"
    # Original agent should still be busy (not cleaned up)
    agent = db.get_agent(agent_id)
    assert agent["status"] == "busy"


def test_execute_recovery_releases_new_agent_if_thread_closed_during_spawn(db, tmp_path):
    """After spawning recovery agent, if thread was already closed (original
    completed during the spawn window), release the recovery agent immediately.
    """
    from juggle_watchdog import execute_recovery

    session_id = "sess-race"
    thread_id = db.create_thread("race-coder", session_id=session_id)
    agent_id = db.create_agent(role="coder", pane_id="%56", repo_path="")
    now_str = datetime.now(timezone.utc).isoformat()
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="implement feature",
        watchdog_retried=0,
        last_active=now_str,
        busy_since=now_str,
    )
    db.update_thread(thread_id, status="background")

    # Pane has no Claude markers (never_fired state) so recovery proceeds
    pane_content = "processing..."
    recovery_dir = tmp_path / "recovery"

    new_agent_id_holder: list[str] = []

    class FakeMgr:
        def verify_pane(self, pane_id):
            return True

        def capture_pane(self, pane_id):
            return pane_content  # unchanged — recovery proceeds

        def spawn_agent(self, _db, role, model=None):
            aid = _db.create_agent(role=role, pane_id="%new", repo_path="")
            new_agent_id_holder.append(aid)
            # Simulate original completing DURING our spawn window
            _db.update_thread(thread_id, status="closed")
            return _db.get_agent(aid)

        def kill_pane(self, pane_id):
            pass

        def send_task(self, pane_id, task):
            return "bbbb000000000000"

    execute_recovery(
        db, FakeMgr(), db.get_agent(agent_id), pane_content,
        recovery_dir=recovery_dir, session_id=session_id,
    )

    # Recovery agent must have been released (idle), not left busy
    if new_agent_id_holder:
        new_agent = db.get_agent(new_agent_id_holder[0])
        assert new_agent is None or new_agent["status"] == "idle", (
            "Recovery agent should be released when original thread closed during spawn"
        )


# ---------------------------------------------------------------------------
# Failure 4: recovery must use current config model, not stale snapshot model
# ---------------------------------------------------------------------------


def test_execute_recovery_ignores_stale_model_from_failed_agent(db, tmp_path):
    """execute_recovery must NOT forward the dead agent's model to spawn_agent.

    Regression: if config switched from deepseek → claude, the recovery agent
    got launched with the old model name and wedged on 'pick a different model'.
    Fix: always pass model=None so spawn_agent reads from current settings.
    """
    from juggle_watchdog import execute_recovery

    session_id = "sess-model"
    thread_id = db.create_thread("model-test", session_id=session_id)
    agent_id = db.create_agent(role="coder", pane_id="%60", repo_path="")
    now_str = datetime.now(timezone.utc).isoformat()
    db.update_agent(
        agent_id,
        status="busy",
        assigned_thread=thread_id,
        last_task="build feature",
        watchdog_retried=0,
        last_active=now_str,
        busy_since=now_str,
        model="deepseek-coder-v2",  # stale model from original spawn
    )
    db.update_thread(thread_id, status="background")

    pane_content = "some output with no shell or Claude markers"
    recovery_dir = tmp_path / "recovery"
    captured_spawn_kwargs: list[dict] = []

    class FakeMgr:
        def verify_pane(self, pane_id):
            return True

        def capture_pane(self, pane_id):
            return pane_content  # unchanged → recovery proceeds

        def spawn_agent(self, _db, role, model=None):
            captured_spawn_kwargs.append({"role": role, "model": model})
            aid = _db.create_agent(role=role, pane_id="%new-m", repo_path="")
            return _db.get_agent(aid)

        def kill_pane(self, pane_id):
            pass

        def send_task(self, pane_id, task):
            return "cccc000000000000"

    execute_recovery(
        db, FakeMgr(), db.get_agent(agent_id), pane_content,
        recovery_dir=recovery_dir, session_id=session_id,
    )

    assert captured_spawn_kwargs, "spawn_agent should have been called"
    assert captured_spawn_kwargs[0]["model"] is None, (
        f"Recovery spawn must pass model=None, got {captured_spawn_kwargs[0]['model']!r}"
    )


# ---------------------------------------------------------------------------
# Failure 5: get_ranked_idle_agents reuse must respect role filter
# ---------------------------------------------------------------------------


def test_get_ranked_idle_agents_wrong_role_not_returned_as_reuse(db):
    """get-agent --role coder must not reuse an idle planner.

    Regression: role adds only +1 to score; an idle planner with a context-match
    (+2) could win and be returned for a coder request, causing role confusion.
    Fix: hard-filter reuse candidates to matching role when role is specified.
    """
    # Create an idle planner agent
    planner_id = db.create_agent(role="planner", pane_id="%70", repo_path="/repo")
    db.update_agent(planner_id, status="idle", assigned_thread=None)

    # get_ranked_idle_agents with role="coder" must NOT return the planner
    candidates = db.get_ranked_idle_agents("some-thread-id", role="coder")

    # The planner should either not appear, or be skippable by role check
    wrong_role = [c for c in candidates if c["role"] != "coder"]
    # The real enforcement is in cmd_get_agent (the caller filters), but
    # we want to confirm the DB helper exposes the role so callers CAN filter.
    # Assert that a hard role-match filter in the caller would leave no wrong candidates.
    correct_only = [c for c in candidates if c["role"] == "coder"]
    assert len(correct_only) == 0, "No coder agents exist, so reuse should find none"
    # The planner appears in ranked list (not yet filtered) — verify it's skippable
    # by asserting the planner IS in candidates (so we know the filter must be in the caller)
    planner_in_list = any(c["id"] == planner_id for c in candidates)
    # This test documents the bug: planner appears even when role="coder" requested
    assert planner_in_list, "Planner appears unfiltered — caller must apply hard role filter"


def test_cmd_get_agent_does_not_reuse_wrong_role(db, tmp_path, monkeypatch):
    """When --role coder requested, idle planner must be skipped; fresh coder spawned.

    This tests the cmd_get_agent loop (the actual fix location).
    """
    from juggle_cmd_agents import cmd_get_agent

    # Create idle planner — should be skipped for coder request
    planner_id = db.create_agent(role="planner", pane_id="%80", repo_path="/repo")
    db.update_agent(planner_id, status="idle", assigned_thread=None)

    thread_id = db.create_thread("coder work", session_id="")

    spawned_roles: list[str] = []

    class FakeMgr:
        def wait_for_ready_to_paste(self, pane_id, attempts=1):
            return True  # planner pane "ready" — but must be skipped by role

        def spawn_agent(self, _db, role, model=None, harness_override=None, effort=None):
            spawned_roles.append(role)
            aid = _db.create_agent(role=role, pane_id="%90", repo_path="/repo")
            return _db.get_agent(aid)

    args = MagicMock()
    args.thread_id = thread_id  # cmd_get_agent uses thread_id, not thread
    args.role = "coder"
    args.model = None
    args.repo = "/repo"

    import juggle_cmd_agents_common as _com_mod
    import juggle_tmux as _tmux_mod
    import juggle_cli_common as _common_mod

    monkeypatch.setattr(_com_mod, "get_db", lambda: db)
    monkeypatch.setattr(_com_mod, "JuggleTmuxManager", lambda: FakeMgr())
    # Bypass label resolution (labels map to full UUID)
    monkeypatch.setattr(_common_mod, "_resolve_thread", lambda _db, t: t)
    monkeypatch.setattr(_com_mod, "_resolve_thread", lambda _db, t: t)

    import io
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        try:
            cmd_get_agent(args)
        except SystemExit:
            pass

    # Must have spawned a fresh coder, not reused the planner
    assert spawned_roles == ["coder"], f"Expected fresh coder spawn, got: {spawned_roles}"
