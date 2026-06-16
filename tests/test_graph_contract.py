"""Tests for autopilot Phase 2 contracts: --handoff enforcement on
complete-agent (DA M4) and the send-task tick-ownership guard (DA B5).
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
    import juggle_cmd_agents_common as agents_common

    monkeypatch.setattr(common, "get_db", lambda: d)
    monkeypatch.setattr(agents_common, "get_db", lambda: d)
    return d


def _mk_graph(db):
    """a → b (b depends on a); a promoted to ready."""
    g.create_task(db, task_id="a", project_id="INBOX", title="Task A", prompt="do a")
    g.create_task(db, task_id="b", project_id="INBOX", title="Task B", prompt="do b")
    g.replace_edges(db, "b", ["a"])
    g.recompute_ready(db, "INBOX")


def _bind_running_thread(db, task_id, session="sessA"):
    tid = db.create_thread("t", session_id=session)
    db.update_thread(tid, agent_task_id="task-1", status="running")
    db._set_session_key_external("session_id", session)
    g.set_task_thread(db, task_id, tid)
    for ev in ("claim", "dispatch"):
        g.task_transition(db, task_id, ev)
    return tid


def _complete(tid, summary="done", handoff=None):
    from juggle_cmd_agents import cmd_complete_agent

    cmd_complete_agent(
        argparse.Namespace(
            thread_id=tid,
            result_summary=summary,
            retain_text=None,
            open_questions=None,
            handoff=handoff,
        )
    )


# ── --handoff enforcement (DA M4) ──────────────────────────────────────────────


def test_complete_refuses_missing_handoff_when_task_has_dependents(db, capsys):
    """REGRESSION PIN (DA M4, 2026-06-10): hydration garbage-in — completing a
    graph task WITH dependents and no --handoff used to succeed, leaving
    dependents to hydrate from junk. complete-agent must REFUSE (exit nonzero)
    and leave task + thread untouched."""
    _mk_graph(db)
    tid = _bind_running_thread(db, "a")

    with pytest.raises(SystemExit) as ei:
        _complete(tid, handoff=None)

    assert ei.value.code != 0
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "--handoff" in out and "b" in out  # names the dependents
    assert g.get_task(db, "a")["state"] == "running"  # task untouched
    assert db.get_thread(tid)["status"] == "running"  # thread NOT closed
    assert db.get_open_action_items() == []  # no partial side effects


def test_complete_refuses_blank_handoff(db):
    _mk_graph(db)
    tid = _bind_running_thread(db, "a")
    with pytest.raises(SystemExit):
        _complete(tid, handoff="   ")
    assert g.get_task(db, "a")["state"] == "running"


def test_complete_with_handoff_passes_enforcement(db):
    _mk_graph(db)
    tid = _bind_running_thread(db, "a")
    _complete(tid, handoff="files: x.py; api: get_task()")
    assert g.get_task(db, "a")["state"] == "verified"
    assert g.get_task(db, "b")["state"] == "ready"


def test_complete_leaf_task_needs_no_handoff(db):
    """b has no dependents — the contract only binds tasks others consume."""
    _mk_graph(db)
    # drive a → verified first
    tid_a = _bind_running_thread(db, "a")
    _complete(tid_a, handoff="a done")
    tid_b = _bind_running_thread(db, "b")
    _complete(tid_b, handoff=None)  # must NOT raise
    assert g.get_task(db, "b")["state"] == "verified"


def test_complete_terminal_task_skips_enforcement(db):
    """Double-completion of an already-terminal task keeps Phase 1 warn+no-op
    behavior — no retroactive handoff demand."""
    _mk_graph(db)
    tid = _bind_running_thread(db, "a")
    _complete(tid, handoff="a done")
    _complete(tid, handoff=None)  # second completion: warn, never refuse
    assert g.get_task(db, "a")["state"] == "verified"


def test_complete_unbound_thread_unaffected_by_enforcement(db):
    _mk_graph(db)
    tid = db.create_thread("t", session_id="sessA")
    db.update_thread(tid, agent_task_id="task-1", status="running")
    db._set_session_key_external("session_id", "sessA")
    _complete(tid, handoff=None)  # no task — must not raise
    assert db.get_thread(tid)["status"] == "closed"


# ── send-task task guard (DA B5) ───────────────────────────────────────────────


def test_check_task_guard_refuses_tick_owned_states(db):
    from juggle_cmd_agents_graph import check_task_guard

    g.create_task(db, task_id="n", project_id="INBOX", title="N", prompt="p")
    tid = db.create_thread("t", session_id="s")
    g.set_task_thread(db, "n", tid)

    for state in sorted(g.TICK_OWNED_STATES):
        with db._connect() as conn:
            conn.execute("UPDATE graph_tasks SET state=? WHERE id='n'", (state,))
            conn.commit()
        err = check_task_guard(db, tid, force=False)
        assert err and "force-task" in err, state
        assert check_task_guard(db, tid, force=True) is None, state


def test_check_task_guard_allows_operator_states_and_unbound(db):
    from juggle_cmd_agents_graph import check_task_guard

    g.create_task(db, task_id="n", project_id="INBOX", title="N", prompt="p")
    tid = db.create_thread("t", session_id="s")
    g.set_task_thread(db, "n", tid)
    for state in ("pending", "failed-exec", "failed-integration", "failed-verify",
                  "blocked-failed"):
        with db._connect() as conn:
            conn.execute("UPDATE graph_tasks SET state=? WHERE id='n'", (state,))
            conn.commit()
        assert check_task_guard(db, tid, force=False) is None, state
    tid2 = db.create_thread("t2", session_id="s")
    assert check_task_guard(db, tid2, force=False) is None  # unbound thread


def test_send_task_refuses_task_bound_thread_without_force(db, tmp_path, monkeypatch, capsys):
    """REGRESSION PIN (DA B5, 2026-06-10): the autopilot LLM loop raced the
    tick by manually dispatching ready/running graph tasks. cmd_send_task must
    refuse threads bound to tick-owned tasks unless --force-task, BEFORE any
    tmux side effects."""
    import juggle_cmd_agents_common as _com
    from juggle_cmd_agents import cmd_send_task

    g.create_task(db, task_id="n", project_id="INBOX", title="N", prompt="p")
    g.recompute_ready(db, "INBOX")
    tid = db.create_thread("t", session_id="s")
    g.set_task_thread(db, "n", tid)
    for ev in ("claim", "dispatch"):
        g.task_transition(db, "n", ev)  # n → running (tick-owned)
    agent_id = db.create_agent("coder", "%99")
    db.update_agent(agent_id, assigned_thread=tid)

    def _boom(*a, **kw):  # tmux must never be touched on refusal
        raise AssertionError("JuggleTmuxManager constructed despite guard")

    monkeypatch.setattr(_com, "JuggleTmuxManager", _boom)
    prompt_file = tmp_path / "p.md"
    prompt_file.write_text("task")

    args = argparse.Namespace(
        agent_id=agent_id, prompt_file=str(prompt_file), no_template=True,
        worktree_path=None, worktree_branch=None, main_repo_path=None,
        allow_main=False, force_task=False,
    )
    with pytest.raises(SystemExit) as ei:
        cmd_send_task(args)
    assert ei.value.code != 0
    assert "force-task" in capsys.readouterr().out


def test_send_task_force_task_flag_registered():
    """--force-task must be wired into the send-task parser."""
    import juggle_cli_parsers_agents as parsers

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    parsers.register(sub)
    args = parser.parse_args(["send-task", "AGENT", "/tmp/p.md", "--force-task"])
    assert args.force_task is True
    args = parser.parse_args(["send-task", "AGENT", "/tmp/p.md"])
    assert args.force_task is False


# ── topic completion gate + marking (R9, 2026-06-11) ──────────────────────────

from dbops import db_topics as tp  # noqa: E402


def _mk_topic_task(db, topic_id, task_id, project="INBOX"):
    if tp.get_topic(db, topic_id) is None:
        tp.create_topic(db, topic_id=topic_id, project_id=project, title=topic_id)
    g.create_task(db, task_id=task_id, project_id=project, title=task_id, prompt="p")
    with db._connect() as conn:
        conn.execute("UPDATE graph_tasks SET topic_id=? WHERE id=?", (topic_id, task_id))
        conn.commit()


def _verify_task(db, task_id, fail=False):
    g.mark_completion(db, task_id, integrate_ok=True, verify_ok=not fail, handoff="h")


def _merged_repo() -> str:
    """A real repo whose 'main' branch satisfies the G1 verified⟺merged guard."""
    import subprocess
    import tempfile
    d = tempfile.mkdtemp(prefix="juggle-merged-")

    def _git(*a):
        subprocess.run(["git", "-C", d, *a], check=True, capture_output=True,
                       text=True)

    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "t@t.t")
    _git("config", "user.name", "T")
    (Path(d) / "f.txt").write_text("base\n")
    _git("add", ".")
    _git("commit", "-qm", "base")
    return d


def _bind_running_topic(db, topic_id, session="sessA", *, merged=False):
    tid = db.create_thread("t", session_id=session)
    # T-verified-merged-sha: a topic verifies only via a recorded merged_sha that
    # is an ancestor of main. Use a real repo + record main's HEAD when the test
    # expects verification; a fake path with no merged_sha otherwise.
    branch, repo = ("cyc_x", _merged_repo()) if merged else ("cyc_x", "/tmp/repo")
    db.update_thread(tid, agent_task_id="task-1", status="running",
                     worktree_path="/tmp/wt", worktree_branch=branch,
                     main_repo_path=repo)
    db._set_session_key_external("session_id", session)
    tp.set_topic_thread(db, topic_id, tid)
    if merged:
        import subprocess
        sha = subprocess.run(["git", "-C", repo, "rev-parse", "main"],
                             capture_output=True, text=True)
        if sha.returncode == 0 and sha.stdout.strip():
            tp.set_topic_merged_sha(db, topic_id, sha.stdout.strip())
    for ev in ("deps_ready", "claim", "dispatch"):
        tp.topic_transition(db, topic_id, ev)
    return tid


def test_complete_agent_refuses_while_tasks_unmarked(db, capsys, monkeypatch):
    """REGRESSION PIN (2026-06-11 R9/A10): complete-agent on a topic thread with
    non-terminal tasks must REFUSE (exit 1) BEFORE integrate — nothing marked,
    nothing merged. The gate is code, not prompt."""
    import juggle_cmd_agents_common as _com

    _mk_topic_task(db, "A", "a1")
    _mk_topic_task(db, "A", "a2")
    _verify_task(db, "a1")  # a2 still pending
    tid = _bind_running_topic(db, "A")
    calls = []
    monkeypatch.setattr(_com.juggle_cmd_integrate, "_run_integrate",
                        lambda thread, db_: calls.append(1) or (True, "ok"))

    with pytest.raises(SystemExit) as ei:
        _complete(tid, handoff="topic done")

    assert ei.value.code != 0
    assert tp.get_topic(db, "A")["state"] == "running"  # NOT advanced past running
    assert g.get_task(db, "a2")["state"] == "pending"
    assert calls == [], "integrate must NOT run when the gate refuses"


def test_complete_agent_marks_topic_when_all_tasks_terminal(db, monkeypatch):
    """All tasks verified → topic 'verified', handoff stored; integrate stub
    called exactly ONCE (integrate-once-per-topic, spec §2.3)."""
    import juggle_cmd_agents_common as _com

    _mk_topic_task(db, "A", "a1")
    _mk_topic_task(db, "A", "a2")
    _verify_task(db, "a1")
    _verify_task(db, "a2")
    tid = _bind_running_topic(db, "A", merged=True)  # G1: merged → verifies
    calls = []
    monkeypatch.setattr(_com.juggle_cmd_integrate, "_run_integrate",
                        lambda thread, db_: calls.append(1) or (True, "ok"))

    _complete(tid, handoff="topic handoff")

    assert tp.get_topic(db, "A")["state"] == "verified"
    assert tp.get_topic(db, "A")["handoff"] == "topic handoff"
    assert calls == [1], "integrate runs exactly once per topic"


def test_topic_with_failed_task_completes_as_failed_verify(db, monkeypatch):
    """a1 verified, a2 failed-verify (terminal) → gate passes, verify_ok=False
    → topic 'failed-verify'; derived dependent topics → blocked-failed."""
    import juggle_cmd_agents_common as _com

    _mk_topic_task(db, "A", "a1")
    _mk_topic_task(db, "A", "a2")
    _mk_topic_task(db, "B", "b1")
    with db._connect() as conn:  # B derives on A (b1 → a1)
        conn.execute("INSERT INTO graph_edges (task_id, depends_on_id) "
                     "VALUES ('b1','a1')")
        conn.commit()
    _verify_task(db, "a1")
    _verify_task(db, "a2", fail=True)  # terminal, not verified
    tid = _bind_running_topic(db, "A")
    monkeypatch.setattr(_com.juggle_cmd_integrate, "_run_integrate",
                        lambda thread, db_: (True, "ok"))

    _complete(tid, handoff="topic attempted")

    assert tp.get_topic(db, "A")["state"] == "failed-verify"
    assert tp.get_topic(db, "B")["state"] == "blocked-failed"


# ── armed-project dispatch guard (R8, adapted to topics 2026-06-11) ───────────


def test_guard_refuses_unbound_thread_of_armed_project(db):
    """REGRESSION PIN (2026-06-10 R8): ad-hoc send-task to an armed project's
    threads bypassed the graph — new work must route through add-task."""
    from juggle_cmd_agents_graph import check_task_guard
    from juggle_autopilot_state import arm_project

    arm_project(db, "INBOX")
    tid = db.create_thread("adhoc", session_id="s")
    db.update_thread(tid, project_id="INBOX")
    err = check_task_guard(db, tid, force=False)
    assert err and "add-task" in err and "force-task" in err
    assert check_task_guard(db, tid, force=True) is None


def test_guard_lifts_on_disarm_and_ignores_unarmed(db):
    from juggle_cmd_agents_graph import check_task_guard
    from juggle_autopilot_state import arm_project, disarm_project

    p2 = db.create_project(name="Other", objective="o")
    arm_project(db, p2)
    tid = db.create_thread("adhoc", session_id="s")
    db.update_thread(tid, project_id="INBOX")
    assert check_task_guard(db, tid, force=False) is None  # unarmed project
    arm_project(db, "INBOX")
    assert check_task_guard(db, tid, force=False) is not None
    disarm_project(db, "INBOX")
    assert check_task_guard(db, tid, force=False) is None


def test_guard_topic_bound_operator_state_allowed(db):
    """R8 must not tighten DA B5: a TOPIC-bound thread in operator territory
    (failed-exec) stays manually redispatchable even while armed."""
    from dbops import db_topics as tp_
    from juggle_cmd_agents_graph import check_task_guard
    from juggle_autopilot_state import arm_project

    arm_project(db, "INBOX")
    tp_.create_topic(db, topic_id="T1", project_id="INBOX", title="t")
    tid = db.create_thread("t", session_id="s")
    tp_.set_topic_thread(db, "T1", tid)
    with db._connect() as conn:
        conn.execute("UPDATE graph_topics SET state='failed-exec' WHERE id='T1'")
        conn.commit()
    assert check_task_guard(db, tid, force=False) is None
