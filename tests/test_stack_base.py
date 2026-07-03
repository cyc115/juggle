"""Tests for juggle_stack_base.stack_base — SPEC §4 stack-relative base ref.

Regression pins (SPEC §6): (c) trunk when deps landed / dep-tip when unlanded,
(d) >=2 unlanded deps fails loud with a manual_step action item.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics  # noqa: E402
from vcs import backend_for  # noqa: E402
from juggle_stack_base import TooManyUnlandedDeps, stack_base  # noqa: E402


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("one\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "first")
    return str(r)


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "stack.db"))
    d.init_db()
    return d


def _mk_topic(db, topic_id, project_id="INBOX"):
    db_topics.create_topic(db, topic_id=topic_id, project_id=project_id, title=topic_id)


def _bind_dep(db, topic_id, dep_topic_id):
    """Fake a cross-topic dep edge: a task under topic_id deps on a task under
    dep_topic_id (derived_topic_deps derives topic-level deps from task edges)."""
    task_a = f"{topic_id}-t"
    task_b = f"{dep_topic_id}-t"
    if g.get_task(db, task_a) is None:
        g.create_task(db, task_id=task_a, project_id="INBOX", title=task_a, prompt="x")
        g.set_task_topic(db, task_a, topic_id)
    if g.get_task(db, task_b) is None:
        g.create_task(db, task_id=task_b, project_id="INBOX", title=task_b, prompt="x")
        g.set_task_topic(db, task_b, dep_topic_id)
    g.replace_edges(db, task_a, list(set(g.get_deps(db, task_a)) | {task_b}))


def test_stack_base_zero_unlanded_deps_returns_trunk(db, repo):
    _mk_topic(db, "topic-a")
    backend = backend_for(repo)
    trunk = backend.trunk(repo)

    assert stack_base(db, "topic-a", repo, backend) == trunk


def test_stack_base_one_unlanded_dep_returns_dep_submitted_rev(db, repo):
    _mk_topic(db, "topic-a")
    _mk_topic(db, "topic-dep")
    _bind_dep(db, "topic-a", "topic-dep")
    db_topics.set_topic_submitted_rev(db, "topic-dep", "deadbeef")
    backend = backend_for(repo)

    assert stack_base(db, "topic-a", repo, backend) == "deadbeef"


def test_stack_base_landed_dep_is_not_unlanded_falls_to_trunk(db, repo, monkeypatch):
    _mk_topic(db, "topic-a")
    _mk_topic(db, "topic-dep")
    _bind_dep(db, "topic-a", "topic-dep")
    db_topics.set_topic_submitted_rev(db, "topic-dep", "deadbeef")
    monkeypatch.setattr(
        "juggle_stack_base.topic_is_merged", lambda db_, tid: tid == "topic-dep"
    )
    backend = backend_for(repo)

    assert stack_base(db, "topic-a", repo, backend) == backend.trunk(repo)


def test_stack_base_two_unlanded_deps_fails_loud(db, repo):
    _mk_topic(db, "topic-a")
    _mk_topic(db, "topic-dep1")
    _mk_topic(db, "topic-dep2")
    _bind_dep(db, "topic-a", "topic-dep1")
    _bind_dep(db, "topic-a", "topic-dep2")
    backend = backend_for(repo)
    db.add_action_item = Mock()

    with pytest.raises(TooManyUnlandedDeps):
        stack_base(db, "topic-a", repo, backend)

    assert db.add_action_item.called
    kwargs = db.add_action_item.call_args.kwargs
    assert kwargs["type_"] == "manual_step"
