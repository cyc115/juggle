"""Integration test: 4-TOPIC diamond graph driven end-to-end by real
cmd_complete_agent calls + directly-invoked graph_tick (autopilot R9 topic tick).

Rewritten to the TOPIC seam (2026-06-11 Task 7): graph_tick dispatches one
thread per TOPIC; each topic's tasks are marked via the task machine
(graph mark-task), then complete-agent finishes the topic ONCE — the A10 gate
(refuse while tasks unmarked), integrate-once-per-topic, topic verified /
failure propagation, and ready-set promotion of dependent topics.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest  # noqa: E402

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as tp  # noqa: E402
import juggle_graph_dispatch as gd  # noqa: E402
import juggle_cmd_graph as cg  # noqa: E402  (top-level: bind real get_db pre-patch)


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    d._set_session_key_external("session_id", "sessI")
    import juggle_cli_common as common
    import juggle_cmd_agents_common as agents_common

    monkeypatch.setattr(common, "get_db", lambda: d)
    monkeypatch.setattr(agents_common, "get_db", lambda: d)
    return d


def _topic(db, tid, project="INBOX"):
    """A single-task synthetic topic `tid` (task `<tid>1`)."""
    tp.create_topic(db, topic_id=tid, project_id=project, title=f"Topic {tid}")
    g.create_task(db, task_id=f"{tid}1", project_id=project, title=f"{tid}1",
                  prompt=f"build {tid}")
    g.set_task_topic(db, f"{tid}1", tid)  # dual-writes graph_tasks + nodes


def _task_edge(db, child_task, parent_task):
    with db._connect() as conn:
        # task readers read node_edges (P8 Task 4.1; legacy graph_edges dropped).
        conn.execute(
            "INSERT OR IGNORE INTO node_edges (node_id, depends_on_id) VALUES (?,?)",
            (child_task, parent_task))
        conn.commit()


def _load_diamond(db):
    """Topics A → (B, C) → D, each single-task; derived deps via task edges."""
    for t in ("A", "B", "C", "D"):
        _topic(db, t)
    _task_edge(db, "B1", "A1")
    _task_edge(db, "C1", "A1")
    _task_edge(db, "D1", "B1")
    _task_edge(db, "D1", "C1")
    tp.recompute_topic_ready(db, "INBOX")  # A → ready
    db.set_setting(gd.ARMED_PROJECT_KEY, "INBOX")


class FakeDispatch:
    def __init__(self):
        self.prompts: dict[str, str] = {}  # topic_id → hydrated prompt

    def __call__(self, db, thread_id, prompt, topic):
        self.prompts[topic["id"]] = prompt


def _states(db):
    return {t["id"]: t["state"] for t in tp.list_topics(db, "INBOX")}


def _merged_repo() -> str:
    """A real repo whose branch 'main' is trivially an ancestor of main, used to
    satisfy the G1 verified⟺merged guard for topics that should verify."""
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


def _complete_topic(db, topic_id, handoff, *, fail_task=None, merged=True):
    """Mark every task of the topic terminal (verified, or fail_task →
    failed-verify) then finish the topic via REAL cmd_complete_agent (the A10
    gate + integrate-once + topic marking).

    G1 (2026-06-13): bind the topic's thread to a MERGED repo (unless the test
    already set worktree fields, e.g. a deliberate failed integrate) so the
    verified⟺merged guard permits the topic to verify."""
    from juggle_cmd_agents import cmd_complete_agent

    for n in tp.list_topic_tasks(db, topic_id):
        g.mark_completion(db, n["id"], integrate_ok=True,
                          verify_ok=(n["id"] != fail_task), handoff="task handoff")
    topic = tp.get_topic(db, topic_id)
    assert topic["thread_id"], f"topic {topic_id} has no bound thread"
    if merged:
        thread = db.get_thread(topic["thread_id"]) or {}
        repo = (thread.get("main_repo_path") or "").strip()
        if not repo:
            repo = _merged_repo()
            db.update_thread(topic["thread_id"], worktree_branch="cyc_x",
                             main_repo_path=repo)
        # T-verified-merged-sha single gate: record main's HEAD (trivially an
        # ancestor of main) as the topic's merged_sha so it may verify. Fail-soft
        # on a non-repo path (deliberately-failed-integrate tests use a dummy).
        sha = subprocess.run(
            ["git", "-C", repo, "rev-parse", "main"],
            capture_output=True, text=True,
        )
        if sha.returncode == 0 and sha.stdout.strip():
            tp.set_topic_merged_sha(db, topic_id, sha.stdout.strip())
    cmd_complete_agent(argparse.Namespace(
        thread_id=topic["thread_id"],
        result_summary=f"{topic_id} complete",
        retain_text=None, open_questions=None, handoff=handoff,
    ))


def test_diamond_happy_path_full_flow(db):
    _load_diamond(db)
    fake = FakeDispatch()

    # Tick 1: only the root topic is ready → dispatched
    stats = gd.graph_tick(db, dispatch_fn=fake)
    assert stats["dispatched"] == ["A"]
    assert _states(db) == {"A": "running", "B": "open", "C": "open", "D": "open"}

    # Completing A (with handoff) promotes B and C; complete-agent NEVER dispatches
    _complete_topic(db, "A", handoff="base API: use foo() from base.py")
    assert _states(db) == {"A": "verified", "B": "ready", "C": "ready", "D": "open"}
    assert set(fake.prompts) == {"A"}  # no dispatch outside the tick

    # Tick 2: fan-out — B and C dispatched, hydrated from A's TOPIC handoff
    stats = gd.graph_tick(db, dispatch_fn=fake)
    assert sorted(stats["dispatched"]) == ["B", "C"]
    assert _states(db)["B"] == "running" and _states(db)["C"] == "running"
    assert "base API: use foo() from base.py" in fake.prompts["B"]
    assert "base API: use foo() from base.py" in fake.prompts["C"]

    # Fan-in: D waits for BOTH
    _complete_topic(db, "B", handoff="left exposes bar()")
    assert _states(db)["D"] == "open"
    assert gd.graph_tick(db, dispatch_fn=fake)["dispatched"] == []
    _complete_topic(db, "C", handoff="right exposes baz()")
    assert _states(db)["D"] == "ready"

    # Tick 3: D dispatched with BOTH upstream handoffs; leaf completes sans handoff
    stats = gd.graph_tick(db, dispatch_fn=fake)
    assert stats["dispatched"] == ["D"]
    assert "left exposes bar()" in fake.prompts["D"]
    assert "right exposes baz()" in fake.prompts["D"]
    _complete_topic(db, "D", handoff=None)  # no dependents — contract not required

    assert _states(db) == {t: "verified" for t in "ABCD"}
    assert all(tp.get_topic(db, t)["verified_at"] for t in "ABCD")
    # every topic ran on its own thread, all bound + project-tagged
    tids = {tp.get_topic(db, t)["thread_id"] for t in "ABCD"}
    assert len(tids) == 4
    assert all(db.get_thread(t)["project_id"] == "INBOX" for t in tids)


def test_diamond_failed_integration_branch_blocks_dependents_loudly(db, monkeypatch):
    """B's integrate fails mid-diamond → B = failed-integration (DA B3: never
    verified), downstream D goes 'blocked-failed' and is NEVER dispatched,
    sibling C is unaffected (still verifies), and a HIGH action item flags the
    failure naming the blocked topic (no silent stall).
    (rewritten to the topic seam, R9 2026-06-11 Task 7)"""
    import juggle_cmd_agents_common as _com

    _load_diamond(db)
    fake = FakeDispatch()
    gd.graph_tick(db, dispatch_fn=fake)            # A
    _complete_topic(db, "A", handoff="base done")
    gd.graph_tick(db, dispatch_fn=fake)            # B, C

    # B's worktree integration fails; C (no worktree fields) finalizes fine —
    # _run_integrate only runs for threads with worktree fields, i.e. only B.
    b_tid = tp.get_topic(db, "B")["thread_id"]
    db.update_thread(b_tid, worktree_path="/tmp/wt-b", worktree_branch="cyc_b",
                     main_repo_path="/tmp/repo")
    monkeypatch.setattr(
        _com.juggle_cmd_integrate, "_run_integrate",
        lambda thread, db_: (False, "rebase conflict"),
    )
    _complete_topic(db, "B", handoff="b attempted")
    _complete_topic(db, "C", handoff="c done")

    states = _states(db)
    assert states["B"] == "failed-integration"
    assert states["C"] == "verified", "sibling must be unaffected by b's failure"
    assert states["D"] == "blocked-failed"  # propagated, not stalled

    # further ticks never dispatch D while it is blocked-failed
    stats = gd.graph_tick(db, dispatch_fn=fake)
    assert stats["dispatched"] == []
    assert "D" not in fake.prompts
    assert _states(db)["D"] == "blocked-failed"  # tick must not resurrect it
    items = db.get_open_action_items()
    assert any("failed-integration" in i["message"] and i["priority"] == "high"
               for i in items)
    assert any("blocked" in i["message"].lower() and "D" in i["message"]
               for i in items), "action item must name the blocked dependent"


def test_add_task_mid_execution_does_not_touch_running_topic(db):
    """FEATURE PIN (graph add-task, 2026-06-10): injecting work while a topic is
    RUNNING must leave the running topic untouched and execute the new topic in
    dependency order via the tick (no manual dispatch).
    (rewritten to the topic seam, R9 2026-06-11 Task 7)"""
    from types import SimpleNamespace

    _load_diamond(db)
    fake = FakeDispatch()

    # Tick 1: root topic A dispatched → running
    gd.graph_tick(db, dispatch_fn=fake)
    assert _states(db)["A"] == "running"
    a_before = tp.get_topic(db, "A")

    # Inject a new single-task topic X whose task depends on still-running A,
    # via the live add-task CLI (--topic X required: project has real topics).
    tp.create_topic(db, topic_id="X", project_id="INBOX", title="Topic X")
    cg.cmd_graph_add_task(SimpleNamespace(
        project="INBOX", id="x1", title="X1", prompt="run after A",
        deps="A1", required_by=None, verify_cmd=None, topic="X",
        json_out=False, db_path=str(db.db_path),
    ))
    # running topic A is untouched (state, thread)
    a_after = tp.get_topic(db, "A")
    assert a_after["state"] == "running"
    assert a_after["thread_id"] == a_before["thread_id"]
    # X waits on A (A not verified yet) → pending, NOT dispatched this tick
    assert _states(db)["X"] == "open"
    assert "X" not in gd.graph_tick(db, dispatch_fn=fake)["dispatched"]
    assert "X" not in fake.prompts

    # complete A → X (and B, C) become ready; tick dispatches them
    _complete_topic(db, "A", handoff="a done")
    assert _states(db)["X"] == "ready"
    dispatched = gd.graph_tick(db, dispatch_fn=fake)["dispatched"]
    assert "X" in dispatched
    assert _states(db)["X"] == "running"
