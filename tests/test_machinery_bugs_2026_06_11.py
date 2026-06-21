"""Regression pins for autopilot/graph machinery bugs surfaced 2026-06-11.

Bug F — model field not cleared on agent release (release-agent model poison)
Bug G — graphify-out dirty tree blocks integrate ff-merge
Bug E — worktree trust pre-registration in ~/.claude.json
Bug I — mark_topic_completion idempotent on already-verified topic
Bug J — graph_tick legacy flat-task fallback for projects without topics
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as t  # noqa: E402
import juggle_graph_dispatch as gd  # noqa: E402


# ── shared fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "test.db"))
    d.init_db()
    with d._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO session (key, value) VALUES ('session_id', 'testsession')"
        )
        conn.commit()
    return d


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in [
        ["git", "init", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
        ["git", "-C", str(repo), "config", "user.name", "T"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "init"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "branch", "-M", "main"],
        check=True, capture_output=True,
    )
    return str(repo)


# ── Bug F: model field not cleared on agent release ────────────────────────────

def test_release_agent_clears_model(db, monkeypatch):
    """2026-06-11: release-agent carried a poisoned model string to the next
    dispatch because the model field was not cleared in the task-state wipe.
    Pin: after release, model must be NULL."""
    agent_id = db.create_agent(role="coder", pane_id="fake-pane", harness="claude")
    db.update_agent(agent_id, model="bad/model-typo")
    assert db.get_agent(agent_id)["model"] == "bad/model-typo"

    import juggle_cmd_agents_lifecycle as lc
    import juggle_cmd_agents_common as _com
    monkeypatch.setattr(_com, "get_db", lambda: db)

    from argparse import Namespace
    lc.cmd_release_agent(Namespace(agent_id=agent_id, force=True))

    agent = db.get_agent(agent_id)
    assert agent["status"] == "idle"
    assert agent["model"] is None, "model must be cleared on release to prevent pool poison"


# ── Bug G: graphify-out dirty tree blocks integrate ff-merge ──────────────────

def _setup_worktree_branch(repo: str, also_modify_graphify: bool = False) -> tuple[str, str]:
    """Create a branch with one extra commit; return (worktree_path, branch)."""
    wt = str(Path(repo).parent / "wt-test")
    branch = "cyc_gtest"
    subprocess.run(
        ["git", "-C", repo, "worktree", "add", "-b", branch, wt],
        check=True, capture_output=True,
    )
    (Path(wt) / "feature.py").write_text("y = 2\n")
    if also_modify_graphify:
        Path(wt, "graphify-out").mkdir(exist_ok=True)
        (Path(wt) / "graphify-out" / "graph.json").write_text('{"tasks":["agent-version"]}')
    subprocess.run(["git", "-C", wt, "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", wt, "commit", "-m", "feat"],
        check=True, capture_output=True,
    )
    return wt, branch


def test_integrate_succeeds_with_dirty_graphify_out(db, git_repo):
    """2026-06-11: graphify watch hook regenerates tracked graphify-out/*.json
    after every commit. The agent's worktree commit also updates graph.json (hook
    ran in the worktree). Main's graph.json is then left dirty by the hook
    running again. git merge --ff-only fails: 'local changes would be overwritten'.
    _run_integrate must discard those dirty graphify-out files before merging."""
    repo = git_repo

    # Baseline: track graphify-out/graph.json in main
    gout = Path(repo) / "graphify-out"
    gout.mkdir(exist_ok=True)
    (gout / "graph.json").write_text('{"tasks":[]}')
    subprocess.run(["git", "-C", repo, "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", repo, "commit", "-m", "track graphify-out"],
        check=True, capture_output=True,
    )

    # Branch also modifies graphify-out/graph.json (hook ran in worktree)
    wt, branch = _setup_worktree_branch(repo, also_modify_graphify=True)

    # Simulate hook re-running in main after some other work — leaves it dirty
    (gout / "graph.json").write_text('{"tasks":["main-dirty"]}')

    # Confirm the raw ff-merge fails (this is the bug we're pinning against)
    raw = subprocess.run(
        ["git", "-C", repo, "merge", "--ff-only", branch],
        capture_output=True, text=True,
    )
    assert raw.returncode != 0, (
        "Expected bare ff-merge to fail when graphify-out is dirty — "
        "if this passes the test fixture doesn't reproduce the bug"
    )
    # Restore dirty state (merge attempt may have exited cleanly or reset)
    (gout / "graph.json").write_text('{"tasks":["main-dirty"]}')

    thread_id = db.create_thread("gtest", session_id="")
    db.update_thread(
        thread_id,
        worktree_path=wt,
        worktree_branch=branch,
        main_repo_path=repo,
    )
    thread = db.get_thread(thread_id)

    from juggle_cmd_integrate import _run_integrate
    success, msg = _run_integrate(thread, db)
    assert success, f"integrate failed with dirty graphify-out: {msg}"


# ── Bug E: worktree trust pre-registration in ~/.claude.json ──────────────────

def test_create_worktree_registers_trust(git_repo, tmp_path, monkeypatch):
    """2026-06-11: new worktree dir not in ~/.claude.json projects map causes
    Claude Code to show the 'Do you trust this folder?' prompt, swallowing the
    task submission. _create_worktree must pre-register the path as trusted."""
    fake_claude_json = tmp_path / "claude.json"
    fake_claude_json.write_text(json.dumps({"projects": {}}))

    monkeypatch.setenv("JUGGLE_CLAUDE_JSON_PATH", str(fake_claude_json))

    import uuid
    from juggle_cmd_agents_worktree import _create_worktree
    label = "E" + uuid.uuid4().hex[:6].upper()
    ok, wt_path, branch, msg = _create_worktree(
        git_repo, label, worktree_root=str(tmp_path)
    )
    assert ok, f"_create_worktree failed: {msg}"

    data = json.loads(fake_claude_json.read_text())
    assert wt_path in data.get("projects", {}), (
        f"Worktree {wt_path} not registered in projects trust map; "
        f"Claude will show workspace trust prompt and swallow task."
    )


# ── Bug I: mark_topic_completion idempotent on verified ───────────────────────

def _bind_merged_topic(db, topic_id, tmp_path):
    """T-verified-merged-sha: topic→verified requires a recorded merged_sha that
    is an ancestor of main."""
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


def test_mark_topic_completion_idempotent_on_verified(db, tmp_path):
    """2026-06-11: when an out-of-band integrate already moved the topic to
    'verified', a second complete-agent call triggers mark_topic_completion on a
    terminal state → ValueError, which mark_graph_topic catches as a warning.
    The topic stays verified (OK), but the pattern was fragile.
    Pin: mark_topic_completion on an already-verified topic returns 'verified'
    without raising."""
    t.create_topic(db, topic_id="ta", project_id="INBOX", title="A", objective="")
    g.create_task(db, task_id="n1", project_id="INBOX", title="n1", prompt="do n1")
    with db._connect() as conn:
        conn.execute("UPDATE graph_tasks SET topic_id='ta' WHERE id='n1'")
        conn.commit()
    _bind_merged_topic(db, "ta", tmp_path)  # G1: merged → verify allowed

    # Walk topic to verified
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start"):
        t.topic_transition(db, "ta", ev)
    g.task_transition(db, "n1", "deps_ready")
    g.task_transition(db, "n1", "claim")
    g.task_transition(db, "n1", "dispatch")
    g.task_transition(db, "n1", "integrate_start")
    g.task_transition(db, "n1", "integrate_ok")
    result = t.topic_transition(db, "ta", "integrate_ok")
    assert result == "verified"

    # Second call — must be idempotent, not raise ValueError
    state = t.mark_topic_completion(db, "ta", integrate_ok=True, verify_ok=True)
    assert state == "verified", (
        "mark_topic_completion on already-verified topic must return 'verified' "
        "idempotently (duplicate complete-agent calls must not leave task stuck)"
    )


# ── Bug J: graph_tick legacy flat-task fallback ───────────────────────────────

class FakeDispatchJ:
    def __init__(self):
        self.calls: list[tuple] = []

    def __call__(self, db, thread_id, prompt, task_or_topic):
        self.calls.append((thread_id, task_or_topic.get("id")))


def _arm(db, project="INBOX"):
    db.set_setting(gd.ARMED_PROJECT_KEY, project)


def test_graph_tick_dispatches_ready_tasks_for_topicless_project(db):
    """2026-06-11: graph_tick only dispatches topics; a project whose graph has
    graph_tasks but 0 graph_topics (migration 37 backfilled 0) silently skips
    all tasks → graph build stalls. Spec R9/R6 intended a legacy flat fallback.
    Pin: ready tasks in a topicless project ARE dispatched by graph_tick."""
    _arm(db)
    # Create tasks but NO topics
    g.create_task(db, task_id="n1", project_id="INBOX", title="n1", prompt="do n1")
    g.create_task(db, task_id="n2", project_id="INBOX", title="n2", prompt="do n2",)
    g.replace_edges(db, "n2", ["n1"])  # n2 depends on n1
    g.recompute_ready(db, "INBOX")

    dispatcher = FakeDispatchJ()
    stats = gd.graph_tick(db, dispatch_fn=dispatcher)

    dispatched_task_ids = {nid for _, nid in dispatcher.calls}
    assert "n1" in dispatched_task_ids, (
        "Ready task n1 must be dispatched by graph_tick legacy fallback "
        "when project has no topics."
    )
    assert "n2" not in dispatched_task_ids, "n2 has unmet dep, must not be dispatched"
    assert stats["dispatched"], "stats must reflect dispatched task"
