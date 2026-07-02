"""Tests for juggle_integrate_mergedsha._record_merged_sha routed through the
VCS seam (vcs-route-queries: A4 — dbops/graph_guards.py's sibling module).

The phantom-SHA / not-yet-ancestor guards must stay fail-closed after routing
through ``backend_for(repo).resolve``/``is_ancestor`` instead of raw
``subprocess.run(["git", ...])`` (facts §A4).
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_integrate_mergedsha import _record_merged_sha  # noqa: E402


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check
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
    return r


class _FakeDB:
    def __init__(self, topic):
        self._topic = topic
        self.recorded = None


def _monkeypatch_topic(monkeypatch, topic):
    from dbops import db_topics

    monkeypatch.setattr(db_topics, "get_topic_by_thread", lambda db, tid: topic)

    def _set(db, topic_id, sha):
        db.recorded = (topic_id, sha)

    monkeypatch.setattr(db_topics, "set_topic_merged_sha", _set)


def test_records_sha_that_is_ancestor_of_main(repo, monkeypatch):
    topic = {"id": "T1"}
    db = _FakeDB(topic)
    _monkeypatch_topic(monkeypatch, topic)

    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _record_merged_sha(db, "thread-1", str(repo), "main")

    assert db.recorded == ("T1", head)


def test_no_topic_bound_is_noop(repo, monkeypatch):
    db = _FakeDB(None)
    _monkeypatch_topic(monkeypatch, None)

    _record_merged_sha(db, "thread-1", str(repo), "main")

    assert db.recorded is None


def test_phantom_ref_never_recorded(repo, monkeypatch):
    topic = {"id": "T1"}
    db = _FakeDB(topic)
    _monkeypatch_topic(monkeypatch, topic)

    _record_merged_sha(db, "thread-1", str(repo), "deadbeefdeadbeefdead")

    assert db.recorded is None


def test_offmain_sha_never_recorded(repo, monkeypatch):
    topic = {"id": "T1"}
    db = _FakeDB(topic)
    _monkeypatch_topic(monkeypatch, topic)

    _git(repo, "checkout", "-q", "-b", "cyc_side")
    (repo / "s.txt").write_text("s\n")
    _git(repo, "add", "s.txt")
    _git(repo, "commit", "-q", "-m", "side")
    side_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "main")

    _record_merged_sha(db, "thread-1", str(repo), side_sha)

    assert db.recorded is None
