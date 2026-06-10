"""Tests for autopilot Phase 1 marking: complete-agent → node events (notify
only, NO dispatch) and [blocked:]/[ready] context tags for node-bound threads.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest  # noqa: E402

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    import juggle_cli_common as common

    monkeypatch.setattr(common, "get_db", lambda: d)
    return d


def _mk_graph(db):
    """a → b (b depends on a)."""
    g.create_node(db, node_id="a", project_id="INBOX", title="Node A", prompt="do a")
    g.create_node(db, node_id="b", project_id="INBOX", title="Node B", prompt="do b")
    g.replace_edges(db, "b", ["a"])
    g.recompute_ready(db, "INBOX")  # a → ready


def _bind_running_thread(db, node_id, session="sessA"):
    tid = db.create_thread("t", session_id=session)
    db.update_thread(tid, agent_task_id="task-1", status="running")
    db._set_session_key_external("session_id", session)
    g.set_node_thread(db, node_id, tid)
    for ev in ("claim", "dispatch"):
        g.node_transition(db, node_id, ev)
    return tid


def _complete(tid, summary="done", handoff=None):
    from juggle_cmd_agents import cmd_complete_agent

    args = argparse.Namespace(
        thread_id=tid,
        result_summary=summary,
        retain_text=None,
        open_questions=None,
        handoff=handoff,
    )
    cmd_complete_agent(args)


def test_complete_agent_marks_bound_node_verified_and_promotes_dependents(db):
    _mk_graph(db)
    tid = _bind_running_thread(db, "a")
    _complete(tid, handoff="schema landed in x.py")

    node_a = g.get_node(db, "a")
    assert node_a["state"] == "verified"
    assert node_a["verified_at"]
    assert node_a["handoff"] == "schema landed in x.py"
    # dependent promoted to ready, with notification + action item — NO dispatch
    assert g.get_node(db, "b")["state"] == "ready"
    notifs = db.get_notifications_for_session("sessA")
    assert any("b" in n["message"] and "ready" in n["message"] for n in notifs)
    items = db.get_open_action_items()
    assert any("b" in i["message"] and "ready" in i["message"].lower() for i in items)


def test_complete_agent_integrate_failure_marks_failed_never_verified(db, monkeypatch):
    """REQUIRED PIN (DA B3, 2026-06-10): cmd_complete_agent closes the thread
    even when integrate FAILS — node truth must be failed-integration, NEVER
    'verified', and dependents must NOT become ready."""
    import juggle_cmd_agents_common as _com

    _mk_graph(db)
    tid = _bind_running_thread(db, "a")
    db.update_thread(
        tid,
        worktree_path="/tmp/wt",
        worktree_branch="cyc_x",
        main_repo_path="/tmp/repo",
    )
    monkeypatch.setattr(
        _com.juggle_cmd_integrate, "_run_integrate", lambda thread, db_: (False, "rebase conflict")
    )
    # handoff supplied: Phase 2 enforces --handoff for nodes with dependents
    _complete(tid, handoff="attempted; rebase conflict")

    node_a = g.get_node(db, "a")
    assert node_a["state"] == "failed-integration"
    assert node_a["state"] != "verified"
    assert node_a["verified_at"] is None
    # dependents NOT marched over (B3); since Phase 3 they are explicitly
    # blocked-failed (never 'ready'/'verified') — same behavior, new seam.
    assert g.get_node(db, "b")["state"] == "blocked-failed"
    # thread still closed (existing behavior) — node state is the truth
    assert db.get_thread(tid)["status"] == "closed"


def test_complete_agent_unbound_thread_untouched_by_graph(db):
    _mk_graph(db)
    tid = db.create_thread("t", session_id="sessA")
    db.update_thread(tid, agent_task_id="task-1", status="running")
    db._set_session_key_external("session_id", "sessA")
    _complete(tid)  # no node bound to this thread — must not raise
    assert g.get_node(db, "a")["state"] == "ready"
    assert g.get_node(db, "b")["state"] == "pending"


def test_complete_agent_twice_on_terminal_node_does_not_crash(db, capsys):
    _mk_graph(db)
    tid = _bind_running_thread(db, "a")
    _complete(tid, handoff="a done")  # Phase 2: dependents demand a handoff
    assert g.get_node(db, "a")["state"] == "verified"
    _complete(tid)  # second completion: warn, never crash or change state
    assert g.get_node(db, "a")["state"] == "verified"


def test_complete_agent_handoff_cli_flag_registered():
    """--handoff must be wired into the complete-agent parser."""
    import juggle_cli_parsers_agents as parsers

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    parsers.register(sub)
    args = parser.parse_args(
        ["complete-agent", "T", "summary", "--handoff", '{"files":["x.py"]}']
    )
    assert args.handoff == '{"files":["x.py"]}'


# ── context tags ───────────────────────────────────────────────────────────────


def _tier1_header(db, tid):
    from juggle_context import _render_tier1

    thread = db.get_thread(tid)
    return _render_tier1(thread, db)[0]


def test_context_tag_ready_for_node_bound_thread(db):
    _mk_graph(db)
    tid = db.create_thread("t", session_id="s")
    g.set_node_thread(db, "a", tid)  # a is ready
    assert "[ready]" in _tier1_header(db, tid)


def test_context_tag_blocked_lists_unverified_deps(db):
    _mk_graph(db)
    tid = db.create_thread("t", session_id="s")
    g.set_node_thread(db, "b", tid)  # b blocked on a
    assert "[blocked:a]" in _tier1_header(db, tid)


def test_context_no_tag_for_unbound_thread(db):
    _mk_graph(db)
    tid = db.create_thread("t", session_id="s")
    header = _tier1_header(db, tid)
    assert "[ready]" not in header and "[blocked" not in header
