"""Async-land integrate wiring — the DEAD-path revival (SPEC 2026-07-05).

Incident named by every pin below: "async-land path dead — submitted ticket
dropped, 2026-07-05". The async-land lifecycle (integrated-unlanded state,
submitted_rev, the land poller, integrate_submitted/land_confirmed edges) was
fully built but had NO production caller: juggle_cmd_integrate handled a
``submit()`` returning ``status="submitted"`` by tearing down the worktree and
returning True WITHOUT recording the ticket or advancing the topic, so the land
poller had nothing to sweep. These pins revive it (Option B): publish advances
the topic to the NON-terminal 'integrated-unlanded' (worktree torn down, agent
freed), and the land poller confirms the real land and only THEN flips it to
'verified' with a genuine merged_sha.

Everything is driven with FakeBackend (async_land=True) + temp git repos — this
machine has NO sl/Phabricator, and none is needed: the seam is capability-gated.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "helpers"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as tp  # noqa: E402
from dbops import graph_guards as gg  # noqa: E402
from vcs_types import Capabilities, LandStatus, SubmitResult  # noqa: E402
from fake_vcs import FakeBackend  # noqa: E402

import juggle_cmd_integrate as ci  # noqa: E402
import juggle_land_poller as lp  # noqa: E402


# -- fixtures / helpers ------------------------------------------------------

@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "async_land.db"))
    d.init_db()
    return d


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True,
                   capture_output=True, text=True)


def _merged_repo(tmp_path, *, branch="main") -> str:
    """A real repo whose ``branch`` HEAD satisfies the G1 verified<=>merged gate."""
    repo = tmp_path / f"repo_{branch}"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", branch)
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "T")
    (repo / "f.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return str(repo)


def _head_sha(repo, ref) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", ref],
                          capture_output=True, text=True).stdout.strip()


def _integrating_topic(db, tid, repo, *, worktree_path, project="INBOX"):
    """Topic bound to a thread + worktree, walked to 'integrating' with one
    verified member task — the shape a detached integrate sees."""
    tp.create_topic(db, topic_id=tid, project_id=project, title=f"Topic {tid}")
    nid = f"{tid}-k0"
    g.create_task(db, task_id=nid, project_id=project, title=nid, prompt="p")
    g.set_task_topic(db, nid, tid)
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET state='verified' WHERE id=?", (nid,))
        conn.commit()
    thread_id = db.create_thread("w", session_id="s")
    db.update_thread(thread_id, worktree_path=str(worktree_path),
                     worktree_branch="cyc_AL", main_repo_path=str(repo))
    tp.set_topic_thread(db, tid, thread_id)
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start"):
        tp.topic_transition(db, tid, ev)
    return thread_id


def _run_async_integrate(db, monkeypatch, tmp_path, thread_id, *, ticket="D123",
                         async_land=True, submit=None):
    """Drive the REAL _run_integrate with FakeBackend injected so submit() returns
    a scripted result. Mirrors cmd_complete_agent's detached integrate."""
    fake = FakeBackend()
    fake.capabilities = Capabilities(async_land=async_land, auto_restack=False)
    fake.scripted["has_changes"] = True
    fake.scripted["submit"] = submit or SubmitResult(status="submitted", ticket=ticket)

    monkeypatch.setattr(ci, "backend_for", lambda r: fake)
    monkeypatch.setattr(ci, "get_repo_config",
                        lambda r: {"push_mode": "pr", "test_cmd": "",
                                   "vcs": None, "trunk": "main", "async_land": None})
    monkeypatch.setattr("juggle_integrate_lock._get_lock_path",
                        lambda repo: tmp_path / "t.lock")
    thread = db.get_thread(thread_id)
    return fake, ci._run_integrate(thread, db)


# -- PIN 1: submitted diff -> integrated-unlanded, submitted_rev recorded ----

def test_submitted_records_ticket_and_frees_worktree(db, tmp_path, monkeypatch):
    """PIN (async-land path dead — submitted ticket dropped, 2026-07-05): after an
    async publish the topic is 'integrated-unlanded' with submitted_rev==ticket,
    merged_sha is NULL (never verified early), the worktree is torn down, and
    main_repo_path is KEPT so the land poller can still resolve repo+ticket."""
    repo = _merged_repo(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    thread_id = _integrating_topic(db, "T-al2", repo, worktree_path=wt)

    _fake, (ok, msg) = _run_async_integrate(db, monkeypatch, tmp_path, thread_id,
                                            ticket="D42")

    assert ok, msg
    topic = tp.get_topic(db, "T-al2")
    assert topic["state"] == "integrated-unlanded"
    assert topic["submitted_rev"] == "D42"
    assert not topic["merged_sha"]
    thread = db.get_thread(thread_id)
    assert thread["worktree_path"] == ""            # worktree freed
    assert thread["main_repo_path"] == str(repo)     # kept for the land poller


# -- PIN 2: full integrate -> poller -> verified (real merged_sha) -----------

def test_full_path_integrate_then_poller_lands_to_verified(db, tmp_path, monkeypatch):
    """PIN (async-land path dead — submitted ticket dropped, 2026-07-05): the FULL
    path — a real _run_integrate submit()->'submitted' feeds the land poller,
    which on 'landed' writes the REAL landed rev as merged_sha and flips the topic
    to 'verified' through the UNCHANGED topic_is_merged ancestor gate. Not a
    poller unit test: the integrate step is what was dead."""
    repo = _merged_repo(tmp_path)
    landed_sha = _head_sha(repo, "main")
    wt = tmp_path / "wt"
    wt.mkdir()
    thread_id = _integrating_topic(db, "T-al3", repo, worktree_path=wt)

    _fake, (ok, _msg) = _run_async_integrate(db, monkeypatch, tmp_path, thread_id,
                                             ticket="D99")
    assert ok
    assert tp.get_topic(db, "T-al3")["state"] == "integrated-unlanded"

    poll_fake = FakeBackend()
    poll_fake.scripted["land_status"] = LandStatus(state="landed", landed_rev=landed_sha)
    monkeypatch.setattr(lp, "backend_for", lambda r: poll_fake)

    stats = lp.poll_unlanded_topics(db, "INBOX")

    assert stats["landed"] == ["T-al3"]
    healed = tp.get_topic(db, "T-al3")
    assert healed["state"] == "verified"
    assert healed["merged_sha"] == landed_sha


# -- PIN 3: invariant — submitted-but-unlanded is NEVER verified -------------

def test_unlanded_topic_never_verified_ancestor_gate_refuses(db, tmp_path, monkeypatch):
    """PIN (async-land path dead — submitted ticket dropped, 2026-07-05): a
    published-but-unlanded topic must NEVER be 'verified'. It has no merged_sha
    (topic_is_merged False), and forcing land_confirmed with a non-ancestor rev
    raises UnmergedVerifyRefused — the verified<=>merged invariant stays honest."""
    repo = _merged_repo(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    thread_id = _integrating_topic(db, "T-al4", repo, worktree_path=wt)
    _run_async_integrate(db, monkeypatch, tmp_path, thread_id, ticket="D7")

    assert tp.get_topic(db, "T-al4")["state"] == "integrated-unlanded"
    assert gg.topic_is_merged(db, "T-al4") is False

    # A land_confirmed with a bogus (non-ancestor) rev must be refused, not verify.
    tp.set_topic_merged_sha(db, "T-al4", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    with pytest.raises(tp.UnmergedVerifyRefused):
        tp.topic_transition(db, "T-al4", "land_confirmed")
    assert tp.get_topic(db, "T-al4")["state"] == "integrated-unlanded"


# -- PIN: mark_graph_topic threads submitted_rev (SPEC touch point #2) -------

def test_mark_graph_topic_threads_submitted_rev_to_integrated_unlanded(db, tmp_path):
    """PIN (async-land path dead — submitted ticket dropped, 2026-07-05):
    mark_graph_topic must thread a topic's recorded submitted_rev into
    mark_topic_completion so completion lands on 'integrated-unlanded' — NOT race
    the fail-closed verified gate (pre-fix it passed submitted_rev=None, raised
    UnmergedVerifyRefused, self-healed to nothing, and filed a bogus HIGH
    failure). It must emit no failure action item for an async-pending topic."""
    from juggle_cmd_agents_graph_topics import mark_graph_topic

    repo = _merged_repo(tmp_path)
    tp.create_topic(db, topic_id="T-al5", project_id="INBOX", title="F")
    nid = "T-al5-k0"
    g.create_task(db, task_id=nid, project_id="INBOX", title=nid, prompt="p")
    g.set_task_topic(db, nid, "T-al5")
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET state='verified' WHERE id=?", (nid,))
        conn.commit()
    thread_id = db.create_thread("w", session_id="s")
    db.update_thread(thread_id, worktree_branch="cyc_AL", main_repo_path=str(repo))
    tp.set_topic_thread(db, "T-al5", thread_id)
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start"):
        tp.topic_transition(db, "T-al5", ev)
    tp.set_topic_submitted_rev(db, "T-al5", "D500")  # integrate persisted the ticket

    mark_graph_topic(db, thread_id, integrate_ok=True, handoff="h", session_id="s")

    assert tp.get_topic(db, "T-al5")["state"] == "integrated-unlanded"
    items = db.get_open_action_items()
    assert not any(i.get("type") == "failure" for i in items), \
        "an async-pending topic must not file a failure action item"


# -- PIN 4: configurable trunk (git-ism sweep) -------------------------------

def test_ancestor_gate_uses_configured_trunk(db, tmp_path, monkeypatch):
    """PIN (async-land path dead — submitted ticket dropped, 2026-07-05): the
    ancestor gate must read the trunk name from repo config, so a non-'main'
    trunk (e.g. Sapling remote/main) doesn't false-refuse. Pre-fix the guards
    hardcoded main='main' and returned False for a develop-only repo."""
    repo = _merged_repo(tmp_path, branch="develop")
    develop_sha = _head_sha(repo, "develop")

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"repos": {repo: {"trunk": "develop"}}}))
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(cfg))

    assert gg.sha_is_ancestor(repo, develop_sha) is True

    # topic_is_merged also picks up the configured trunk.
    tp.create_topic(db, topic_id="T-dev", project_id="INBOX", title="F")
    thread_id = db.create_thread("w", session_id="s")
    db.update_thread(thread_id, worktree_branch="cyc_x", main_repo_path=repo)
    tp.set_topic_thread(db, "T-dev", thread_id)
    tp.set_topic_merged_sha(db, "T-dev", develop_sha)
    assert gg.topic_is_merged(db, "T-dev") is True


def test_ancestor_gate_defaults_to_main_for_git(db, tmp_path, monkeypatch):
    """GUARD: with NO trunk configured, the gate defaults to 'main' — existing
    git behavior unchanged (a sha on main is an ancestor of main)."""
    repo = _merged_repo(tmp_path)  # branch main
    main_sha = _head_sha(repo, "main")
    cfg = tmp_path / "empty.json"
    cfg.write_text("{}")
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(cfg))

    assert gg.sha_is_ancestor(repo, main_sha) is True


# -- GUARD: synchronous git path unchanged -----------------------------------

def test_synchronous_landed_path_still_verifies_in_one_shot(db, tmp_path, monkeypatch):
    """GUARD: an async_land=False backend that lands synchronously
    (submit()->'landed') keeps the pre-existing behavior — merged_sha recorded,
    topic reaches 'verified' via the integrate_ok edge, NEVER integrated-unlanded.
    Async-land is purely additive (no regression to git-direct)."""
    repo = _merged_repo(tmp_path)
    landed_sha = _head_sha(repo, "main")
    wt = tmp_path / "wt"
    wt.mkdir()
    thread_id = _integrating_topic(db, "T-sync", repo, worktree_path=wt)

    _fake, (ok, _msg) = _run_async_integrate(
        db, monkeypatch, tmp_path, thread_id, async_land=False,
        submit=SubmitResult(status="landed", landed_rev=landed_sha),
    )
    assert ok

    topic = tp.get_topic(db, "T-sync")
    assert topic["state"] != "integrated-unlanded"
    # merged_sha recorded via the synchronous landed path; reconcile/mark drives verified.
    assert topic["merged_sha"] == landed_sha


# -- PIN: reconcile must NOT demote an in-flight integrated-unlanded topic ----

def test_reconcile_does_not_demote_integrated_unlanded(db, tmp_path, monkeypatch):
    """PIN (async-land path dead — submitted ticket dropped, 2026-07-05):
    reconcile_topic_state must treat 'integrated-unlanded' as terminal FOR
    DERIVATION (poller-owned), exactly like 'verified'. Pre-fix it had no guard
    for the async-pending state, so an in-flight async-land topic (member tasks
    completion-terminal, merged_sha NULL) derived 'integrating' and got demoted —
    ejecting it from the land poller's sweep (filters 'integrated-unlanded') INTO
    the reintegrate sweep (filters 'integrating'), which re-drives a detached
    integrate against a torn-down worktree while the diff still lands (split-brain).
    Reachable in the up-to-72h land window via `juggle doctor` / manual
    `juggle graph reconcile` (both run reconcile_project_topics UNFILTERED)."""
    from dbops import db_topics_reconcile as tr

    repo = _merged_repo(tmp_path)
    wt = tmp_path / "wt"
    wt.mkdir()
    _integrating_topic(db, "T-rec", repo, worktree_path=wt)
    thread_id = tp.get_topic(db, "T-rec")["thread_id"]
    _run_async_integrate(db, monkeypatch, tmp_path, thread_id, ticket="D77")
    assert tp.get_topic(db, "T-rec")["state"] == "integrated-unlanded"

    state = tr.reconcile_topic_state(db, "T-rec")

    assert state == "integrated-unlanded", "reconcile must not demote a poller-owned topic"
    topic = tp.get_topic(db, "T-rec")
    assert topic["state"] == "integrated-unlanded"
    assert topic["submitted_rev"] == "D77"
    # reconcile_project_topics (the juggle doctor / manual-reconcile entrypoint) too.
    tr.reconcile_project_topics(db, "INBOX")
    assert tp.get_topic(db, "T-rec")["state"] == "integrated-unlanded"


# -- PIN: the landed oracle honors the configured trunk (git-ism sweep) -------

def test_resolve_landed_sha_uses_configured_trunk(tmp_path, monkeypatch):
    """PIN (async-land path dead — submitted ticket dropped, 2026-07-05): the
    two-tier landed oracle (reconcile self-heal / orphan reconcile) must resolve
    ancestry against the CONFIGURED trunk, not a hardcoded 'main'. Pre-fix
    resolve_landed_sha defaulted main='main', so a non-'main'-trunk repo (e.g.
    Sapling develop) false-negatived every landed branch and left topics wedged."""
    from dbops import landed

    repo = _merged_repo(tmp_path, branch="develop")
    _git(repo, "checkout", "-q", "-b", "cyc_feat")
    (Path(repo) / "g.txt").write_text("feat\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "feat")
    feat_sha = _head_sha(repo, "cyc_feat")
    _git(repo, "checkout", "-q", "develop")
    _git(repo, "merge", "-q", "--ff-only", "cyc_feat")

    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"repos": {repo: {"trunk": "develop"}}}))
    monkeypatch.setenv("_JUGGLE_CONFIG_PATH", str(cfg))

    assert landed.resolve_landed_sha(repo, "cyc_feat") == feat_sha
