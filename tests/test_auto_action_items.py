"""Tests for A1–A3 auto-action-item generation (v1.21.2)."""

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    import juggle_cli_common as common
    import juggle_cmd_agents_common

    monkeypatch.setattr(common, "get_db", lambda: d)
    monkeypatch.setattr(juggle_cmd_agents_common, "get_db", lambda: d)
    return d


# ---------------------------------------------------------------------------
# A1: release-agent files failure action item (priority=high)
# ---------------------------------------------------------------------------


def test_release_agent_files_failure_action_item(db):
    from juggle_cmd_agents import cmd_release_agent

    tid = db.create_thread("test-topic", session_id="s")
    db.update_thread(tid, status="background")
    agent_id = db.create_agent("coder", "pane-1")
    db.update_agent(agent_id, status="busy", assigned_thread=tid)

    args = argparse.Namespace(agent_id=agent_id, force=True)
    cmd_release_agent(args)

    items = db.get_open_action_items()
    assert any(
        item["priority"] == "high"
        and item["type"] == "failure"
        and "released without completing" in item["message"]
        for item in items
    ), f"Expected failure action item, got: {items}"
    assert db.get_thread(tid)["status"] == "failed"


# ---------------------------------------------------------------------------
# A3: complete-agent role=planner files decision action item
# ---------------------------------------------------------------------------


def test_complete_agent_planner_files_decision_action_item(db):
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("plan-topic", session_id="s")
    db.update_thread(tid, status="background")

    args = argparse.Namespace(
        thread_id=tid,
        result_summary="Plan written to projects/foo/plan/2026-05-17-plan.md",
        retain_text=None,
        role="planner",
        open_questions=None,
    )
    cmd_complete_agent(args)

    items = db.get_open_action_items()
    assert any(
        item["type"] == "decision"
        and "Review plan before dispatching coder" in item["message"]
        for item in items
    ), f"Expected decision action item, got: {items}"


# ---------------------------------------------------------------------------
# A2: complete-agent role=coder keyword detection
# ---------------------------------------------------------------------------


def test_complete_agent_coder_draft_keyword_files_manual_step(db):
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("draft-topic", session_id="s")
    db.update_thread(tid, status="background")

    args = argparse.Namespace(
        thread_id=tid,
        result_summary="Done. Draft v2 of the email template saved.",
        retain_text=None,
        role="coder",
        open_questions=None,
    )
    cmd_complete_agent(args)

    items = db.get_open_action_items()
    assert any(
        item["type"] == "manual_step" and "Review/iterate" in item["message"]
        for item in items
    ), f"Expected manual_step action item, got: {items}"


def test_complete_agent_coder_plan_keyword_files_decision(db):
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("spec-topic", session_id="s")
    db.update_thread(tid, status="background")

    args = argparse.Namespace(
        thread_id=tid,
        result_summary="Spec written at docs/specs/2026-05-17-foo.md",
        retain_text=None,
        role="coder",
        open_questions=None,
    )
    cmd_complete_agent(args)

    items = db.get_open_action_items()
    assert any(
        item["type"] == "decision"
        and "Review before dispatching coder" in item["message"]
        for item in items
    ), f"Expected decision action item, got: {items}"


def test_complete_agent_coder_clean_summary_files_nothing(db):
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("clean-topic", session_id="s")
    db.update_thread(tid, status="background")

    args = argparse.Namespace(
        thread_id=tid,
        result_summary="All done. Tests pass, committed to main.",
        retain_text=None,
        role="coder",
        open_questions=None,
    )
    cmd_complete_agent(args)

    items = db.get_open_action_items()
    keyword_items = [
        i
        for i in items
        if i["type"] in ("manual_step", "decision") and "Review" in i["message"]
    ]
    assert keyword_items == [], (
        f"Expected no keyword action items, got: {keyword_items}"
    )


# ---------------------------------------------------------------------------
# A2 false-positive regression tests (v1.21.2)
# ---------------------------------------------------------------------------


def _no_keyword_items(db):
    return [
        i
        for i in db.get_open_action_items()
        if i["type"] in ("manual_step", "decision") and "Review" in i["message"]
    ]


def test_a2_partial_unique_index_no_action(db):
    """'partial unique index' (SQL term) must not trigger draft detection."""
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("rate-limit-topic", session_id="s")
    db.update_thread(tid, status="background")
    args = argparse.Namespace(
        thread_id=tid,
        result_summary=(
            "Done. Removed all rate-limit/concurrency-guard logic. "
            "Dropped idx_one_active_call partial unique index. "
            "All 16 tests pass. SHA: 3063a038"
        ),
        retain_text=None,
        role="coder",
        open_questions=None,
    )
    cmd_complete_agent(args)
    assert _no_keyword_items(db) == [], (
        f"Expected no action items, got: {db.get_open_action_items()}"
    )


def test_a2_bare_v1_version_no_action(db):
    """'v1.21.1' in a version string must not trigger draft detection."""
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("v1-topic", session_id="s")
    db.update_thread(tid, status="background")
    args = argparse.Namespace(
        thread_id=tid,
        result_summary=(
            "Shipped v1.21.1. A1+A2+A3 implemented. 5 tests added. "
            "Full suite 375 passing. Committed to main."
        ),
        retain_text=None,
        role="coder",
        open_questions=None,
    )
    cmd_complete_agent(args)
    assert _no_keyword_items(db) == [], (
        f"Expected no action items, got: {db.get_open_action_items()}"
    )


def test_a2_pending_review_optional_no_action(db):
    """'Pending review optional' must not trigger draft detection."""
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("pending-topic", session_id="s")
    db.update_thread(tid, status="background")
    args = argparse.Namespace(
        thread_id=tid,
        result_summary="Done. Migration applied. Pending review optional.",
        retain_text=None,
        role="coder",
        open_questions=None,
    )
    cmd_complete_agent(args)
    assert _no_keyword_items(db) == [], (
        f"Expected no action items, got: {db.get_open_action_items()}"
    )


def test_a2_real_draft_signal_files_manual_step(db):
    """Explicit draft + placeholder signals without completion markers should file manual_step."""
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("real-draft-topic", session_id="s")
    db.update_thread(tid, status="background")
    args = argparse.Namespace(
        thread_id=tid,
        result_summary="Draft v2 written (~206 lines). 4 [TODO] placeholders remain.",
        retain_text=None,
        role="coder",
        open_questions=None,
    )
    cmd_complete_agent(args)

    items = db.get_open_action_items()
    assert any(
        i["type"] == "manual_step" and "Review/iterate" in i["message"] for i in items
    ), f"Expected manual_step, got: {items}"


def test_a2_plan_written_path_files_decision(db):
    """'Plan written at <path>' from a non-planner role must file a decision action item."""
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("plan-coder-topic", session_id="s")
    db.update_thread(tid, status="background")
    args = argparse.Namespace(
        thread_id=tid,
        result_summary="Plan written at plan/2026-05-17-x.md. 10 tasks. DA findings: none.",
        retain_text=None,
        role="coder",
        open_questions=None,
    )
    cmd_complete_agent(args)

    items = db.get_open_action_items()
    assert any(
        i["type"] == "decision" and "Review before dispatching coder" in i["message"]
        for i in items
    ), f"Expected decision, got: {items}"


def test_a2_plain_done_all_tests_committed_no_action(db):
    """'Done. All N tests pass. Committed to main.' should file nothing."""
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("plain-done-topic", session_id="s")
    db.update_thread(tid, status="background")
    args = argparse.Namespace(
        thread_id=tid,
        result_summary="Done. All 16 tests pass. Committed to main.",
        retain_text=None,
        role="coder",
        open_questions=None,
    )
    cmd_complete_agent(args)
    assert _no_keyword_items(db) == [], (
        f"Expected no action items, got: {db.get_open_action_items()}"
    )


# ---------------------------------------------------------------------------
# Fix A: researcher without open_questions → notification only (no action item)
# ---------------------------------------------------------------------------

import json as _json


def test_complete_agent_researcher_no_open_questions_creates_notification_not_action(db):
    """Researcher complete-agent with NO open_questions → NOTIFICATION, not action item."""
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("research-topic", session_id="s")
    db.update_thread(tid, status="background", open_questions="[]")

    args = argparse.Namespace(
        thread_id=tid,
        result_summary="Found 3 candidate libraries, all support async.",
        retain_text=None,
        role="researcher",
        open_questions=None,
    )
    cmd_complete_agent(args)

    # Should NOT create any keyword action item
    keyword_items = [
        i for i in db.get_open_action_items()
        if i["type"] in ("review", "decision", "manual_step", "question")
    ]
    assert keyword_items == [], (
        f"Expected no action items for researcher without open_questions, got: {keyword_items}"
    )

    # But a notification should exist (session_id from DB, which is "" when unset)
    notifs = db.get_notifications_for_session("")
    assert any(
        "Found 3 candidate libraries" in n["message"] for n in notifs
    ), f"Expected notification, got: {notifs}"


def test_complete_agent_researcher_with_open_questions_creates_action(db):
    """Researcher complete-agent WITH open_questions → action item IS created."""
    from juggle_cmd_agents import cmd_complete_agent

    tid = db.create_thread("research-topic-oq", session_id="s")
    db.update_thread(tid, status="background",
                     open_questions=_json.dumps([{"text": "Should we use lib A or B?"}]))

    args = argparse.Namespace(
        thread_id=tid,
        result_summary="Research complete. Two viable options.",
        retain_text=None,
        role="researcher",
        open_questions=None,
    )
    cmd_complete_agent(args)

    items = db.get_open_action_items()
    # Should have: the open_question action item (type="question") + researcher review item
    assert any(
        i["type"] == "review" and "Research complete" in i["message"]
        for i in items
    ), f"Expected review action item when open_questions exist, got: {items}"

    assert any(
        i["type"] == "question" and "Should we use lib A or B" in i["message"]
        for i in items
    ), f"Expected open_question action item, got: {items}"


# ---------------------------------------------------------------------------
# Full text preservation (truncation removed 2026-06-16)
# ---------------------------------------------------------------------------

def test_action_item_stores_full_long_text(db):
    """Action item text is stored in full — no truncation or pointer suffix."""
    tid = db.create_thread("trunc-topic", session_id="s")
    long_text = "x" * 500
    aid = db.add_action_item(
        thread_id=tid,
        message=long_text,
        type_="manual_step",
        priority="normal",
    )
    items = db.get_open_action_items()
    item = next(i for i in items if i["id"] == aid)
    assert item["message"] == long_text
    assert "full detail" not in item["message"]
    assert "get-messages" not in item["message"]


def test_notification_stores_full_long_text(db):
    """Notification text is stored in full — no truncation or pointer suffix."""
    tid = db.create_thread("trunc-notif-topic", session_id="s")
    long_text = "y" * 600
    nid = db.add_notification_v2(
        thread_id=tid,
        message=long_text,
        session_id="s",
    )
    notifs = db.get_notifications_for_session("s")
    notif = next(n for n in notifs if n["id"] == nid)
    assert notif["message"] == long_text
    assert "full detail" not in notif["message"]
    assert "get-messages" not in notif["message"]


def test_short_text_not_truncated(db):
    """Short action/notification text is NOT truncated."""
    tid = db.create_thread("short-topic", session_id="s")
    short_text = "All tests pass, committed to main."

    aid = db.add_action_item(
        thread_id=tid,
        message=short_text,
        type_="manual_step",
        priority="normal",
    )
    items = db.get_open_action_items()
    item = next(i for i in items if i["id"] == aid)
    assert item["message"] == short_text

    nid = db.add_notification_v2(
        thread_id=tid,
        message=short_text,
        session_id="s",
    )
    notifs = db.get_notifications_for_session("s")
    notif = next(n for n in notifs if n["id"] == nid)
    assert notif["message"] == short_text
