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


# ── (a) fails once, then passes → verified after 1 retry ─────────────────────


def test_verify_fails_once_then_passes_verifies_after_one_retry(db, monkeypatch):
    monkeypatch.setenv("JUGGLE_VERIFY_FALLBACK_RETRIES", "1")
    import juggle_cmd_agents_common as _com

    _mk_task(db)
    # Attempt 1: verify red → bounded retry resets the task to ready.
    tid1 = _bind_running_thread(db, session="s1")
    monkeypatch.setattr(
        _com.juggle_cmd_integrate, "_run_integrate",
        lambda thread, db_: _verify_fail_result(),
    )
    _complete(tid1)

    t = g.get_task(db, "a")
    assert t["state"] == "ready", "retry must reset the task to ready"
    assert t["verify_retries"] == 1
    assert "make test" in (t["verify_failure"] or "")

    # Attempt 2 (fresh agent, tick would re-dispatch): verify green → verified.
    tid2 = _bind_running_thread(db, session="s2")
    monkeypatch.setattr(
        _com.juggle_cmd_integrate, "_run_integrate",
        lambda thread, db_: (True, "merged"),
    )
    _complete(tid2)

    t = g.get_task(db, "a")
    assert t["state"] == "verified"
    assert t["verify_retries"] == 1  # counter preserved, not reset


# ── (b) always fails → escalated after N retries ─────────────────────────────


def test_verify_always_fails_escalates_after_n_retries(db, monkeypatch):
    monkeypatch.setenv("JUGGLE_VERIFY_FALLBACK_RETRIES", "2")
    import juggle_cmd_agents_common as _com

    _mk_task(db)
    monkeypatch.setattr(
        _com.juggle_cmd_integrate, "_run_integrate",
        lambda thread, db_: _verify_fail_result(),
    )
    # Two retries: each failed-verify resets to ready.
    for n in range(1, 3):
        tid = _bind_running_thread(db, session=f"s{n}")
        _complete(tid)
        t = g.get_task(db, "a")
        assert t["state"] == "ready", f"retry {n} must reset to ready"
        assert t["verify_retries"] == n

    # Third failure exhausts the budget → terminal failed-verify + escalation.
    tid = _bind_running_thread(db, session="s3")
    _complete(tid)
    t = g.get_task(db, "a")
    assert t["state"] == "failed-verify"
    assert t["verify_retries"] == 2  # never bumped past the budget
    items = db.get_open_action_items()
    assert any(i.get("priority") == "high" and "failed-verify" in i["message"]
               for i in items)


# ── prompt injection: prior failure output reaches the fresh dispatch ────────


def test_prior_failure_injected_into_redispatch_prompt(db, monkeypatch):
    from juggle_graph_hydration import hydrate_for_task

    monkeypatch.setenv("JUGGLE_VERIFY_FALLBACK_RETRIES", "1")
    import juggle_cmd_agents_common as _com

    _mk_task(db)
    tid = _bind_running_thread(db)
    monkeypatch.setattr(
        _com.juggle_cmd_integrate, "_run_integrate",
        lambda thread, db_: _verify_fail_result(),
    )
    _complete(tid)

    prompt = hydrate_for_task(db, "INBOX", g.get_task(db, "a"))
    assert "Previous attempt" in prompt
    assert "make test" in prompt  # the stored verify_cmd failure output


# ── loop closes: the tick re-dispatches the reset task with the failure ──────


def test_tick_redispatches_reset_task_with_prior_failure(db, monkeypatch):
    """The reset-to-ready task is re-picked by the EXISTING watchdog tick (flat
    fallback) and re-dispatched with the prior failure output in its prompt —
    proving the fallback reuses the real dispatch+verify loop."""
    import juggle_graph_dispatch as gd
    import juggle_cmd_agents_common as _com

    monkeypatch.setenv("JUGGLE_VERIFY_FALLBACK_RETRIES", "1")
    _mk_task(db)
    tid = _bind_running_thread(db)
    monkeypatch.setattr(
        _com.juggle_cmd_integrate, "_run_integrate",
        lambda thread, db_: _verify_fail_result(),
    )
    _complete(tid)
    assert g.get_task(db, "a")["state"] == "ready"

    class FakeDispatch:
        def __init__(self):
            self.calls = []

        def __call__(self, db_, thread_id, prompt, task):
            self.calls.append((task["id"], prompt))

    fake = FakeDispatch()
    stats = gd.graph_tick(db, dispatch_fn=fake)

    assert "a" in stats["dispatched"]
    (task_id, prompt), = [c for c in fake.calls if c[0] == "a"]
    assert "Previous attempt" in prompt and "make test" in prompt


# ── migration: additive verify-retry columns ────────────────────────────────


def test_migration_57_adds_columns_idempotent():
    import sqlite3
    from dbops.migration_57_verify_retries import migrate_57_verify_retries

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, kind TEXT)")
    migrate_57_verify_retries(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    assert {"verify_retries", "verify_failure"} <= cols
    migrate_57_verify_retries(conn)  # idempotent — must not raise
    # no nodes table → no-op, no crash
    migrate_57_verify_retries(sqlite3.connect(":memory:"))
