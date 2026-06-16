"""Tests for reconcile_topic_state / reconcile_project_topics (B1 + B3).

Regression pin (2026-06-11 bug J): topic tier drifts from task tier — graph
mark-task advances tasks but topics stay phantom 'running'. reconcile repairs
the drift; B1 wires it into mark-task write path; B3 exposes it as a CLI
subcommand and doctor hook.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as t  # noqa: E402
import juggle_cmd_graph as cg  # noqa: E402


def _bind_merged(db, topic_id: str, tmp_path: Path) -> None:
    """G1 (2026-06-13): a topic only reconciles to 'verified' when its bound
    branch is merged to main. Give the topic a thread on a merged repo so the
    reconcile-to-verified path stays exercisable post-guard."""
    repo = tmp_path / f"repo_{topic_id}"
    repo.mkdir()

    def _git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True,
                       capture_output=True, text=True)

    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "t@t.t")
    _git("config", "user.name", "T")
    (repo / "f.txt").write_text("base\n")
    _git("add", ".")
    _git("commit", "-qm", "base")  # branch 'cyc' will be an ancestor of main
    thread_id = db.create_thread(topic="w", session_id="sessR")
    db.update_thread(thread_id, worktree_branch="main", main_repo_path=str(repo))
    t.set_topic_thread(db, topic_id, thread_id)


# ── helpers ────────────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    import juggle_cli_common as common
    monkeypatch.setattr(common, "get_db", lambda *a, **kw: d)
    return d


def _mk_project(db, pid="P1"):
    db.create_project(pid, pid, "test project")
    return pid


def _mk_topic(db, topic_id, project_id, state="pending"):
    t.create_topic(db, topic_id=topic_id, project_id=project_id, title=topic_id)
    if state != "pending":
        with db._connect() as conn:
            conn.execute(
                "UPDATE graph_topics SET state=? WHERE id=?", (state, topic_id)
            )
            conn.commit()


def _mk_task(db, task_id, project_id, topic_id, state="pending"):
    g.create_task(db, task_id=task_id, project_id=project_id, title=task_id,
                  prompt=f"do {task_id}")
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_tasks SET topic_id=? WHERE id=?", (topic_id, task_id)
        )
        conn.commit()
    if state != "pending":
        # Walk the task to the desired state via mark_completion
        if state == "verified":
            g.mark_completion(db, task_id, integrate_ok=True, verify_ok=True)
        elif state == "failed-verify":
            g.mark_completion(db, task_id, integrate_ok=True, verify_ok=False)
        elif state == "failed-exec":
            g.mark_exec_failed(db, task_id)
        elif state in ("running", "dispatching", "integrating"):
            # manually set for simplicity
            with db._connect() as conn:
                conn.execute(
                    "UPDATE graph_tasks SET state=? WHERE id=?", (state, task_id)
                )
                conn.commit()


# ── reconcile_topic_state unit tests ──────────────────────────────────────────


def test_reconcile_sets_topic_verified_when_all_tasks_verified(db, tmp_path):
    """All member tasks verified AND work merged → topic becomes verified."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="pending")
    _mk_task(db, "n1", pid, "T1", state="verified")
    _mk_task(db, "n2", pid, "T1", state="verified")
    _bind_merged(db, "T1", tmp_path)

    result = t.reconcile_topic_state(db, "T1")

    assert result == "verified"
    topic = t.get_topic(db, "T1")
    assert topic["state"] == "verified"
    assert topic["verified_at"] is not None


def test_reconcile_idempotent_on_already_verified_topic(db, tmp_path):
    """Re-running reconcile on an already-verified topic leaves it unchanged.

    Regression pin (2026-06-11 bug J): idempotency required so repeated reconcile
    calls don't corrupt terminal topics.
    """
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="verified")
    _mk_task(db, "n1", pid, "T1", state="verified")
    _bind_merged(db, "T1", tmp_path)

    result1 = t.reconcile_topic_state(db, "T1")
    result2 = t.reconcile_topic_state(db, "T1")

    assert result1 == "verified"
    assert result2 == "verified"
    assert t.get_topic(db, "T1")["state"] == "verified"


def test_reconcile_clears_phantom_running_to_verified(db, tmp_path):
    """Exact prod bug (2026-06-11 bug J): topic stored 'running' but all member
    tasks verified AND merged → reconcile sets topic to verified."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="running")  # phantom — agent died
    _mk_task(db, "n1", pid, "T1", state="verified")
    _mk_task(db, "n2", pid, "T1", state="verified")
    _bind_merged(db, "T1", tmp_path)

    result = t.reconcile_topic_state(db, "T1")

    assert result == "verified"
    assert t.get_topic(db, "T1")["state"] == "verified"


def test_reconcile_holds_at_integrating_when_unmerged(db):
    """G1 (2026-06-13): all member tasks verified but NO merge to main → topic
    stays pre-verified ('integrating'), never silently 'verified'."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="running")
    _mk_task(db, "n1", pid, "T1", state="verified")
    _mk_task(db, "n2", pid, "T1", state="verified")
    # No bound merged repo → not mergeable.

    result = t.reconcile_topic_state(db, "T1")

    assert result == "integrating"
    assert t.get_topic(db, "T1")["state"] == "integrating"


def test_reconcile_failed_member_sets_topic_failed_verify(db):
    """Any member task in a failed state → topic becomes failed-verify."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="pending")
    _mk_task(db, "n1", pid, "T1", state="verified")
    _mk_task(db, "n2", pid, "T1", state="failed-verify")

    result = t.reconcile_topic_state(db, "T1")

    assert result == "failed-verify"
    assert t.get_topic(db, "T1")["state"] == "failed-verify"


def test_reconcile_running_member_sets_topic_running(db):
    """Any member task running/dispatching/integrating → topic becomes running."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="pending")
    _mk_task(db, "n1", pid, "T1", state="verified")
    _mk_task(db, "n2", pid, "T1", state="running")

    result = t.reconcile_topic_state(db, "T1")

    assert result == "running"
    assert t.get_topic(db, "T1")["state"] == "running"


# ── B1: write-path sync via cmd_graph_mark_task ───────────────────────────────


def test_mark_task_last_task_flips_topic_verified(db, tmp_path):
    """B1 regression pin (2026-06-11 bug J): marking the last unverified task
    of a topic via 'graph mark-task' must atomically flip the owning topic to
    verified — task tier and topic tier must never drift after mark-task.
    G1 (2026-06-13): the topic's work must be merged for the flip to verified."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="running")
    _mk_task(db, "n1", pid, "T1", state="verified")
    _mk_task(db, "n2", pid, "T1", state="pending")  # last unverified
    _bind_merged(db, "T1", tmp_path)

    args = SimpleNamespace(
        task_id="n2", fail=False, handoff=None,
        db_path=str(db.db_path),
    )
    cg.cmd_graph_mark_task(args)

    task = g.get_task(db, "n2")
    assert task["state"] == "verified"
    topic = t.get_topic(db, "T1")
    assert topic["state"] == "verified", (
        f"topic tier drifted: expected 'verified', got {topic['state']!r}"
    )


# ── B3: reconcile CLI subcommand ──────────────────────────────────────────────


def test_graph_reconcile_cli_corrects_drifted_topic(db, capsys, tmp_path):
    """'juggle graph reconcile <project>' fixes drifted topic states."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="running")  # phantom running
    _mk_task(db, "n1", pid, "T1", state="verified")
    _bind_merged(db, "T1", tmp_path)
    _mk_topic(db, "T2", pid, state="pending")
    _mk_task(db, "n2", pid, "T2", state="pending")

    args = SimpleNamespace(project=pid, json_out=False, db_path=str(db.db_path))
    cg.cmd_graph_reconcile(args)

    out = capsys.readouterr().out
    assert "T1" in out
    assert "running" in out
    assert "verified" in out
    assert t.get_topic(db, "T1")["state"] == "verified"
    assert t.get_topic(db, "T2")["state"] == "pending"  # unchanged


def test_graph_reconcile_cli_json_output(db, capsys, tmp_path):
    """'juggle graph reconcile --json' emits valid JSON with before/after."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="running")
    _mk_task(db, "n1", pid, "T1", state="verified")
    _bind_merged(db, "T1", tmp_path)

    args = SimpleNamespace(project=pid, json_out=True, db_path=str(db.db_path))
    cg.cmd_graph_reconcile(args)

    out = capsys.readouterr().out
    data = json.loads(out)
    assert "T1" in data
    assert data["T1"]["before"] == "running"
    assert data["T1"]["after"] == "verified"


# ── T-reconcile-orphan-integrating: recover orphaned 'integrating' topics ─────


def _mk_busy_agent(db, thread_id):
    """Register a busy agent bound to ``thread_id`` (a live bound agent)."""
    agent_id = db.create_agent(role="coder", pane_id="p0")
    assert db.cas_assign_agent(agent_id, thread_id)
    return agent_id


def test_reconcile_recovers_orphaned_integrating(db):
    """DEFECT (2026-06-15): a topic stuck 'integrating' with NO bound thread/agent
    whose member tasks are ALL verified must be advanced to 'verified' — the
    integrate agent died before flipping the state. This is the recovery path."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="integrating")  # orphaned — agent died
    _mk_task(db, "n1", pid, "T1", state="verified")
    _mk_task(db, "n2", pid, "T1", state="verified")
    # thread_id IS NULL (no _bind_merged) → _verified_allowed is False, yet the
    # orphan-recovery path must still advance it.

    result = t.reconcile_topic_state(db, "T1")

    assert result == "verified"
    topic = t.get_topic(db, "T1")
    assert topic["state"] == "verified"
    assert topic["verified_at"] is not None


def test_reconcile_recovered_integrating_is_idempotent(db):
    """Re-running reconcile on a recovered orphan (now 'verified', still no bound
    thread) must NOT demote it back to 'integrating'."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="integrating")
    _mk_task(db, "n1", pid, "T1", state="verified")

    first = t.reconcile_topic_state(db, "T1")
    second = t.reconcile_topic_state(db, "T1")

    assert first == "verified"
    assert second == "verified"
    assert t.get_topic(db, "T1")["state"] == "verified"


def test_reconcile_mirror_integrating_not_advanced(db):
    """Mirror guard (commit 50b105d defense-in-depth): an is_mirror=1 topic is a
    reflection-only tracker — reconcile must NEVER advance it, even when it looks
    like an orphaned integrating topic with all tasks verified."""
    pid = _mk_project(db)
    _mk_topic(db, "M1", pid, state="integrating")
    with db._connect() as conn:
        conn.execute("UPDATE graph_topics SET is_mirror=1 WHERE id=?", ("M1",))
        conn.commit()
    _mk_task(db, "n1", pid, "M1", state="verified")

    result = t.reconcile_topic_state(db, "M1")

    assert result == "integrating"
    assert t.get_topic(db, "M1")["state"] == "integrating"
    assert t.get_topic(db, "M1")["verified_at"] is None


def test_reconcile_integrating_with_live_agent_not_advanced(db):
    """An 'integrating' topic with a LIVE bound agent is still in progress — the
    integrate is genuinely running, not orphaned. Do NOT advance to verified."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="integrating")
    _mk_task(db, "n1", pid, "T1", state="verified")
    thread_id = db.create_thread(topic="w", session_id="sessL")
    t.set_topic_thread(db, "T1", thread_id)
    _mk_busy_agent(db, thread_id)  # live bound agent → not orphaned

    result = t.reconcile_topic_state(db, "T1")

    assert result == "integrating"
    assert t.get_topic(db, "T1")["state"] == "integrating"


def test_reconcile_integrating_with_unverified_task_not_advanced(db):
    """An 'integrating' topic with a non-verified member task is still in
    progress — reconcile must not jump it to verified."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="integrating")
    _mk_task(db, "n1", pid, "T1", state="verified")
    _mk_task(db, "n2", pid, "T1", state="running")  # not all verified

    result = t.reconcile_topic_state(db, "T1")

    assert result != "verified"
    assert t.get_topic(db, "T1")["state"] != "verified"


def test_list_topics_excludes_conversational_mirror(db):
    """A conversational thread `project assign`-ed to a project gets a mirror
    (is_mirror=1) topic. That phantom must NOT appear in the project's
    graph-topic listing (it pollutes the cockpit graph pane)."""
    from dbops.db_mirror import mirror_upsert_thread

    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="pending")  # a real graph topic
    chat = db.create_thread(topic="improve dispatch", session_id="sessC")
    db.update_thread(chat, project_id=pid)
    mirror_id = mirror_upsert_thread(db, chat, pid)  # ~<uuid> phantom

    listed = {top["id"] for top in t.list_topics(db, pid)}

    assert "T1" in listed
    assert mirror_id not in listed
    assert not any(tid.startswith("~") for tid in listed)


# ── B3: doctor runs reconcile ─────────────────────────────────────────────────


def test_doctor_reconciles_drifted_topic(db, tmp_path, monkeypatch):
    """'juggle doctor' repairs drifted topic states (B3 repair valve).

    Regression pin (2026-06-11 bug J): after doctor runs, no topic should have
    a state that contradicts its member tasks.
    """
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="running")  # phantom
    _mk_task(db, "n1", pid, "T1", state="verified")
    _bind_merged(db, "T1", tmp_path)

    import juggle_db
    import juggle_cmd_doctor as doc
    monkeypatch.setattr(juggle_db, "DB_PATH", str(db.db_path))
    # Suppress config migration noise by making CONFIG_PATH point to non-existent file
    monkeypatch.setattr(doc, "CONFIG_PATH", tmp_path / "no-config.json")

    args = SimpleNamespace(dry_run=False)
    doc.cmd_doctor(args)

    assert t.get_topic(db, "T1")["state"] == "verified"
