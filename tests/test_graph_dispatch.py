"""Tests for juggle_graph_dispatch — watchdog-owned dispatcher (autopilot Phase 2).

Covers: atomic claim (DA B4 pin: two concurrent claimers, exactly one wins),
cap-aware defer + next-tick retry, crash-mid-dispatch stale-claim sweep
recovery, hydration content (DA M4: dep handoffs + objective, never
thread.summary), and graph_tick orchestration (armed-key gating, dispatch
errors → task released + action item, disarm mid-batch).
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
import juggle_graph_dispatch as gd  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "graph.db"))
    d.init_db()
    return d


def _mk(db, task_id, deps=(), state=None, **kw):
    g.create_task(
        db,
        task_id=task_id,
        project_id="INBOX",
        title=kw.get("title", f"Task {task_id}"),
        prompt=kw.get("prompt", f"do {task_id}"),
        verify_cmd=kw.get("verify_cmd"),
    )
    if deps:
        g.replace_edges(db, task_id, list(deps))


def _arm(db, project="INBOX"):
    db.set_setting(gd.ARMED_PROJECT_KEY, project)


class FakeDispatch:
    """Records dispatches; optionally raises. No tmux, no LLM."""

    def __init__(self, exc=None):
        self.calls: list[tuple[str, str, str]] = []  # (thread_id, prompt, task_id)
        self.exc = exc

    def __call__(self, db, thread_id, prompt, task):
        if self.exc:
            raise self.exc
        self.calls.append((thread_id, prompt, task["id"]))


# ── settings key / arming ─────────────────────────────────────────────────────


def test_set_setting_upsert_and_delete(db):
    db.set_setting("k", "v1")
    assert db.get_setting("k") == "v1"
    db.set_setting("k", "v2")
    assert db.get_setting("k") == "v2"
    db.set_setting("k", None)
    assert db.get_setting("k") is None


def test_get_armed_project_blank_means_disarmed(db):
    assert gd.get_armed_project(db) is None
    db.set_setting(gd.ARMED_PROJECT_KEY, "  ")
    assert gd.get_armed_project(db) is None
    _arm(db)
    assert gd.get_armed_project(db) == "INBOX"


# ── atomic claim (DA B4) ───────────────────────────────────────────────────────


def test_claim_only_from_ready(db):
    _mk(db, "a")
    assert gd.claim_task(db, "a") is False  # pending — not claimable
    g.recompute_ready(db, "INBOX")
    assert gd.claim_task(db, "a") is True
    assert g.get_task(db, "a")["state"] == "dispatching"
    assert gd.claim_task(db, "a") is False  # already claimed


def test_concurrent_claim_exactly_one_wins(db, tmp_path):
    """REQUIRED PIN (DA B4, 2026-06-10): two dispatchers racing the same ready
    task spawned two agents on one thread/worktree — the atomic
    UPDATE..WHERE state='ready' claim must let EXACTLY one claimer win."""
    _mk(db, "a")
    g.recompute_ready(db, "INBOX")

    db2 = JuggleDB(db_path=str(tmp_path / "graph.db"))  # separate connection
    barrier = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def _claim(handle):
        barrier.wait()
        won = gd.claim_task(handle, "a")
        with lock:
            results.append(won)

    threads = [
        threading.Thread(target=_claim, args=(h,)) for h in (db, db2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [False, True]  # exactly one winner
    assert g.get_task(db, "a")["state"] == "dispatching"


# ── stale-claim sweep ──────────────────────────────────────────────────────────


def _age_claim(db, task_id, secs):
    old = (datetime.now(timezone.utc) - timedelta(seconds=secs)).isoformat()
    with db._connect() as conn:
        # sweep_stale_claims reads nodes.updated_at (P8 Task 4.1) — age both the
        # authoritative store and the legacy mirror.
        conn.execute("UPDATE nodes SET updated_at=? WHERE id=?", (old, task_id))
        conn.execute(
            "UPDATE graph_tasks SET updated_at=? WHERE id=?", (old, task_id)
        )
        conn.commit()


def test_sweep_resets_stale_threadless_claims_only(db):
    _mk(db, "stale")
    _mk(db, "fresh")
    _mk(db, "bound")
    g.recompute_ready(db, "INBOX")
    for n in ("stale", "fresh", "bound"):
        assert gd.claim_task(db, n)
    g.set_task_thread(db, "bound", "some-thread")
    _age_claim(db, "stale", gd.STALE_CLAIM_SECS + 60)
    _age_claim(db, "bound", gd.STALE_CLAIM_SECS + 60)

    swept = gd.sweep_stale_claims(db, "INBOX")

    assert swept == ["stale"]
    assert g.get_task(db, "stale")["state"] == "ready"
    assert g.get_task(db, "fresh")["state"] == "dispatching"  # too young
    assert g.get_task(db, "bound")["state"] == "dispatching"  # has a thread


def test_crash_mid_dispatch_recovers_via_sweep_then_redispatches(db):
    """Crash between claim and send-task (no thread bound): after 10 min the
    sweep returns the topic to ready and the SAME tick re-dispatches it.
    (adapted to topics, R9 2026-06-11)"""
    _mk_topic(db, "a")
    assert gd.claim_topic(db, "a")  # simulated dispatcher died right here
    _age_topic_claim(db, "a", gd.STALE_CLAIM_SECS + 60)
    _arm(db)

    fake = FakeDispatch()
    stats = gd.graph_tick(db, dispatch_fn=fake)

    assert stats["swept"] == ["a"]
    assert stats["dispatched"] == ["a"]
    assert tp.get_topic(db, "a")["state"] == "running"
    assert tp.get_topic(db, "a")["thread_id"]


# ── hydration (DA M4) ──────────────────────────────────────────────────────────


def test_build_hydration_contains_objective_handoffs_prompt_contract():
    task = {
        "id": "api",
        "title": "Build API",
        "prompt": "Implement the API on top of the schema.",
        "verify_cmd": "uv run pytest tests/test_api.py -q",
    }
    deps = [
        {"id": "schema", "title": "Add schema", "handoff": "migration 35 adds graph tables; use db_graph.get_task"},
        {"id": "auth", "title": "Auth", "handoff": None},
    ]
    out = gd.build_hydration("Ship the autopilot.", task, deps)
    assert "Ship the autopilot." in out
    assert "migration 35 adds graph tables" in out
    assert "### schema — Add schema" in out  # dep section: id then title
    assert "(no handoff recorded)" in out  # dep without handoff degrades loudly
    assert "Implement the API on top of the schema." in out
    assert "uv run pytest tests/test_api.py -q" in out
    assert "--handoff" in out  # completion contract instruction


def test_hydration_uses_topic_handoff_not_junk(db):
    """DA M4: dep TOPIC prompts hydrate from dep-TOPIC handoffs only."""
    _mk_topic(db, "a")
    _mk_topic(db, "b", ready=False)
    _dep_topic(db, "b", "a")  # b depends on a
    tid = db.create_thread("dep thread", session_id="s")
    tp.set_topic_thread(db, "a", tid)
    _verify_topic(db, "a", handoff="real handoff content")
    tp.recompute_topic_ready(db, "INBOX")  # b → ready
    _arm(db)

    fake = FakeDispatch()
    gd.graph_tick(db, dispatch_fn=fake)

    (_, prompt, topic_id), = fake.calls
    assert topic_id == "b"
    assert "real handoff content" in prompt


# ── graph_tick orchestration ───────────────────────────────────────────────────


def test_tick_dispatches_without_arming(db):
    """REGRESSION PIN (P7): tick dispatches all ready topics without any
    armed-project setting — per-project arming is removed."""
    _mk_topic(db, "a")  # ready topic; no armed key needed after P7
    fake = FakeDispatch()
    stats = gd.graph_tick(db, dispatch_fn=fake)
    assert stats["dispatched"] == ["a"]
    assert tp.get_topic(db, "a")["state"] == "running"


def test_tick_dispatches_ready_tasks_and_binds_threads(db):
    """(adapted to topics, R9 2026-06-11)"""
    _mk_topic(db, "a")
    _mk_topic(db, "b")
    _mk_topic(db, "c", ready=False)
    _dep_topic(db, "c", "a")  # c gated on a
    _arm(db)
    fake = FakeDispatch()

    stats = gd.graph_tick(db, dispatch_fn=fake)

    assert sorted(stats["dispatched"]) == ["a", "b"]
    for tid_ in ("a", "b"):
        topic = tp.get_topic(db, tid_)
        assert topic["state"] == "running"
        thread = db.get_thread(topic["thread_id"])
        assert thread is not None
        assert thread["project_id"] == "INBOX"
    assert tp.get_topic(db, "c")["state"] == "open"  # dep not verified


def test_tick_self_heals_missed_ready_promotion(db):
    """A completion that crashes between marking 'verified' and topic-ready
    recompute would strand eligible dependents in 'open' forever — the tick
    promotes them itself (idempotent recompute_topic_ready) before scanning the
    ready set. (adapted to topics, R9 2026-06-11)"""
    _mk_topic(db, "a")
    _mk_topic(db, "b", ready=False)
    _dep_topic(db, "b", "a")
    _bind_merged_topic(db, "a")  # G1: merged → may verify
    for ev in ("claim", "dispatch", "integrate_start", "integrate_ok"):
        tp.topic_transition(db, "a", ev)  # verified, but NO recompute ran
    assert tp.get_topic(db, "b")["state"] == "open"
    _arm(db)

    fake = FakeDispatch()
    stats = gd.graph_tick(db, dispatch_fn=fake)

    assert stats["dispatched"] == ["b"]
    assert tp.get_topic(db, "b")["state"] == "running"


def test_tick_cap_hit_defers_and_retries_next_tick(db, monkeypatch):
    """MAX_THREADS during lazy create_thread: skip + retry next tick, topic
    back to 'ready', daemon never crashes (cap-aware lazy threads).
    (adapted to topics, R9 2026-06-11)"""
    import dbops.threads as threads_mod

    _mk_topic(db, "a")
    _arm(db)
    monkeypatch.setattr(threads_mod, "MAX_THREADS", 0)

    fake = FakeDispatch()
    stats = gd.graph_tick(db, dispatch_fn=fake)
    assert stats["deferred"] == ["a"] and fake.calls == []
    assert tp.get_topic(db, "a")["state"] == "ready"  # claim released

    monkeypatch.setattr(threads_mod, "MAX_THREADS", 10)
    stats = gd.graph_tick(db, dispatch_fn=fake)  # next tick: cap lifted
    assert stats["dispatched"] == ["a"]
    assert tp.get_topic(db, "a")["state"] == "running"


def test_tick_dispatch_failure_releases_task_and_files_action_item(db):
    """(adapted to topics, R9 2026-06-11)"""
    _mk_topic(db, "a")
    _arm(db)

    stats = gd.graph_tick(db, dispatch_fn=FakeDispatch(exc=RuntimeError("tmux gone")))

    assert stats["errors"] == ["a"]
    topic = tp.get_topic(db, "a")
    assert topic["state"] == "ready"  # released for retry/operator
    assert topic["thread_id"] is None
    items = db.get_open_action_items()
    assert any("dispatch failed" in i["message"] and "a" in i["message"] for i in items)
    # orphan thread was archived, not leaked into the active cap
    assert all(t["state"] == "archived" for t in db.get_all_threads() if t)


def test_tick_capacity_error_defers_quietly(db):
    """(adapted to topics, R9 2026-06-11)"""
    _mk_topic(db, "a")
    _arm(db)

    stats = gd.graph_tick(db, dispatch_fn=FakeDispatch(exc=gd.CapacityError("pool full")))

    assert stats["deferred"] == ["a"] and stats["errors"] == []
    assert tp.get_topic(db, "a")["state"] == "ready"
    assert db.get_open_action_items() == []  # no spam for capacity defers


def test_tick_processes_all_topics_despite_settings_key_change(db):
    """REGRESSION PIN (P7): clearing ARMED_PROJECT_KEY mid-dispatch must NOT
    stop the tick — the armed key is dead data after P7. Both topics dispatch."""
    _mk_topic(db, "a")
    _mk_topic(db, "b")

    def key_clearing_dispatch(db_, thread_id, prompt, topic):
        db_.set_setting(gd.ARMED_PROJECT_KEY, None)  # key change is ignored

    stats = gd.graph_tick(db, dispatch_fn=key_clearing_dispatch)

    assert set(stats["dispatched"]) == {"a", "b"}
    states = {tid_: tp.get_topic(db, tid_)["state"] for tid_ in ("a", "b")}
    assert all(s == "running" for s in states.values())


# ── DA round-2 (2026-06-10): dispatch window, retry cap, error hygiene ─────────


def test_task_thread_bound_before_dispatch_call(db):
    """REGRESSION PIN (DA round-2 MAJOR-4, 2026-06-10): the tick dispatched
    BEFORE binding thread_id; a crash in that window left a 'dispatching' task
    with thread_id NULL — the stale sweep reclaimed it and the task was
    double-dispatched. thread_id must be bound before send-task fires.
    (adapted to topics, R9 2026-06-11)"""
    _mk_topic(db, "a")
    _arm(db)
    seen = {}

    def spy(db_, thread_id, prompt, topic):
        seen["bound"] = tp.get_topic(db_, topic["id"])["thread_id"]
        seen["thread"] = thread_id

    gd.graph_tick(db, dispatch_fn=spy)
    assert seen["thread"] is not None
    assert seen["bound"] == seen["thread"]


def test_crash_in_dispatch_window_not_reclaimed_by_sweep(db):
    """REGRESSION PIN (DA round-2 MAJOR-4, 2026-06-10): simulate a hard crash
    after send-task fired but before the topic went 'running'. The topic stays
    thread-bound 'dispatching', so the stale sweep must NOT reclaim it — the
    old order yielded a second dispatch of already-running work.
    (adapted to topics, R9 2026-06-11)"""

    class HardCrash(BaseException):
        """Process death — bypasses the tick's belt-and-braces Exception nets."""

    def crashing(db_, thread_id, prompt, topic):
        raise HardCrash()

    _mk_topic(db, "a")
    _arm(db)
    with pytest.raises(HardCrash):
        gd.graph_tick(db, dispatch_fn=crashing)

    topic = tp.get_topic(db, "a")
    assert topic["state"] == "dispatching"
    assert topic["thread_id"]  # bound → sweep-immune
    _age_topic_claim(db, "a", gd.STALE_CLAIM_SECS + 60)
    assert gd.sweep_stale_topic_claims(db, "INBOX") == []  # no reclaim


def test_capacity_defer_clears_thread_binding(db):
    """Guard for the MAJOR-4 reorder: defer/failure paths must clear the
    binding they now set before dispatch, or the released topic would carry a
    stale archived thread. (adapted to topics, R9 2026-06-11)"""
    _mk_topic(db, "a")
    _arm(db)
    gd.graph_tick(db, dispatch_fn=FakeDispatch(exc=gd.CapacityError("pool full")))
    topic = tp.get_topic(db, "a")
    assert topic["state"] == "ready"
    assert topic["thread_id"] is None


def test_dispatch_retry_cap_marks_failed_exec_and_stops_flood(db):
    """REGRESSION PIN (DA round-2 minor 1, 2026-06-10): a permanently broken
    dispatch path reset the task to 'ready' every tick — one HIGH action item
    per tick forever (action-item flood) and an infinite retry loop. After
    MAX_DISPATCH_FAILS consecutive failures the topic must go failed-exec and
    propagate to dependents instead. (adapted to topics, R9 2026-06-11)"""
    gd._dispatch_fails.clear()  # module-level counter — isolate from other tests
    _mk_topic(db, "a")
    _mk_topic(db, "b", ready=False)
    _dep_topic(db, "b", "a")
    _arm(db)
    failing = FakeDispatch(exc=RuntimeError("broken adapter"))

    for _ in range(gd.MAX_DISPATCH_FAILS):
        gd.graph_tick(db, dispatch_fn=failing)

    assert tp.get_topic(db, "a")["state"] == "failed-exec"
    assert tp.get_topic(db, "b")["state"] == "blocked-failed"
    items = db.get_open_action_items()
    assert any("gave up" in i["message"] and "a" in i["message"] for i in items)

    # the flood stops: further ticks neither retry nor file new items
    n_items = len(db.get_open_action_items())
    stats = gd.graph_tick(db, dispatch_fn=failing)
    assert stats["dispatched"] == [] and stats["errors"] == []
    assert len(db.get_open_action_items()) == n_items


def test_dispatch_success_resets_failure_count(db):
    """One-off dispatch hiccups must not accumulate toward the give-up cap.
    (adapted to topics, R9 2026-06-11)"""
    gd._dispatch_fails.clear()  # module-level counter — isolate from other tests
    _mk_topic(db, "a")
    _arm(db)
    gd.graph_tick(db, dispatch_fn=FakeDispatch(exc=RuntimeError("hiccup")))
    assert tp.get_topic(db, "a")["state"] == "ready"
    gd.graph_tick(db, dispatch_fn=FakeDispatch())  # succeeds
    assert tp.get_topic(db, "a")["state"] == "running"
    assert gd._dispatch_fails == {}


def test_unrelated_valueerror_in_create_thread_is_error_not_defer(db, monkeypatch):
    """REGRESSION PIN (DA round-2 minor 6, 2026-06-10): EVERY ValueError from
    create_thread was treated as the MAX_THREADS cap and silently deferred —
    an unrelated bug could starve the graph forever with zero signal.
    (adapted to topics, R9 2026-06-11)"""
    _mk_topic(db, "a")
    _arm(db)

    def buggy_create_thread(*a, **kw):
        raise ValueError("totally unrelated bug")

    monkeypatch.setattr(db, "create_thread", buggy_create_thread)
    stats = gd.graph_tick(db, dispatch_fn=FakeDispatch())

    assert stats["errors"] == ["a"]
    assert stats["deferred"] == []
    assert tp.get_topic(db, "a")["state"] == "ready"  # released for the operator
    items = db.get_open_action_items()
    assert any("thread creation failed" in i["message"] for i in items)


def test_cross_connection_thread_visibility(tmp_path):
    """REGRESSION PIN (2026-06-10): create_thread on db1 must be visible via
    get_thread on a separate JuggleDB instance (db2) pointing at the same file.
    WAL + synchronous=FULL on every connect (f7187b4) should ensure this.
    If this test is RED the WAL hardening is broken; if GREEN the visibility
    issue is already solved and the remaining gap is db_path mismatch."""
    db1 = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db1.init_db()

    thread_id = db1.create_thread("task thread", session_id="s")

    db2 = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db2.init_db()

    t = db2.get_thread(thread_id)
    assert t is not None, (
        f"thread {thread_id} not visible on separate JuggleDB after create_thread commit"
    )
    assert t["id"] == thread_id


def test_dispatch_via_pool_passes_same_db_to_dispatch_node(tmp_path, monkeypatch):
    """REGRESSION PIN (2026-06-10, rewritten 2026-06-20 for P3 seam):
    _dispatch_via_pool must pass the same db object to dispatch_node so the
    tick and the dispatch layer operate on the same database instance.
    Previously a db_path string was threaded through cmd_get_agent; now the
    db object is passed directly — no path-divergence risk."""
    import juggle_dispatch_core as _core

    db_local = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db_local.init_db()
    thread_id = db_local.create_thread("task-thread", session_id="s")

    captured = {}

    def spy_dispatch_node(db_, thread_id_, prompt_, task_, **kw):
        captured["db"] = db_

    monkeypatch.setattr(_core, "dispatch_node", spy_dispatch_node)

    gd._dispatch_via_pool(db_local, thread_id, "prompt", {"id": "n1"})

    assert captured.get("db") is db_local, (
        "dispatch_node must receive the same db object the tick uses"
    )


def test_dispatch_via_pool_propagates_exception_from_dispatch_node(db, monkeypatch):
    """REGRESSION PIN (DA round-2 minor 2, 2026-06-10, rewritten 2026-06-20 for P3 seam):
    _dispatch_via_pool propagates exceptions from dispatch_node so the tick's
    error-handling path (archive thread, bump fail counter) runs correctly.
    Agent cleanup on exception is now handled inside dispatch_node itself
    (see test_dispatch_node_releases_agent_on_send_failure)."""
    import juggle_dispatch_core as _core

    tid = db.create_thread("t", session_id="s")

    def boom(db_, tid_, prompt_, task_, **kw):
        raise RuntimeError("tmux exploded mid-send")

    monkeypatch.setattr(_core, "dispatch_node", boom)
    with pytest.raises(RuntimeError, match="tmux exploded mid-send"):
        gd._dispatch_via_pool(db, tid, "prompt", {"id": "a"})


# ── multi-project TOPIC tick (R9, 2026-06-11) ─────────────────────────────────

from dbops import db_topics as tp  # noqa: E402


def _mk_topic(db, tid, project="INBOX", n_tasks=1, ready=True):
    tp.create_topic(db, topic_id=tid, project_id=project, title=f"Topic {tid}")
    for i in range(n_tasks):
        nid = f"{tid}-k{i}"
        g.create_task(db, task_id=nid, project_id=project, title=nid, prompt="p")
        # set_task_topic dual-writes graph_tasks.topic_id + nodes.parent_id, so the
        # nodes-sourced topic ready-set (P8 Task 4.2) sees the child as a member.
        g.set_task_topic(db, nid, tid)
    if ready:
        tp.recompute_topic_ready(db, project)


def _arm_many(db, *projects):
    db.set_setting(gd.ARMED_PROJECT_KEY, ",".join(projects))


def _age_topic_claim(db, tid, secs):
    old = (datetime.now(timezone.utc) - timedelta(seconds=secs)).isoformat()
    with db._connect() as conn:
        # The stale-claim sweep reads nodes.updated_at (P8 Task 4.2); age both.
        conn.execute("UPDATE graph_topics SET updated_at=? WHERE id=?", (old, tid))
        conn.execute(
            "UPDATE nodes SET updated_at=? WHERE id=? AND kind='topic'", (old, tid)
        )
        conn.commit()


def _merged_repo() -> str:
    """A real repo whose 'main' branch satisfies the G1 verified⟺merged guard."""
    import subprocess
    import tempfile
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


def _bind_merged_topic(db, tid):
    """T-verified-merged-sha: bind tid to a thread on a repo and record main's
    HEAD as merged_sha so it may verify under the single gate."""
    import subprocess
    existing = tp.get_topic(db, tid)
    thread_id = existing["thread_id"] if existing else None
    if not thread_id:
        thread_id = db.create_thread("merge", session_id="s")
        tp.set_topic_thread(db, tid, thread_id)
    repo = _merged_repo()
    db.update_thread(thread_id, worktree_branch="cyc_x", main_repo_path=repo)
    sha = subprocess.run(["git", "-C", repo, "rev-parse", "main"],
                         capture_output=True, text=True)
    if sha.returncode == 0 and sha.stdout.strip():
        tp.set_topic_merged_sha(db, tid, sha.stdout.strip())


def _verify_topic(db, tid, handoff=None):
    """Walk a ready topic to 'verified' (claim→dispatch→integrate). Binds a
    merged repo (G1) so the verify transition is permitted."""
    _bind_merged_topic(db, tid)
    for ev in ("claim", "dispatch", "integrate_start", "integrate_ok"):
        tp.topic_transition(db, tid, ev)
    if handoff is not None:
        tp.set_topic_handoff(db, tid, handoff)


def _dep_topic(db, child, parent, project="INBOX"):
    """Make topic `child` derive-depend on `parent`: child's task → parent's.

    Writes node_edges (the flipped derived-dep source, P8 Task 4.2) alongside the
    legacy graph_edges so both stores agree."""
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO graph_edges (task_id, depends_on_id) VALUES (?,?)",
            (f"{child}-k0", f"{parent}-k0"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO node_edges (node_id, depends_on_id) VALUES (?,?)",
            (f"{child}-k0", f"{parent}-k0"),
        )
        conn.commit()
    tp.recompute_topic_ready(db, project)


def test_tick_dispatches_topics_across_all_armed_projects(db):
    """REGRESSION PIN (2026-06-10): the tick served get_armed_project() only —
    a second armed project never dispatched. Every armed graph must tick,
    and the dispatch unit is the TOPIC (R9): one dispatch per topic, not per
    task."""
    _mk_topic(db, "A1", "P1", n_tasks=3)
    _mk_topic(db, "B1", "P2")
    _arm_many(db, "P1", "P2")
    fd = FakeDispatch()
    stats = gd.graph_tick(db, dispatch_fn=fd)
    assert sorted(stats["dispatched"]) == ["A1", "B1"]
    assert tp.get_topic(db, "A1")["state"] == "running"
    assert len(fd.calls) == 2, "one dispatch per TOPIC, not per task"


def test_each_thread_bound_to_its_topics_project(db):
    """REGRESSION PIN (2026-06-10 spec DA): cross-project thread mis-binding
    would hydrate the wrong objective. Thread.project_id must match its
    topic's project."""
    _mk_topic(db, "A1", "P1")
    _mk_topic(db, "B1", "P2")
    _arm_many(db, "P1", "P2")
    gd.graph_tick(db, dispatch_fn=FakeDispatch())
    for tid_, pid in (("A1", "P1"), ("B1", "P2")):
        th = tp.get_topic(db, tid_)["thread_id"]
        assert th and db.get_thread(th)["project_id"] == pid


def test_all_projects_dispatched_regardless_of_settings_key(db):
    """REGRESSION PIN (P7): ARMED_PROJECT_KEY changes mid-batch are ignored —
    all projects dispatch regardless of what the settings key contains."""
    for t_ in ("A1", "A2"):
        _mk_topic(db, t_, "P1")
    for t_ in ("B1", "B2"):
        _mk_topic(db, t_, "P2")

    class SettingsKeyMutator(FakeDispatch):
        def __call__(self, db_, thread_id, prompt, topic):
            super().__call__(db_, thread_id, prompt, topic)
            if topic["id"].startswith("A"):
                db_.set_setting(gd.ARMED_PROJECT_KEY, "P2")  # ignored by tick

    stats = gd.graph_tick(db, dispatch_fn=SettingsKeyMutator())
    dispatched = set(stats["dispatched"])
    assert {"A1", "A2", "B1", "B2"} <= dispatched


def test_poisoned_project_scan_does_not_block_others(db, monkeypatch):
    """REGRESSION PIN (R4): a ready-scan exception used to abort the whole
    tick; blast radius must be one project."""
    _mk_topic(db, "A1", "P1")
    _mk_topic(db, "B1", "P2")
    _arm_many(db, "P1", "P2")
    real = tp.recompute_topic_ready

    def boom(db_, pid):
        if pid == "P1":
            raise RuntimeError("poisoned graph")
        return real(db_, pid)

    monkeypatch.setattr(gd.db_topics, "recompute_topic_ready", boom)
    stats = gd.graph_tick(db, dispatch_fn=FakeDispatch())
    assert stats["dispatched"] == ["B1"]


def test_global_cap_defers_fairly_across_projects(db):
    """Capacity is GLOBAL (MAX_THREADS bounds TOPICS — R9 budget model): a cap
    hit defers the pass with claims released; the fair prefix contains BOTH
    projects."""
    for i in range(3):
        _mk_topic(db, f"A{i}", "P1")
    _mk_topic(db, "B0", "P2")
    _arm_many(db, "P1", "P2")

    class CapAfter(FakeDispatch):
        def __init__(self, n):
            super().__init__()
            self.n = n
        def __call__(self, db_, thread_id, prompt, topic):
            if len(self.calls) >= self.n:
                raise gd.CapacityError("pool full")
            super().__call__(db_, thread_id, prompt, topic)

    stats = gd.graph_tick(db, dispatch_fn=CapAfter(2))
    assert len(stats["dispatched"]) == 2
    assert {t_[0] for t_ in stats["dispatched"]} == {"A", "B"}
    for tid_ in stats["deferred"]:
        assert tp.get_topic(db, tid_)["state"] == "ready", "claim released"


def test_single_project_single_topic_behavior_unchanged(db):
    """R6 pin: a 1-element armed set with synthetic 1-task topics behaves like
    the legacy flat tick (one dispatch, dep-gated)."""
    _mk_topic(db, "T-a")
    tp.create_topic(db, topic_id="T-b", project_id="INBOX", title="b")
    g.create_task(db, task_id="b", project_id="INBOX", title="b", prompt="p")
    with db._connect() as conn:
        conn.execute("UPDATE graph_tasks SET topic_id='T-b' WHERE id='b'")
        conn.execute(
            "INSERT INTO graph_edges (task_id, depends_on_id) VALUES ('b','T-a-k0')")
        conn.commit()
    tp.recompute_topic_ready(db, "INBOX")
    _arm(db)  # legacy scalar arm helper
    stats = gd.graph_tick(db, dispatch_fn=FakeDispatch())
    assert stats["dispatched"] == ["T-a"]  # T-b gated on T-a via derived dep


# ── P8 Task 4.1 (c4-task-reads): claim/sweep operate on nodes ─────────────────


def _seed_node_task(db, task_id, *, state="ready", project_id="INBOX",
                    dispatch_thread_id=None, updated_at=None):
    """Seed a task into nodes ONLY (no graph_tasks row) — P8 Task 4.1."""
    from dbops.schema import _now
    now = updated_at or _now()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO nodes (id, kind, title, objective, state, project_id, "
            "dispatch_thread_id, created_at, updated_at) "
            "VALUES (?, 'task', ?, '', ?, ?, ?, ?, ?)",
            (task_id, task_id, state, project_id, dispatch_thread_id, now, now),
        )
        conn.commit()


def test_claim_task_operates_on_nodes_only(db):
    """2026-06-29 P8 Task 4.1 (c4-task-reads): claim_task's CAS runs against
    nodes — a 'ready' task present ONLY in nodes (no graph_tasks row) is claimed
    exactly once and lands in 'dispatching'. RED before the flip: cas_state used
    the legacy row as the claim token, so a nodes-only task was never claimable."""
    _seed_node_task(db, "n", state="ready")
    assert gd.claim_task(db, "n") is True
    assert gd.claim_task(db, "n") is False  # already dispatching — lost race
    row = db._connect().execute(
        "SELECT state FROM nodes WHERE id='n'").fetchone()
    assert row[0] == "dispatching"


def test_sweep_stale_claims_reads_nodes_only(db):
    """2026-06-29 P8 Task 4.1 (c4-task-reads): sweep_stale_claims finds stale
    'dispatching' tasks via nodes (dispatch_thread_id IS NULL, updated_at old),
    not graph_tasks, and resets them to 'ready'. RED before the flip."""
    old = "2000-01-01T00:00:00+00:00"
    _seed_node_task(db, "s", state="dispatching", dispatch_thread_id=None,
                    updated_at=old)
    swept = gd.sweep_stale_claims(db, "INBOX")
    assert "s" in swept
    row = db._connect().execute(
        "SELECT state FROM nodes WHERE id='s'").fetchone()
    assert row[0] == "ready"
