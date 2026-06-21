"""Tests for Task 3 completion CLI commands."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "juggle.db"
    d = JuggleDB(db_path=str(path))
    d.init_db()
    # Force shared singleton used by cmd_* handlers to this test DB
    import juggle_cli_common as common

    monkeypatch.setattr(common, "get_db", lambda: d)
    return d


def test_add_notification_v2_creates_row(db):
    tid = db.create_thread("t", session_id="s")
    nid = db.add_notification_v2(thread_id=tid, message="merged PR", session_id="sess1")
    rows = db.get_notifications_for_session("sess1")
    assert len(rows) == 1
    assert rows[0]["message"] == "merged PR"


def test_add_action_item_creates_open_row(db):
    tid = db.create_thread("t", session_id="s")
    aid = db.add_action_item(
        thread_id=tid, message="push to prod", type_="manual_step", priority="high"
    )
    items = db.get_open_action_items()
    assert len(items) == 1
    assert items[0]["message"] == "push to prod"
    assert items[0]["priority"] == "high"


def test_dismiss_action_item(db):
    aid = db.add_action_item(
        thread_id=None, message="x", type_="question", priority="normal"
    )
    db.dismiss_action_item(aid)
    assert db.get_open_action_items() == []


def test_cmd_complete_agent_creates_notification_and_closes_thread(db, capsys):
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("t", session_id="sessA")
    # Set agent_task_id so the command recognises this as an agent-completed thread
    db.update_thread(tid, agent_task_id="task-1", status="running")
    db._set_session_key_external("session_id", "sessA")
    args = argparse.Namespace(
        thread_id=tid,
        result_summary="merged PR #412",
        retain_text=None,
        open_questions=None,
    )
    cmd_complete_agent(args)
    assert db.get_thread(tid)["status"] == "closed"
    notifs = db.get_notifications_for_session("sessA")
    assert any("merged PR #412" in n["message"] for n in notifs)


def test_cmd_complete_agent_preserves_feature_topic_with_user_messages(db):
    """Regression (2026-06-21): a transient/research agent bound to an active
    feature topic must NOT close that topic on complete-agent. Symptom:
    researcher 6238df03 closed Topic CQ on finish (had to unarchive).

    A thread with real user-authored messages is a user-facing feature topic,
    NOT an agent-owned ephemeral dispatch thread — complete-agent only
    auto-closes the latter.
    """
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("feature topic", session_id="sessA")
    # A real feature topic carries a user-authored message (added by the prompt
    # hook); ephemeral autopilot/delegate dispatch threads never do.
    db.add_message(tid, "user", "Please build the dashboard feature")
    # Agent dispatch set it to 'background' while wrongly bound to this topic.
    db.update_thread(tid, status="background")
    db._set_session_key_external("session_id", "sessA")
    args = argparse.Namespace(
        thread_id=tid, result_summary="research done", retain_text=None,
        open_questions=None,
    )
    cmd_complete_agent(args)
    # Feature topic must survive — not closed.
    assert db.get_thread(tid)["status"] != "closed"


def test_cmd_complete_agent_closes_thread_with_only_orchestrator_chatter(db):
    """Regression (2026-06-21): an agent/orchestrator thread whose only 'user'
    messages are automated chatter (task-notifications, '# Autonomous loop tick'
    headers) is NOT a feature topic and MUST close on complete-agent.

    Guards against the message-count false-positive: such chatter accumulates on
    any thread that was 'current' during loop ticks, so a plain user-message
    count would wrongly preserve a finished agent thread.
    """
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("agent task", session_id="sessA")
    db.add_message(tid, "user", "<task-notification>\n<task-id>abc</task-id>\n</task-notification>")
    db.add_message(tid, "user", "# Autonomous loop tick (dynamic pacing)\n\nRun the check.")
    db.update_thread(tid, status="background")
    db._set_session_key_external("session_id", "sessA")
    args = argparse.Namespace(
        thread_id=tid, result_summary="agent done", retain_text=None,
        open_questions=None,
    )
    cmd_complete_agent(args)
    # No real human message → agent-owned ephemeral thread → closes.
    assert db.get_thread(tid)["status"] == "closed"


def test_cmd_complete_agent_does_not_reopen_closed_feature_topic(db):
    """Idempotency (2026-06-21 Codex review): complete-agent on an ALREADY
    closed user-message thread must NOT resurrect it to 'active'. The preserve
    guard only un-hijacks an in-flight bind (background/running); a terminal
    status is left untouched so a duplicate/retry completion is a no-op.
    """
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("feature topic", session_id="sessA")
    db.add_message(tid, "user", "build the feature")
    db.set_thread_status(tid, "closed")  # already terminal
    db._set_session_key_external("session_id", "sessA")
    args = argparse.Namespace(
        thread_id=tid, result_summary="dup completion", retain_text=None,
        open_questions=None,
    )
    cmd_complete_agent(args)
    # Must stay closed — not reopened.
    assert db.get_thread(tid)["status"] == "closed"


def test_cmd_complete_agent_converts_open_questions_to_action_items(db):
    import json
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("t", session_id="sessA")
    db.update_thread(
        tid,
        agent_task_id="task-2",
        status="running",
        open_questions=json.dumps(["Push to prod?", "Also bump version?"]),
    )
    db._set_session_key_external("session_id", "sessA")
    args = argparse.Namespace(
        thread_id=tid, result_summary="done", retain_text=None, open_questions=None
    )
    cmd_complete_agent(args)
    items = db.get_open_action_items()
    assert len(items) == 2
    msgs = {i["message"] for i in items}
    assert msgs == {"Push to prod?", "Also bump version?"}
    # open_questions cleared
    assert json.loads(db.get_thread(tid)["open_questions"] or "[]") == []


def test_cmd_request_action_creates_action_item_keeps_state(db):
    from juggle_cmd_agents import cmd_request_action

    tid = db.create_thread("t", session_id="sessA")
    db.set_thread_status(tid, "running")
    args = argparse.Namespace(
        thread_id=tid,
        message="push to prod pending",
        type="manual_step",
        priority="high",
    )
    cmd_request_action(args)
    # Thread remains running; action_items row created
    assert db.get_thread(tid)["status"] == "running"
    items = db.get_open_action_items()
    assert len(items) == 1
    assert items[0]["message"] == "push to prod pending"
    assert items[0]["priority"] == "high"


def test_cmd_ack_action_dismisses(db, capsys):
    from juggle_cmd_agents import cmd_ack_action

    aid = db.add_action_item(
        thread_id=None, message="x", type_="question", priority="normal"
    )
    args = argparse.Namespace(action_id=aid)
    cmd_ack_action(args)
    assert db.get_open_action_items() == []


def test_cmd_close_thread_sets_closed_state(db, capsys):
    from juggle_cmd_threads import cmd_close_thread

    tid = db.create_thread("t", session_id="sessA")
    args = argparse.Namespace(thread_id=tid)
    cmd_close_thread(args)
    assert db.get_thread(tid)["status"] == "closed"


# ── Fix 3: Worktree finalization tests ──────────────────────────────────────

import subprocess
from pathlib import Path

def test_finalize_worktree_no_metadata_skips(tmp_path):
    """Thread has no worktree_path → success, no-op."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from juggle_cmd_agents import _finalize_worktree
    thread = {}
    success, msg = _finalize_worktree(thread)
    assert success, "Should succeed with no metadata"


def test_finalize_worktree_success(tmp_path):
    """Full cycle: create mock repo → worktree → commit → finalize."""
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "-C", str(main), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main), "config", "user.email", "test@test.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "Test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main), "commit", "--allow-empty", "-m", "init"], check=True, capture_output=True)

    wt_path = tmp_path / "worktree"
    branch = "test-branch"
    subprocess.run(["git", "-C", str(main), "worktree", "add", str(wt_path), "-b", branch, "HEAD"],
                   check=True, capture_output=True)
    # Make a commit in the worktree
    (wt_path / "newfile").write_text("hello")
    subprocess.run(["git", "-C", str(wt_path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt_path), "commit", "-m", "test commit"], check=True, capture_output=True)

    # Now call _finalize_worktree
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from juggle_cmd_agents import _finalize_worktree
    thread = {
        "worktree_path": str(wt_path),
        "worktree_branch": branch,
        "main_repo_path": str(main),
    }
    success, msg = _finalize_worktree(thread)
    assert success, f"_finalize_worktree failed: {msg}"
    assert not wt_path.exists(), f"Worktree still exists at {wt_path}"
    # Verify branch was deleted
    result = subprocess.run(["git", "-C", str(main), "branch"], capture_output=True, text=True)
    assert branch not in result.stdout, f"Branch {branch} still exists"


def test_finalize_worktree_already_removed(tmp_path):
    """Worktree path doesn't exist → success, no-op."""
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from juggle_cmd_agents import _finalize_worktree
    thread = {
        "worktree_path": str(tmp_path / "nonexistent"),
        "worktree_branch": "ghost-branch",
        "main_repo_path": str(tmp_path / "main"),
    }
    success, msg = _finalize_worktree(thread)
    assert success, f"Should succeed for already-removed worktree: {msg}"
    assert "already removed" in msg.lower() or success


def test_finalize_worktree_non_ff_leaves_worktree(tmp_path):
    """Diverged main → merge fails → returns failure, worktree intact."""
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "-C", str(main), "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main), "config", "user.email", "test@test.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "Test"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main), "commit", "--allow-empty", "-m", "init"], check=True, capture_output=True)

    wt_path = tmp_path / "worktree"
    branch = "divergent-branch"
    subprocess.run(["git", "-C", str(main), "worktree", "add", str(wt_path), "-b", branch, "HEAD"],
                   check=True, capture_output=True)

    # Divert main: make a commit on main
    (main / "diverged_file").write_text("main change")
    subprocess.run(["git", "-C", str(main), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main), "commit", "-m", "main diverged"], check=True, capture_output=True)

    # Make a commit in the worktree (different branch)
    (wt_path / "wt_file").write_text("wt change")
    subprocess.run(["git", "-C", str(wt_path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt_path), "commit", "-m", "wt commit"], check=True, capture_output=True)

    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from juggle_cmd_agents import _finalize_worktree
    thread = {
        "worktree_path": str(wt_path),
        "worktree_branch": branch,
        "main_repo_path": str(main),
    }
    success, msg = _finalize_worktree(thread)
    # Should fail because main diverged (ff-only won't work)
    assert not success, f"Should fail for non-ff: {msg}"
    assert wt_path.exists(), f"Worktree should still exist on non-ff"
