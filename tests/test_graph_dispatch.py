"""Tests for juggle_graph_dispatch — watchdog-owned dispatcher (autopilot Phase 2).

Covers: atomic claim (DA B4 pin: two concurrent claimers, exactly one wins),
cap-aware defer + next-tick retry, crash-mid-dispatch stale-claim sweep
recovery, hydration content (DA M4: dep handoffs + objective, never
thread.summary), and graph_tick orchestration (armed-key gating, dispatch
errors → node released + action item, disarm mid-batch).
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


def _mk(db, node_id, deps=(), state=None, **kw):
    g.create_node(
        db,
        node_id=node_id,
        project_id="INBOX",
        title=kw.get("title", f"Node {node_id}"),
        prompt=kw.get("prompt", f"do {node_id}"),
        verify_cmd=kw.get("verify_cmd"),
    )
    if deps:
        g.replace_edges(db, node_id, list(deps))


def _arm(db, project="INBOX"):
    db.set_setting(gd.ARMED_PROJECT_KEY, project)


class FakeDispatch:
    """Records dispatches; optionally raises. No tmux, no LLM."""

    def __init__(self, exc=None):
        self.calls: list[tuple[str, str, str]] = []  # (thread_id, prompt, node_id)
        self.exc = exc

    def __call__(self, db, thread_id, prompt, node):
        if self.exc:
            raise self.exc
        self.calls.append((thread_id, prompt, node["id"]))


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
    assert gd.claim_node(db, "a") is False  # pending — not claimable
    g.recompute_ready(db, "INBOX")
    assert gd.claim_node(db, "a") is True
    assert g.get_node(db, "a")["state"] == "dispatching"
    assert gd.claim_node(db, "a") is False  # already claimed


def test_concurrent_claim_exactly_one_wins(db, tmp_path):
    """REQUIRED PIN (DA B4, 2026-06-10): two dispatchers racing the same ready
    node spawned two agents on one thread/worktree — the atomic
    UPDATE..WHERE state='ready' claim must let EXACTLY one claimer win."""
    _mk(db, "a")
    g.recompute_ready(db, "INBOX")

    db2 = JuggleDB(db_path=str(tmp_path / "graph.db"))  # separate connection
    barrier = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def _claim(handle):
        barrier.wait()
        won = gd.claim_node(handle, "a")
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
    assert g.get_node(db, "a")["state"] == "dispatching"


# ── stale-claim sweep ──────────────────────────────────────────────────────────


def _age_claim(db, node_id, secs):
    old = (datetime.now(timezone.utc) - timedelta(seconds=secs)).isoformat()
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_nodes SET updated_at=? WHERE id=?", (old, node_id)
        )
        conn.commit()


def test_sweep_resets_stale_threadless_claims_only(db):
    _mk(db, "stale")
    _mk(db, "fresh")
    _mk(db, "bound")
    g.recompute_ready(db, "INBOX")
    for n in ("stale", "fresh", "bound"):
        assert gd.claim_node(db, n)
    g.set_node_thread(db, "bound", "some-thread")
    _age_claim(db, "stale", gd.STALE_CLAIM_SECS + 60)
    _age_claim(db, "bound", gd.STALE_CLAIM_SECS + 60)

    swept = gd.sweep_stale_claims(db, "INBOX")

    assert swept == ["stale"]
    assert g.get_node(db, "stale")["state"] == "ready"
    assert g.get_node(db, "fresh")["state"] == "dispatching"  # too young
    assert g.get_node(db, "bound")["state"] == "dispatching"  # has a thread


def test_crash_mid_dispatch_recovers_via_sweep_then_redispatches(db):
    """Crash between claim and send-task (no thread bound): after 10 min the
    sweep returns the node to ready and the SAME tick re-dispatches it."""
    _mk(db, "a")
    g.recompute_ready(db, "INBOX")
    assert gd.claim_node(db, "a")  # simulated dispatcher died right here
    _age_claim(db, "a", gd.STALE_CLAIM_SECS + 60)
    _arm(db)

    fake = FakeDispatch()
    stats = gd.graph_tick(db, dispatch_fn=fake)

    assert stats["swept"] == ["a"]
    assert stats["dispatched"] == ["a"]
    assert g.get_node(db, "a")["state"] == "running"
    assert g.get_node(db, "a")["thread_id"]


# ── hydration (DA M4) ──────────────────────────────────────────────────────────


def test_build_hydration_contains_objective_handoffs_prompt_contract():
    node = {
        "id": "api",
        "title": "Build API",
        "prompt": "Implement the API on top of the schema.",
        "verify_cmd": "uv run pytest tests/test_api.py -q",
    }
    deps = [
        {"id": "schema", "title": "Add schema", "handoff": "migration 35 adds graph tables; use db_graph.get_node"},
        {"id": "auth", "title": "Auth", "handoff": None},
    ]
    out = gd.build_hydration("Ship the autopilot.", node, deps)
    assert "Ship the autopilot." in out
    assert "migration 35 adds graph tables" in out
    assert "### schema — Add schema" in out  # dep section: id then title
    assert "(no handoff recorded)" in out  # dep without handoff degrades loudly
    assert "Implement the API on top of the schema." in out
    assert "uv run pytest tests/test_api.py -q" in out
    assert "--handoff" in out  # completion contract instruction


def test_hydration_never_uses_thread_summary(db):
    """DA M4: dependent prompts hydrate from dep handoffs, NEVER the dep
    thread's 80-char summary."""
    _mk(db, "a")
    _mk(db, "b", deps=("a",))
    tid = db.create_thread("dep thread", session_id="s")
    db.update_thread(tid, summary="SUMMARY-JUNK-MUST-NOT-LEAK")
    g.set_node_thread(db, "a", tid)
    g.recompute_ready(db, "INBOX")
    for ev in ("claim", "dispatch", "integrate_start", "integrate_ok"):
        g.node_transition(db, "a", ev)
    g.set_node_handoff(db, "a", "real handoff content")
    g.recompute_ready(db, "INBOX")  # b → ready
    _arm(db)

    fake = FakeDispatch()
    gd.graph_tick(db, dispatch_fn=fake)

    (_, prompt, node_id), = fake.calls
    assert node_id == "b"
    assert "real handoff content" in prompt
    assert "SUMMARY-JUNK-MUST-NOT-LEAK" not in prompt


# ── graph_tick orchestration ───────────────────────────────────────────────────


def test_tick_noop_when_disarmed(db):
    _mk(db, "a")
    g.recompute_ready(db, "INBOX")
    fake = FakeDispatch()
    stats = gd.graph_tick(db, dispatch_fn=fake)
    assert stats["dispatched"] == [] and fake.calls == []
    assert g.get_node(db, "a")["state"] == "ready"


def test_tick_dispatches_ready_nodes_and_binds_threads(db):
    _mk(db, "a")
    _mk(db, "b")
    _mk(db, "c", deps=("a",))
    g.recompute_ready(db, "INBOX")
    _arm(db)
    fake = FakeDispatch()

    stats = gd.graph_tick(db, dispatch_fn=fake)

    assert sorted(stats["dispatched"]) == ["a", "b"]
    for nid in ("a", "b"):
        node = g.get_node(db, nid)
        assert node["state"] == "running"
        thread = db.get_thread(node["thread_id"])
        assert thread is not None
        assert thread["project_id"] == "INBOX"
    assert g.get_node(db, "c")["state"] == "pending"  # dep not verified


def test_tick_cap_hit_defers_and_retries_next_tick(db, monkeypatch):
    """MAX_THREADS during lazy create_thread: skip + retry next tick, node
    back to 'ready', daemon never crashes (cap-aware lazy threads)."""
    import dbops.threads as threads_mod

    _mk(db, "a")
    g.recompute_ready(db, "INBOX")
    _arm(db)
    monkeypatch.setattr(threads_mod, "MAX_THREADS", 0)

    fake = FakeDispatch()
    stats = gd.graph_tick(db, dispatch_fn=fake)
    assert stats["deferred"] == ["a"] and fake.calls == []
    assert g.get_node(db, "a")["state"] == "ready"  # claim released

    monkeypatch.setattr(threads_mod, "MAX_THREADS", 10)
    stats = gd.graph_tick(db, dispatch_fn=fake)  # next tick: cap lifted
    assert stats["dispatched"] == ["a"]
    assert g.get_node(db, "a")["state"] == "running"


def test_tick_dispatch_failure_releases_node_and_files_action_item(db):
    _mk(db, "a")
    g.recompute_ready(db, "INBOX")
    _arm(db)

    stats = gd.graph_tick(db, dispatch_fn=FakeDispatch(exc=RuntimeError("tmux gone")))

    assert stats["errors"] == ["a"]
    node = g.get_node(db, "a")
    assert node["state"] == "ready"  # released for retry/operator
    assert node["thread_id"] is None
    items = db.get_open_action_items()
    assert any("dispatch failed" in i["message"] and "a" in i["message"] for i in items)
    # orphan thread was archived, not leaked into the active cap
    assert all(t["status"] == "archived" for t in db.get_all_threads() if t)


def test_tick_capacity_error_defers_quietly(db):
    _mk(db, "a")
    g.recompute_ready(db, "INBOX")
    _arm(db)

    stats = gd.graph_tick(db, dispatch_fn=FakeDispatch(exc=gd.CapacityError("pool full")))

    assert stats["deferred"] == ["a"] and stats["errors"] == []
    assert g.get_node(db, "a")["state"] == "ready"
    assert db.get_open_action_items() == []  # no spam for capacity defers


def test_tick_stops_claiming_when_disarmed_mid_batch(db):
    _mk(db, "a")
    _mk(db, "b")
    g.recompute_ready(db, "INBOX")
    _arm(db)

    def disarming_dispatch(db_, thread_id, prompt, node):
        db_.set_setting(gd.ARMED_PROJECT_KEY, None)  # disarm during node 1

    stats = gd.graph_tick(db, dispatch_fn=disarming_dispatch)

    assert len(stats["dispatched"]) == 1  # second node never claimed
    states = {nid: g.get_node(db, nid)["state"] for nid in ("a", "b")}
    assert sorted(states.values()) == ["ready", "running"]
