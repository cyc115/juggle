"""Regression pins for junk-thread creation guard (2026-06-15).

Incident: autopilot sessions spawned ~15 duplicate threads with LLM-generated
titles like "Improve Dispatch", "Enhance Agent Dispatch System" because the
auto-thread creation path accepted orchestrator-chatter (AUTOPILOT MODE cards,
JUGGLE ACTIVE blocks, "# Autonomous loop tick" headers) as legitimate topics.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "juggle.db"
    d = JuggleDB(db_path=str(path))
    d.init_db()
    return d


# ---------------------------------------------------------------------------
# Table tests — is_auto_topic_eligible predicate
# ---------------------------------------------------------------------------

CHATTER_CASES = [
    # autopilot directive header
    "--- AUTOPILOT MODE: ON ---\nAutopilot is engaged...",
    # standalone autopilot marker
    "--- AUTOPILOT MODE: ON ---",
    # autonomous loop tick header
    "# Autonomous loop tick\nState: armed, 2 ready topics",
    # JUGGLE ACTIVE context block
    "--- JUGGLE ACTIVE (do not forward to sub-agents) ---\n# Active Threads\n...",
    # just the JUGGLE ACTIVE marker
    "--- JUGGLE ACTIVE",
    # Active Threads section dump
    "# Active Threads\n[ZV] Some task (active)\n",
    # Notifications section dump
    "# Notifications (this session)\n✓ topic ZV dispatched\n",
    # END JUGGLE footer
    "--- END JUGGLE ---",
]

REAL_REQUEST_CASES = [
    "add a hotkey to dismiss dossier items",
    "fix the watchdog stall detection for planner agents",
    "implement OAuth token refresh for the Slack integration",
    "refactor the graph hydration to support topic-level handoffs",
    "write tests for the autopilot armed-project guard",
]


@pytest.mark.parametrize("content", CHATTER_CASES)
def test_is_auto_topic_eligible_rejects_orchestrator_chatter(content):
    from dbops.schema import is_auto_topic_eligible
    assert is_auto_topic_eligible(content) is False, (
        f"Expected chatter to be ineligible: {content[:60]!r}"
    )


@pytest.mark.parametrize("content", REAL_REQUEST_CASES)
def test_is_auto_topic_eligible_allows_real_requests(content):
    from dbops.schema import is_auto_topic_eligible
    assert is_auto_topic_eligible(content) is True, (
        f"Expected real request to be eligible: {content!r}"
    )


# ---------------------------------------------------------------------------
# Integration — create-thread trigger guard
# ---------------------------------------------------------------------------

def test_create_thread_rejects_orchestrator_chatter(db, monkeypatch, capsys):
    """Trigger: cmd_create_thread with orchestrator chatter MUST NOT create a thread."""
    from juggle_cmd_threads import cmd_create_thread

    monkeypatch.setattr("juggle_cmd_threads.get_db", lambda: db)
    monkeypatch.setattr(
        "juggle_cmd_threads._generate_title_for_thread",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "juggle_cmd_threads.assign_project_background",
        lambda *a, **kw: None,
    )

    chatter = "--- AUTOPILOT MODE: ON ---\nAutopilot is engaged..."
    args = argparse.Namespace(topic=chatter)

    with pytest.raises(SystemExit):
        cmd_create_thread(args)

    threads = db.get_all_threads()
    assert len(threads) == 0, (
        f"No thread should be created from orchestrator chatter; got {threads}"
    )


def test_create_thread_allows_real_topic(db, monkeypatch):
    """Trigger: cmd_create_thread with a real topic DOES create a thread."""
    from juggle_cmd_threads import cmd_create_thread

    monkeypatch.setattr("juggle_cmd_threads.get_db", lambda: db)
    monkeypatch.setattr(
        "juggle_cmd_threads._generate_title_for_thread",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "juggle_cmd_threads.assign_project_background",
        lambda *a, **kw: None,
    )

    args = argparse.Namespace(topic="add a hotkey to dismiss dossier items")

    cmd_create_thread(args)

    threads = db.get_all_threads()
    assert len(threads) == 1
    assert threads[0]["title"] == "add a hotkey to dismiss dossier items"


# ---------------------------------------------------------------------------
# Cleanup selector
# ---------------------------------------------------------------------------

def test_close_junk_threads_selector(db):
    """Cleanup picks only generic auto-titled, work-less threads."""
    from juggle_cmd_threads import close_junk_threads

    # Junk thread: topic looks like orchestrator chatter, no messages, no worktree
    junk_id = db.create_thread(
        "--- AUTOPILOT MODE: ON ---\nImprove dispatch reliability",
        session_id="",
    )
    db.update_thread(junk_id, title="Improve Dispatch")

    # Real thread: has a real topic and a real message
    real_id = db.create_thread(
        "add hotkey to dismiss dossier items",
        session_id="",
    )
    db.add_message(real_id, "user", "implement this feature")

    # Another junk: JUGGLE ACTIVE in topic, no messages
    junk2_id = db.create_thread(
        "--- JUGGLE ACTIVE (do not forward to sub-agents) ---",
        session_id="",
    )
    db.update_thread(junk2_id, title="Enhance Agent Dispatch Orchestration")

    closed = close_junk_threads(db)

    closed_ids = {t["id"] for t in closed}
    assert junk_id in closed_ids, "Junk thread (chatter topic) should be closed"
    assert junk2_id in closed_ids, "Junk thread (JUGGLE ACTIVE topic) should be closed"
    assert real_id not in closed_ids, "Real thread with messages must NOT be closed"

    # Verify status in DB
    junk_thread = db.get_thread(junk_id)
    assert junk_thread["state"] in ("done", "archived"), (
        f"Expected junk thread closed, got {junk_thread['status']!r}"
    )
    real_thread = db.get_thread(real_id)
    assert real_thread["state"] == "open", "Real thread must remain active"
