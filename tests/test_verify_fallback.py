"""Verify-fallback (self-heal): bounded retry with fresh context.

Deterministic, seeded tmp_path DB. A scripted ``_run_integrate`` stands in for
the real integrate+verify_cmd run so the retry loop is exercised without spawning
agents:
  (a) verify fails once then passes  → task verified after 1 retry
  (b) verify always fails            → task escalated after N retries

R0 pin: a failed-verify still escalates via the SAME HIGH action item + dependent
propagation as any other terminal failure (behaviour-preserving seam).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest  # noqa: E402

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from juggle_integrate_verify import VERIFY_FAIL_PREFIX  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    import juggle_cli_common as common

    monkeypatch.setattr(common, "get_db", lambda: d)
    return d


def _mk_task(db, task_id="a", project="INBOX"):
    g.create_task(db, task_id=task_id, project_id=project, title=task_id,
                  prompt="do it", verify_cmd="make test")
    g.recompute_ready(db, project)  # → ready (no deps)


def _bind_running_thread(db, task_id="a", session="sessA"):
    tid = db.create_thread("t", session_id=session)
    db.update_thread(
        tid, agent_task_id="task-1", status="running",
        worktree_path="/tmp/wt", worktree_branch="cyc_x", main_repo_path="/tmp/repo",
    )
    db._set_session_key_external("session_id", session)
    g.set_task_thread(db, task_id, tid)
    for ev in ("claim", "dispatch"):
        g.task_transition(db, task_id, ev)
    return tid


def _complete(tid, summary="done", handoff="h"):
    from juggle_cmd_agents import cmd_complete_agent

    args = argparse.Namespace(
        thread_id=tid, result_summary=summary, retain_text=None,
        open_questions=None, handoff=handoff,
    )
    cmd_complete_agent(args)


def _verify_fail_result(task_id="a"):
    return (False, f"{VERIFY_FAIL_PREFIX} for task {task_id} (`make test`): exit 1. "
                   f"stdout tail: assert 1 == 2")


# ── R0: behaviour-preserving escalation seam ─────────────────────────────────


def test_failed_verify_escalates_when_retries_disabled(db, monkeypatch):
    """With N=0 the seam must escalate on the FIRST failed-verify: task stays
    failed-verify + a HIGH failure action item is raised (R0 behaviour)."""
    monkeypatch.setenv("JUGGLE_VERIFY_FALLBACK_RETRIES", "0")
    import juggle_cmd_agents_common as _com

    _mk_task(db)
    tid = _bind_running_thread(db)
    monkeypatch.setattr(
        _com.juggle_cmd_integrate, "_run_integrate",
        lambda thread, db_: _verify_fail_result(),
    )
    _complete(tid)

    assert g.get_task(db, "a")["state"] == "failed-verify"
    items = db.get_open_action_items()
    assert any(i.get("priority") == "high" and "failed-verify" in i["message"]
               for i in items)
