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
    import juggle_cmd_agents

    monkeypatch.setattr(common, "get_db", lambda: d)
    monkeypatch.setattr(juggle_cmd_agents, "get_db", lambda: d)
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
