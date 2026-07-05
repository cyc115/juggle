"""P2 (Loop Entity V2, Release 1) — general per-node model persist + best-effort
headroom preference in agent acquisition.

Six named regression pins (plan roster 2026-07-04), each naming its incident:
  - model-honored-cold-spawn       — the tick resolves a topic's persisted model
                                      and threads it to spawn (lost today: dispatch
                                      passed model=None).
  - launch-model-not-clobbered     — warm reuse must NOT overwrite a pane's
                                      IMMUTABLE launch model (a reused Claude pane
                                      cannot re-model; matching depends on it).
  - headroom-prefers-right-model   — with spawn headroom, a wrong-model idle pane
                                      is SKIPPED for a right-model cold-spawn.
  - saturation-falls-back-no-starve— saturated pool falls back to ANY idle pane
                                      (wrong model accepted) — asserts NO raise.
  - null-matches-any               — a NULL node-model is today's behavior exactly
                                      (reuse any structural match).
  - migration-idempotent           — the additive nodes.model migration is
                                      presence-guarded (reapply is a no-op).

All tests isolate to a per-test tmp_path DB (conftest fail-closes the prod DB).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB  # noqa: E402
from juggle_graph_dispatch import CapacityError  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "model_pref.db"))
    d.init_db()
    d.set_active(True)
    return d


@pytest.fixture
def thread_id(db):
    return db.create_thread("model-pref-test", session_id="")


def _fake_mgr(pane_id="%fake"):
    mgr = MagicMock()
    mgr.wait_for_ready_to_paste.return_value = True
    mgr._run_tmux.return_value = MagicMock(returncode=0, stdout="")
    mgr.verify_pane.return_value = True
    return mgr


def _spawning_mgr():
    """A mock manager whose spawn_agent creates a REAL idle agent in the DB and
    returns it (so acquire_agent's post-spawn update_agent has a real row)."""
    mgr = _fake_mgr()

    def fake_spawn(db_, role, *, model=None, harness_override=None, effort=None):
        aid = db_.create_agent(
            role=role, pane_id="%spawned",
            harness=harness_override or "claude", repo_path="",
        )
        db_.update_agent(aid, model=model)
        return db_.get_agent(aid)

    mgr.spawn_agent.side_effect = fake_spawn
    return mgr


def _insert_node(db, node_id, *, kind="topic", model=None, parent_id=None,
                 project_id="INBOX"):
    """Insert a minimal kind='topic'/'task' node carrying a persisted model."""
    from dbops.schema import _now
    now = _now()
    with db._connect() as c:
        c.execute(
            "INSERT INTO nodes (id, kind, title, objective, state, project_id, "
            "parent_id, model, created_at, updated_at) "
            "VALUES (?, ?, ?, '', 'open', ?, ?, ?, ?, ?)",
            (node_id, kind, node_id, project_id, parent_id, model, now, now),
        )
        c.commit()


def _make_idle_agent(db, *, model, role="coder", pane="%pool", harness="claude"):
    aid = db.create_agent(role=role, pane_id=pane, harness=harness, repo_path="")
    db.update_agent(aid, model=model)
    return aid


# ── model-honored-cold-spawn ────────────────────────────────────────────────────


def test_model_honored_cold_spawn(db, thread_id, monkeypatch):
    """model-honored-cold-spawn — a topic's persisted nodes.model is resolved by
    the tick and threaded to dispatch. Incident (2026-07-04): the graph tick
    resolved NO model — dispatch_node was called with model=None — so a topic's
    model was lost even on a fresh cold spawn.
    """
    import juggle_graph_dispatch as gd
    import juggle_dispatch_core as _core

    _insert_node(db, "T-topic", kind="topic", model="opus")
    _insert_node(db, "T-topic-r1-task", kind="task", model=None, parent_id="T-topic")

    from juggle_graph_dispatch import _resolve_dispatch_model

    # (a) the resolver reads the persisted column at topic grain …
    assert _resolve_dispatch_model(db, {"id": "T-topic"}) == "opus"
    # … and a task inherits its parent topic's model via parent_id.
    assert _resolve_dispatch_model(db, {"id": "T-topic-r1-task"}) == "opus"

    # (b) the tick threads the resolved model into dispatch_node.
    captured: dict = {}

    def fake_dispatch_node(db_, tid, prompt, task, *, role="coder", model=None):
        captured["role"] = role
        captured["model"] = model

    monkeypatch.setattr(_core, "dispatch_node", fake_dispatch_node)
    gd._dispatch_via_pool(db, thread_id, "prompt", {"id": "T-topic"})
    assert captured["model"] == "opus", "tick must thread the topic's model to spawn"


def test_cold_spawn_threads_model_to_spawn_agent(db, thread_id, monkeypatch):
    """model-honored-cold-spawn (final hop) — acquire_agent cold-spawns with the
    requested model. Empty pool + headroom → spawn_agent(model=...)."""
    from juggle_dispatch_core import acquire_agent

    monkeypatch.setattr("juggle_db.MAX_BACKGROUND_AGENTS", 5)
    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")

    mgr = _spawning_mgr()
    acquire_agent(db, thread_id, role="coder", model="opus", harness="claude", _mgr=mgr)

    assert mgr.spawn_agent.called
    assert mgr.spawn_agent.call_args.kwargs.get("model") == "opus"


# ── launch-model-not-clobbered ──────────────────────────────────────────────────


def test_launch_model_not_clobbered(db, thread_id, monkeypatch):
    """launch-model-not-clobbered — warm reuse must NOT overwrite the pane's
    IMMUTABLE launch model. Incident (2026-07-04): acquire_agent stamped the
    requested model onto agents.model on the warm-reuse path, so once the tick
    threaded a real model the pane's launch model became 'last-requested' and the
    headroom preference could no longer match against the true launch model.

    Saturated pool (MAX=1) with one wrong-model idle pane → acquire a DIFFERENT
    model → falls back to reuse it; its stored launch model must be unchanged.
    """
    from juggle_dispatch_core import acquire_agent

    monkeypatch.setattr("juggle_db.MAX_BACKGROUND_AGENTS", 1)
    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")
    aid = _make_idle_agent(db, model="sonnet", pane="%pool1")

    mgr = _spawning_mgr()
    got = acquire_agent(db, thread_id, role="coder", model="opus",
                        harness="claude", _mgr=mgr)

    assert got["id"] == aid, "must reuse the idle pane under saturation"
    assert db.get_agent(aid)["model"] == "sonnet", (
        "warm reuse clobbered the pane's launch model with the requested model"
    )
    mgr.spawn_agent.assert_not_called()


# ── headroom-prefers-right-model ────────────────────────────────────────────────


def test_headroom_prefers_right_model(db, thread_id, monkeypatch):
    """headroom-prefers-right-model — with spawn headroom, a wrong-model idle pane
    is SKIPPED so a right-model cold-spawn happens. Incident (2026-07-04):
    acquire_agent matched panes on repo/role/harness and IGNORED model, so it
    reused a wrong-model idle pane even when the pool had room to spawn the right
    model.
    """
    from juggle_dispatch_core import acquire_agent

    monkeypatch.setattr("juggle_db.MAX_BACKGROUND_AGENTS", 5)  # headroom
    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")
    wrong = _make_idle_agent(db, model="sonnet", pane="%pool1")

    mgr = _spawning_mgr()
    got = acquire_agent(db, thread_id, role="coder", model="opus",
                        harness="claude", _mgr=mgr)

    # cold-spawned the right model rather than reusing the wrong-model pane
    assert mgr.spawn_agent.called
    assert mgr.spawn_agent.call_args.kwargs.get("model") == "opus"
    assert got["id"] != wrong, "must not reuse the wrong-model idle pane under headroom"
    assert db.get_agent(wrong)["assigned_thread"] is None, "wrong-model pane left idle"


# ── saturation-falls-back-no-starve ─────────────────────────────────────────────


def test_saturation_falls_back_no_starve(db, thread_id, monkeypatch):
    """saturation-falls-back-no-starve — a saturated pool (no spawn slot, no
    right-model idle pane) falls back to reusing ANY idle pane; it must NEVER
    raise CapacityError (the whole reason idle-TTL/anti-starvation was cut).
    """
    from juggle_dispatch_core import acquire_agent

    monkeypatch.setattr("juggle_db.MAX_BACKGROUND_AGENTS", 1)  # saturated
    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")
    wrong = _make_idle_agent(db, model="sonnet", pane="%pool1")

    mgr = _spawning_mgr()
    # Must NOT raise — falls back to the wrong-model idle pane.
    got = acquire_agent(db, thread_id, role="coder", model="opus",
                        harness="claude", _mgr=mgr)

    assert got is not None
    assert got["id"] == wrong
    assert got["assigned_thread"] == thread_id
    mgr.spawn_agent.assert_not_called()  # pool did not grow


def test_saturation_no_idle_still_raises(db, thread_id, monkeypatch):
    """saturation-falls-back-no-starve (boundary) — a saturated pool with NO
    reusable idle pane at all still raises CapacityError (unchanged safety)."""
    from juggle_dispatch_core import acquire_agent

    monkeypatch.setattr("juggle_db.MAX_BACKGROUND_AGENTS", 1)
    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")
    # harness-mismatched → not reusable for a 'claude' request; pool is at cap.
    _make_idle_agent(db, model="sonnet", pane="%pool1", harness="deepseek")

    mgr = _spawning_mgr()
    with pytest.raises(CapacityError):
        acquire_agent(db, thread_id, role="coder", model="opus",
                      harness="claude", _mgr=mgr)


# ── null-matches-any ────────────────────────────────────────────────────────────


def test_null_matches_any(db, thread_id, monkeypatch):
    """null-matches-any — a NULL node-model preserves today's behavior exactly:
    reuse the first structural (repo/role/harness) match, model-blind, no spawn.
    """
    from juggle_dispatch_core import acquire_agent

    monkeypatch.setattr("juggle_db.MAX_BACKGROUND_AGENTS", 5)
    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")
    aid = _make_idle_agent(db, model="sonnet", pane="%pool1")

    mgr = _spawning_mgr()
    got = acquire_agent(db, thread_id, role="coder", model=None,
                        harness="claude", _mgr=mgr)

    assert got["id"] == aid, "NULL model must reuse any structural match"
    mgr.spawn_agent.assert_not_called()


# ── migration-idempotent ────────────────────────────────────────────────────────


def test_migration_idempotent():
    """migration-idempotent — the additive nodes.model migration is presence-
    guarded: reapplying it is a no-op, never a duplicate-column error. Incident
    (2026-07-02): two coders landed duplicate ADD COLUMN for the same migration
    number; presence-guarded + reserved-number migrations prevent it.
    """
    from dbops.migration_74_node_model import migrate_74_node_model
    from dbops.schema_nodes import CREATE_NODES

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(CREATE_NODES)

    assert "model" not in {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    migrate_74_node_model(conn)
    migrate_74_node_model(conn)  # reapply — must not raise

    info = list(conn.execute("PRAGMA table_info(nodes)"))
    cols = [r[1] for r in info]
    assert "model" in cols
    assert cols.count("model") == 1, "duplicate model column"
    # DEFAULT NULL (today's behavior — harness/role-resolved). PRAGMA reports the
    # default expression as text, so an explicit `DEFAULT NULL` reads as 'NULL'.
    model_col = [r for r in info if r[1] == "model"][0]
    assert model_col[4] in (None, "NULL"), "nodes.model must DEFAULT NULL"
    # a fresh row leaves model NULL (not some non-null default).
    conn.execute(
        "INSERT INTO nodes (id, kind, title, objective, state, created_at, updated_at) "
        "VALUES ('n1','topic','t','','open','now','now')"
    )
    assert conn.execute("SELECT model FROM nodes WHERE id='n1'").fetchone()[0] is None
