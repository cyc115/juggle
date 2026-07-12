"""juggle_land_poller — watchdog land-poller sweep (SPEC §5.3).

FakeBackend (async_land=True) pins per SPEC §7.3: submitted rev X, landed rev
Y != X (squash-on-land) — the poller promotes via land_status's landed_rev,
never false-verifies off the stale submitted_rev, and times out to a loud
failure when land_status stays pending past JUGGLE_LAND_TIMEOUT_H.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "helpers"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as tp  # noqa: E402
from vcs_types import LandStatus  # noqa: E402
from fake_vcs import FakeBackend  # noqa: E402

import juggle_land_poller as lp  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "land.db"))
    d.init_db()
    return d


def _merged_repo(tmp_path) -> str:
    """A real repo whose 'main' satisfies the G1 verified<=>merged guard."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True,
                       capture_output=True, text=True)

    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "t@t.t")
    _git("config", "user.name", "T")
    (repo / "f.txt").write_text("base\n")
    _git("add", ".")
    _git("commit", "-qm", "base")
    return str(repo)


def _unlanded_topic(db, tid, repo, *, submitted_rev="cyc_T", worktree_path="",
                    project="INBOX"):
    """Build a topic sitting at 'integrated-unlanded' with a bound thread."""
    tp.create_topic(db, topic_id=tid, project_id=project, title=f"Topic {tid}")
    nid = f"{tid}-k0"
    g.create_task(db, task_id=nid, project_id=project, title=nid, prompt="p")
    g.set_task_topic(db, nid, tid)

    thread_id = db.create_thread("w", session_id="s")
    db.update_thread(thread_id, worktree_branch=submitted_rev,
                     main_repo_path=repo, worktree_path=worktree_path)
    tp.set_topic_thread(db, tid, thread_id)

    tp.recompute_topic_ready(db, project)
    for ev in ("claim", "dispatch", "integrate_start"):
        tp.topic_transition(db, tid, ev)
    tp.mark_topic_completion(db, tid, integrate_ok=True, submitted_rev=submitted_rev)
    assert tp.get_topic(db, tid)["state"] == "integrated-unlanded"
    return thread_id


def test_landed_promotes_to_verified_via_landed_rev_not_submitted_rev(db, tmp_path, monkeypatch):
    """SPEC §7.3 squash-on-land pin: landed_rev Y != submitted_rev X — merged_sha
    must be Y (the real ancestor), never the stale submitted ticket."""
    repo = _merged_repo(tmp_path)
    import subprocess
    landed_sha = subprocess.run(["git", "-C", repo, "rev-parse", "main"],
                                capture_output=True, text=True).stdout.strip()

    _unlanded_topic(db, "T1", repo, submitted_rev="cyc_T1")

    fake = FakeBackend()
    fake.scripted["land_status"] = LandStatus(state="landed", landed_rev=landed_sha)
    monkeypatch.setattr(lp, "backend_for", lambda r: fake)

    stats = lp.poll_unlanded_topics(db, "INBOX")

    assert stats["landed"] == ["T1"]
    topic = tp.get_topic(db, "T1")
    assert topic["state"] == "verified"
    assert topic["merged_sha"] == landed_sha
    assert topic["merged_sha"] != "cyc_T1"


def test_pending_leaves_topic_unlanded(db, tmp_path, monkeypatch):
    repo = _merged_repo(tmp_path)
    _unlanded_topic(db, "T2", repo)

    fake = FakeBackend()
    fake.scripted["land_status"] = LandStatus(state="unlanded")
    monkeypatch.setattr(lp, "backend_for", lambda r: fake)

    stats = lp.poll_unlanded_topics(db, "INBOX")

    assert stats["still_unlanded"] == ["T2"]
    assert tp.get_topic(db, "T2")["state"] == "integrated-unlanded"


def test_landed_but_not_yet_ancestor_stays_pending(db, tmp_path, monkeypatch):
    """G1 gate pin: topic_transition RAISES UnmergedVerifyRefused (not a
    non-'verified' return) when landed_rev isn't (yet) an ancestor of main
    (e.g. remote not fetched) — the poller must catch it and retry next tick,
    never crash or false-verify."""
    repo = _merged_repo(tmp_path)
    _unlanded_topic(db, "T8", repo)

    fake = FakeBackend()
    fake.scripted["land_status"] = LandStatus(state="landed", landed_rev="not-an-ancestor-sha")
    monkeypatch.setattr(lp, "backend_for", lambda r: fake)

    stats = lp.poll_unlanded_topics(db, "INBOX")

    assert stats["still_unlanded"] == ["T8"]
    assert stats["errors"] == []
    topic = tp.get_topic(db, "T8")
    assert topic["state"] == "integrated-unlanded"


def test_timeout_land_fails_with_high_action_item(db, tmp_path, monkeypatch):
    repo = _merged_repo(tmp_path)
    _unlanded_topic(db, "T3", repo)
    old = (datetime.now(timezone.utc) - timedelta(hours=200)).isoformat()
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET updated_at=? WHERE id='T3'", (old,))
        conn.commit()

    fake = FakeBackend()
    fake.scripted["land_status"] = LandStatus(state="unlanded")
    monkeypatch.setattr(lp, "backend_for", lambda r: fake)
    monkeypatch.setenv("JUGGLE_LAND_TIMEOUT_H", "72")

    stats = lp.poll_unlanded_topics(db, "INBOX")

    assert stats["timed_out"] == ["T3"]
    topic = tp.get_topic(db, "T3")
    assert topic["state"] == "failed-integration"
    items = db.get_open_action_items()
    assert any("land timed out" in i["message"] for i in items)
    assert any(i["priority"] == "high" for i in items)


def test_land_failed_status_fails_immediately(db, tmp_path, monkeypatch):
    """A backend that reports 'failed' (e.g. rejected diff) must not wait
    out the timeout — fail loud right away."""
    repo = _merged_repo(tmp_path)
    _unlanded_topic(db, "T4", repo)

    fake = FakeBackend()
    fake.scripted["land_status"] = LandStatus(state="failed", detail="rejected")
    monkeypatch.setattr(lp, "backend_for", lambda r: fake)

    stats = lp.poll_unlanded_topics(db, "INBOX")

    assert stats["timed_out"] == ["T4"]
    assert tp.get_topic(db, "T4")["state"] == "failed-integration"


def test_landed_tears_down_workspace(db, tmp_path, monkeypatch):
    """Deferred workspace teardown (SPEC §5.3): remove_workspace happens ONLY
    on confirmed land, not at submit time, and clears the thread's worktree
    fields afterward."""
    repo = _merged_repo(tmp_path)
    import subprocess
    landed_sha = subprocess.run(["git", "-C", repo, "rev-parse", "main"],
                                capture_output=True, text=True).stdout.strip()
    thread_id = _unlanded_topic(db, "T5", repo, worktree_path=str(tmp_path / "wt5"))

    fake = FakeBackend()
    fake.scripted["land_status"] = LandStatus(state="landed", landed_rev=landed_sha)
    monkeypatch.setattr(lp, "backend_for", lambda r: fake)

    lp.poll_unlanded_topics(db, "INBOX")

    assert ("remove_workspace", (repo, str(tmp_path / "wt5")), {}) in fake.calls
    thread = db.get_thread(thread_id)
    assert thread["worktree_path"] == ""


def test_missing_repo_or_ticket_is_an_error_not_a_crash(db):
    tp.create_topic(db, topic_id="T6", project_id="INBOX", title="Topic T6")
    nid = "T6-k0"
    g.create_task(db, task_id=nid, project_id="INBOX", title=nid, prompt="p")
    g.set_task_topic(db, nid, "T6")
    tp.recompute_topic_ready(db, "INBOX")
    for ev in ("claim", "dispatch", "integrate_start"):
        tp.topic_transition(db, "T6", ev)
    tp.mark_topic_completion(db, "T6", integrate_ok=True, submitted_rev="cyc_T6")

    stats = lp.poll_unlanded_topics(db, "INBOX")

    assert stats["errors"] == ["T6"]
    assert tp.get_topic(db, "T6")["state"] == "integrated-unlanded"


def test_ignores_topics_not_in_unlanded_state(db, tmp_path, monkeypatch):
    _merged_repo(tmp_path)
    tp.create_topic(db, topic_id="T7", project_id="INBOX", title="Topic T7")
    fake = FakeBackend()
    monkeypatch.setattr(lp, "backend_for", lambda r: fake)

    stats = lp.poll_unlanded_topics(db, "INBOX")

    assert stats == {"landed": [], "still_unlanded": [], "timed_out": [], "errors": []}
    assert fake.calls == []
