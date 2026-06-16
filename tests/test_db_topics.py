"""dbops.db_topics — topic CRUD, shared state machine, DERIVED topic deps
(task edges crossing topic boundaries), ready-set, completion (R9 3-tier)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as t  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "topics.db"))
    d.init_db()
    return d


def _topic(db, tid, project="INBOX", **kw):
    t.create_topic(db, topic_id=tid, project_id=project,
                   title=kw.get("title", f"Topic {tid}"),
                   objective=kw.get("objective", ""))


def _task(db, nid, topic_id, deps=()):
    g.create_task(db, task_id=nid, project_id="INBOX", title=nid, prompt=f"do {nid}")
    with db._connect() as conn:
        conn.execute("UPDATE graph_tasks SET topic_id=? WHERE id=?", (topic_id, nid))
        conn.commit()
    if deps:
        g.replace_edges(db, nid, list(deps))


def _bind_merged(db, topic_id, tmp_path):
    """T-verified-merged-sha: topic→verified requires a recorded merged_sha that
    is an ancestor of main. Bind the topic to a thread on a repo and record
    main's HEAD so the verify path stays reachable under the single gate."""
    import subprocess
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
    thread_id = db.create_thread(topic="w", session_id="s")
    db.update_thread(thread_id, worktree_branch="cyc_x", main_repo_path=str(repo))
    t.set_topic_thread(db, topic_id, thread_id)
    t.set_topic_merged_sha(db, topic_id, main_sha)


def test_topic_uses_task_state_machine(db):
    _topic(db, "ta")
    assert t.get_topic(db, "ta")["state"] == "pending"
    assert t.topic_transition(db, "ta", "deps_ready") == "ready"
    assert t.topic_transition(db, "ta", "claim") == "dispatching"
    with pytest.raises(ValueError):
        t.topic_transition(db, "ta", "integrate_ok")  # illegal from dispatching


def test_derived_topic_deps_from_cross_topic_task_edges(db):
    """Topic A depends on topic B iff any task of A has an edge to a task of B.
    Intra-topic edges must NOT create a self-dep."""
    _topic(db, "A"); _topic(db, "B")
    _task(db, "b1", "B")
    _task(db, "a1", "A")
    _task(db, "a2", "A", deps=("a1", "b1"))  # intra (a1) + cross (b1)
    assert t.derived_topic_deps(db, "A") == ["B"]
    assert t.derived_topic_deps(db, "B") == []


def test_topic_ready_requires_dep_topics_verified(db, tmp_path):
    _topic(db, "A"); _topic(db, "B")
    _task(db, "b1", "B")
    _task(db, "a1", "A", deps=("b1",))
    assert t.recompute_topic_ready(db, "INBOX") == ["B"]  # A blocked on B
    assert t.get_topic(db, "A")["state"] == "pending"
    _bind_merged(db, "B", tmp_path)  # G1: B's work merged → can verify
    for ev in ("claim", "dispatch", "integrate_start", "integrate_ok"):
        t.topic_transition(db, "B", ev)
    assert t.recompute_topic_ready(db, "INBOX") == ["A"]


def test_list_topic_tasks_topological_order(db):
    """The agent executes tasks sequentially in intra-topic dependency order."""
    _topic(db, "A")
    _task(db, "a1", "A")
    _task(db, "a3", "A", deps=("a2",))
    _task(db, "a2", "A", deps=("a1",))
    assert [n["id"] for n in t.list_topic_tasks(db, "A")] == ["a1", "a2", "a3"]


def test_mark_topic_completion_maps_outcomes(db, tmp_path):
    _topic(db, "A")
    _bind_merged(db, "A", tmp_path)  # G1: A's work merged → can verify
    for ev in ("deps_ready", "claim", "dispatch"):
        t.topic_transition(db, "A", ev)
    state = t.mark_topic_completion(db, "A", integrate_ok=True, verify_ok=True,
                                    handoff="done")
    assert state == "verified"
    assert t.get_topic(db, "A")["handoff"] == "done"


def test_topic_counts_shape(db):
    _topic(db, "A"); _topic(db, "B")
    c = t.topic_counts(db, "INBOX")
    assert c["total"] == 2 and c["pending"] == 2
