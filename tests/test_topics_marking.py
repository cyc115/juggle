"""dbops.db_topics_marking — mark_topic_completion event selection by landing
mode (SPEC §5.1/§5.2): a caller that knows integrate only SUBMITTED the work
(async-land queue: push_mode="pr" or an async_land-capable backend) passes
``submitted_rev`` so the topic lands on 'integrated-unlanded' instead of racing
the (fail-closed) verified gate — never sets merged_sha.
"""
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


def _topic(db, tid, project="INBOX"):
    t.create_topic(db, topic_id=tid, project_id=project, title=f"Topic {tid}")


def _task(db, nid, topic_id):
    g.create_task(db, task_id=nid, project_id="INBOX", title=nid, prompt=f"do {nid}")
    g.set_task_topic(db, nid, topic_id)
    g.task_transition(db, nid, "deps_ready")
    g.task_transition(db, nid, "claim")
    g.task_transition(db, nid, "dispatch")
    g.task_transition(db, nid, "integrate_start")
    g.task_transition(db, nid, "integrate_ok")


def test_submitted_rev_lands_on_integrated_unlanded(db):
    _topic(db, "T1")
    _task(db, "n1", "T1")

    state = t.mark_topic_completion(db, "T1", integrate_ok=True, submitted_rev="cyc_T1")

    assert state == "integrated-unlanded"
    topic = t.get_topic(db, "T1")
    assert topic["state"] == "integrated-unlanded"


def test_submitted_rev_never_sets_merged_sha(db):
    _topic(db, "T2")
    _task(db, "n2", "T2")

    t.mark_topic_completion(db, "T2", integrate_ok=True, submitted_rev="cyc_T2")

    topic = t.get_topic(db, "T2")
    assert not topic.get("merged_sha")


def test_submitted_rev_recorded_on_node(db):
    _topic(db, "T3")
    _task(db, "n3", "T3")

    t.mark_topic_completion(db, "T3", integrate_ok=True, submitted_rev="cyc_T3")

    topic = t.get_topic(db, "T3")
    assert topic["submitted_rev"] == "cyc_T3"


def test_no_submitted_rev_keeps_existing_integrate_ok_edge(db, tmp_path):
    """Regression pin (SPEC §6a): git-direct (submitted_rev=None) still goes
    straight to 'verified' — unlanded-state is purely additive."""
    import subprocess

    repo = tmp_path / "repo_t4"
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

    _topic(db, "T4")
    _task(db, "n4", "T4")
    thread_id = db.create_thread(topic="w", session_id="s")
    db.update_thread(thread_id, worktree_branch="cyc_x", main_repo_path=str(repo))
    t.set_topic_thread(db, "T4", thread_id)
    t.set_topic_merged_sha(db, "T4", main_sha)

    state = t.mark_topic_completion(db, "T4", integrate_ok=True)

    assert state == "verified"


def test_submitted_rev_idempotent_on_already_unlanded(db):
    """Mirrors the existing already-verified idempotency guard (Bug I,
    2026-06-11): a racing double-completion on an already-integrated-unlanded
    topic must not raise."""
    _topic(db, "T5")
    _task(db, "n5", "T5")
    t.mark_topic_completion(db, "T5", integrate_ok=True, submitted_rev="cyc_T5")

    state = t.mark_topic_completion(db, "T5", integrate_ok=True, submitted_rev="cyc_T5")

    assert state == "integrated-unlanded"


def test_db_topics_marking_importable_before_db_topics():
    """Regression pin (2026-07-04, red-suite repair): db_topics_marking and
    db_topics form a re-export cycle (marking's top-level `from dbops.db_topics
    import ...` ↔ db_topics's bottom `from dbops.db_topics_marking import ...`).
    Whichever module loads FIRST left the other's names undefined, so importing
    db_topics_marking before db_topics raised `ImportError: cannot import name
    'mark_topic_completion' from partially initialized module`. Any test whose
    import chain reached db_topics_marking first (e.g. test_integrate importing
    juggle_cmd_integrate) died on collect. A FRESH interpreter is required —
    this module already imported db_topics at top, which masks the order bug."""
    import subprocess

    src = os.path.join(os.path.dirname(__file__), "..", "src")
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); "
         "import dbops.db_topics_marking as m; "
         "assert callable(m.mark_topic_completion); print('ok')",
         src],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout
