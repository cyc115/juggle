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
    """T-verified-merged-sha: a topic only reconciles to 'verified' when it has
    a recorded merged_sha that is an ancestor of main. Give the topic a thread
    on a repo AND record main's HEAD as merged_sha so the reconcile-to-verified
    path stays exercisable under the single gate."""
    repo = tmp_path / f"repo_{topic_id}"
    repo.mkdir()

    def _git(*a):
        return subprocess.run(["git", "-C", str(repo), *a], check=True,
                              capture_output=True, text=True)

    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "t@t.t")
    _git("config", "user.name", "T")
    (repo / "f.txt").write_text("base\n")
    _git("add", ".")
    _git("commit", "-qm", "base")
    main_sha = _git("rev-parse", "main").stdout.strip()
    thread_id = db.create_thread(topic="w", session_id="sessR")
    db.update_thread(thread_id, worktree_branch="cyc_x", main_repo_path=str(repo))
    t.set_topic_thread(db, topic_id, thread_id)
    t.set_topic_merged_sha(db, topic_id, main_sha)  # main HEAD is on main


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


def _mk_topic(db, topic_id, project_id, state="open"):
    t.create_topic(db, topic_id=topic_id, project_id=project_id, title=topic_id)
    if state != "open":
        # Topic state now reads from nodes (P8 Task 4.2) — force BOTH stores so
        # the flipped reader and the legacy mirror agree.
        with db._connect() as conn:
            conn.execute(
                "UPDATE graph_topics SET state=? WHERE id=?", (state, topic_id)
            )
            conn.execute(
                "UPDATE nodes SET state=? WHERE id=? AND kind='topic'",
                (state, topic_id),
            )
            conn.commit()


def _mk_task(db, task_id, project_id, topic_id, state="open"):
    g.create_task(db, task_id=task_id, project_id=project_id, title=task_id,
                  prompt=f"do {task_id}")
    g.set_task_topic(db, task_id, topic_id)  # dual-writes graph_tasks + nodes
    if state != "open":
        # Walk the task to the desired state via mark_completion
        if state == "verified":
            g.mark_completion(db, task_id, integrate_ok=True, verify_ok=True)
        elif state == "failed-verify":
            g.mark_completion(db, task_id, integrate_ok=True, verify_ok=False)
        elif state == "failed-exec":
            g.mark_exec_failed(db, task_id)
        elif state in ("running", "dispatching", "integrating"):
            # manually set for simplicity — force the state in BOTH stores
            # (task readers now read nodes; P8 Task 4.1).
            with db._connect() as conn:
                conn.execute(
                    "UPDATE graph_tasks SET state=? WHERE id=?", (state, task_id)
                )
                conn.execute(
                    "UPDATE nodes SET state=? WHERE id=?", (state, task_id)
                )
                conn.commit()


# ── reconcile_topic_state unit tests ──────────────────────────────────────────


def test_reconcile_sets_topic_verified_when_all_tasks_verified(db, tmp_path):
    """All member tasks verified AND work merged → topic becomes verified."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="open")
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
    _mk_topic(db, "T1", pid, state="open")
    _mk_task(db, "n1", pid, "T1", state="verified")
    _mk_task(db, "n2", pid, "T1", state="failed-verify")

    result = t.reconcile_topic_state(db, "T1")

    assert result == "failed-verify"
    assert t.get_topic(db, "T1")["state"] == "failed-verify"


def test_reconcile_running_member_sets_topic_running(db):
    """Any member task running/dispatching/integrating → topic becomes running."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="open")
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
    _mk_task(db, "n2", pid, "T1", state="open")  # last unverified
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
    _mk_topic(db, "T2", pid, state="open")
    _mk_task(db, "n2", pid, "T2", state="open")

    args = SimpleNamespace(project=pid, json_out=False, db_path=str(db.db_path))
    cg.cmd_graph_reconcile(args)

    out = capsys.readouterr().out
    assert "T1" in out
    assert "running" in out
    assert "verified" in out
    assert t.get_topic(db, "T1")["state"] == "verified"
    assert t.get_topic(db, "T2")["state"] == "open"  # unchanged


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


def test_reconcile_orphan_without_merge_proof_stays_integrating(db):
    """T-verified-merged-sha (closes the _orphan_recoverable hole): a topic stuck
    'integrating' with NO bound thread/agent and NO recorded merged_sha must
    NEVER be advanced to 'verified' — the old orphan-recovery bypass was a
    false-verified hole. It stays 'integrating' (needs-attention)."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="integrating")  # orphaned — agent died
    _mk_task(db, "n1", pid, "T1", state="verified")
    _mk_task(db, "n2", pid, "T1", state="verified")
    # thread_id IS NULL and merged_sha IS NULL → no merge proof → never verified.

    result = t.reconcile_topic_state(db, "T1")

    assert result == "integrating"
    topic = t.get_topic(db, "T1")
    assert topic["state"] == "integrating"
    assert topic["verified_at"] is None


def test_reconcile_verified_is_terminal_idempotent(db):
    """A genuinely-verified topic must stay 'verified' on re-reconcile (the
    idempotency guarantee that previously rode on _orphan_recoverable)."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="verified")
    _mk_task(db, "n1", pid, "T1", state="verified")

    first = t.reconcile_topic_state(db, "T1")
    second = t.reconcile_topic_state(db, "T1")

    assert first == "verified"
    assert second == "verified"
    assert t.get_topic(db, "T1")["state"] == "verified"


def test_reconcile_skips_conversation_nodes(db):
    """P8 Task 4.2 (replaces the deleted is_mirror-tracker guard): a conversation
    node `project assign`-ed to a project is kind='conversation', so it is NEVER
    in the topic set (kind='task' AND parent_id IS NULL) — reconcile_project_topics
    must not touch it (the structural successor to 'mirror topics never advanced').
    """
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="open")
    _mk_task(db, "n1", pid, "T1", state="verified")
    chat = db.create_thread(topic="improve dispatch", session_id="sessC")
    db.update_thread(chat, project_id=pid)  # conversation node now in this project

    result = t.reconcile_project_topics(db, pid)

    assert chat not in result, "a conversation node must never be reconciled as a topic"
    assert "T1" in result


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


def test_list_topics_excludes_conversation_nodes(db):
    """P8 Task 4.2 (replaces the deleted db_mirror phantom test): a conversational
    thread `project assign`-ed to a project is a kind='conversation' node, NOT a
    graph topic — it must NOT appear in the project's topic listing (which reads
    kind='task' AND parent_id IS NULL), so it can never pollute the cockpit graph
    pane the way the old ~<uuid> mirror projection did."""
    pid = _mk_project(db)
    _mk_topic(db, "T1", pid, state="open")  # a real graph topic
    chat = db.create_thread(topic="improve dispatch", session_id="sessC")
    db.update_thread(chat, project_id=pid)  # conversation node assigned to pid

    listed = {top["id"] for top in t.list_topics(db, pid)}

    assert "T1" in listed
    assert chat not in listed


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
