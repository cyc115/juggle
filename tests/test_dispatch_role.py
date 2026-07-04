"""Tests for loop-entity Phase 2 — per-node role dispatch.

The watchdog dispatcher used to send EVERY graph node as a hardcoded
``TASK_ROLE="coder"`` (juggle_graph_dispatch.py). Phase 2 resolves the role
from the node's ``nodes.role`` column (Phase-1, DEFAULT 'coder') so a
``role='researcher'`` node claims a researcher pane and runs read-only in place
(NO worktree), while ``coder``/``planner`` nodes are unchanged (worktree as
today) and legacy NULL-role nodes still resolve 'coder'.

The send-path worktree gate (juggle_dispatch_worktree_context.build_worktree_context,
the block send_task_to_agent actually calls) already early-exits for any role
outside {coder, planner}; Phase 2 is what makes the RIGHT role reach it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import juggle_cmd_agents_common as _com  # noqa: E402
import juggle_graph_dispatch as gd  # noqa: E402
from dbops.schema import _now  # noqa: E402
from juggle_db import JuggleDB  # noqa: E402
from juggle_dispatch_worktree_context import build_worktree_context  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "role.db"))
    d.init_db()
    return d


def _seed_node(db, nid, *, role, kind="topic", project="INBOX"):
    """Insert a node with an explicit role."""
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO nodes (id, kind, title, objective, state, project_id, "
            "role, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (nid, kind, nid, "", "ready", project, role, now, now),
        )
        conn.commit()


def _seed_node_default_role(db, nid, kind="topic", project="INBOX"):
    """Insert a node WITHOUT specifying role — it takes nodes.role DEFAULT
    'coder' (how a legacy/existing node reads after the Phase-1 ALTER backfill)."""
    now = _now()
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO nodes (id, kind, title, objective, state, project_id, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (nid, kind, nid, "", "ready", project, now, now),
        )
        conn.commit()


def _forwarded_role(db, monkeypatch, node) -> str:
    """The role _dispatch_via_pool forwards to dispatch_node for ``node``."""
    import juggle_dispatch_core as _core

    captured: dict = {}

    def spy(db_, tid_, prompt_, task_, *, role="coder", model=None):
        captured["role"] = role

    monkeypatch.setattr(_core, "dispatch_node", spy)
    tid = db.create_thread("x", session_id="s")
    gd._dispatch_via_pool(db, tid, "prompt", node)
    return captured["role"]


def _fake_mgr(pane_id="%fake"):
    mgr = MagicMock()
    mgr.wait_for_ready_to_paste.return_value = True
    mgr._run_tmux.return_value = MagicMock(returncode=0, stdout="")
    mgr.verify_pane.return_value = True
    return mgr


# ── role resolution reaches the dispatch layer ────────────────────────────────


def test_researcher_node_gets_no_worktree(db, monkeypatch):
    """Phase 2: a role='researcher' node dispatches as 'researcher' and the
    send-path worktree gate produces NO worktree (early-exit). RED on pre-Phase-2
    code: _dispatch_via_pool forwarded the hardcoded 'coder', so every node got a
    worktree (facts completion §5 — researcher must run read-only in place)."""
    _seed_node(db, "T-r", role="researcher")
    assert _forwarded_role(db, monkeypatch, {"id": "T-r"}) == "researcher"

    # Downstream gate lock: researcher never triggers worktree creation.
    created: list = []
    monkeypatch.setattr(
        _com, "_create_worktree",
        lambda *a, **k: (created.append(1), (True, "/wt", "br", "ok"))[1],
    )
    tid = db.create_thread("r", session_id="s")
    ctx = build_worktree_context(
        db, role="researcher", agent={"repo_path": "/repo"}, pane_id="%p",
        thread_id=tid, allow_main=False, worktree_path_override=None,
        worktree_branch_override=None, main_repo_override=None,
        default_worktree_root="/tmp",
    )
    assert ctx == ""
    assert created == [], "researcher must not create a worktree"
    assert (db.get_thread(tid) or {}).get("worktree_path") in (None, "")


def test_coder_node_unchanged_gets_worktree(db, monkeypatch):
    """REGRESSION PIN (Phase 2): a role='coder' node is UNCHANGED — it dispatches
    as 'coder' and the send-path gate creates/attaches an isolated worktree,
    exactly as before Phase 2. Zero regression for existing coder graphs."""
    _seed_node(db, "T-c", role="coder")
    assert _forwarded_role(db, monkeypatch, {"id": "T-c"}) == "coder"

    created: list = []
    monkeypatch.setattr(
        _com, "_create_worktree",
        lambda *a, **k: (created.append(1), (True, "/wt/c", "cyc_c", "made wt"))[1],
    )
    monkeypatch.setattr(
        "juggle_repo_binding.resolve_worktree_base", lambda *a, **k: "/repo",
    )
    tid = db.create_thread("c", session_id="s")
    ctx = build_worktree_context(
        db, role="coder", agent={"repo_path": "/repo"}, pane_id="%p",
        thread_id=tid, allow_main=False, worktree_path_override=None,
        worktree_branch_override=None, main_repo_override=None,
        default_worktree_root="/tmp",
    )
    assert created == [1], "coder must create a worktree"
    assert "## Working Directory" in ctx
    assert (db.get_thread(tid) or {}).get("worktree_path") == "/wt/c"


def test_planner_node_gets_worktree(db, monkeypatch):
    """Phase 2: a role='planner' node dispatches as 'planner' and (parity with
    coder) gets a worktree — the gate admits {coder, planner}."""
    _seed_node(db, "T-p", role="planner")
    assert _forwarded_role(db, monkeypatch, {"id": "T-p"}) == "planner"

    created: list = []
    monkeypatch.setattr(
        _com, "_create_worktree",
        lambda *a, **k: (created.append(1), (True, "/wt/p", "cyc_p", "made wt"))[1],
    )
    monkeypatch.setattr(
        "juggle_repo_binding.resolve_worktree_base", lambda *a, **k: "/repo",
    )
    tid = db.create_thread("p", session_id="s")
    ctx = build_worktree_context(
        db, role="planner", agent={"repo_path": "/repo"}, pane_id="%p",
        thread_id=tid, allow_main=False, worktree_path_override=None,
        worktree_branch_override=None, main_repo_override=None,
        default_worktree_root="/tmp",
    )
    assert created == [1] and "## Working Directory" in ctx


def test_dispatch_role_defaults_coder_when_unset(db, monkeypatch):
    """Phase 2: a legacy node carrying the nodes.role DEFAULT 'coder' (and a node
    absent from the DB) resolves 'coder' — existing graphs are unaffected."""
    _seed_node_default_role(db, "T-null")
    assert gd._resolve_dispatch_role(db, {"id": "T-null"}) == "coder"
    # Absent id → still coder (no crash), and an empty/None node → coder.
    assert gd._resolve_dispatch_role(db, {"id": "does-not-exist"}) == "coder"
    assert gd._resolve_dispatch_role(db, None) == "coder"
    assert _forwarded_role(db, monkeypatch, {"id": "T-null"}) == "coder"


def test_resolve_role_prefers_hydrated_dict_value(db):
    """A caller that already hydrated 'role' onto the node dict wins without a
    DB read (forward-compat for hydrated topic/task dicts)."""
    assert gd._resolve_dispatch_role(db, {"id": "unseeded", "role": "researcher"}) == "researcher"


# ── agent pool claims the matching role ───────────────────────────────────────


def test_acquire_agent_matches_role(db, monkeypatch):
    """Phase 2 pane match (agent-pool facts §4): a researcher dispatch claims a
    researcher-role idle pane, not a coder pane — acquire_agent filters idle
    candidates by the passed role."""
    from juggle_dispatch_core import acquire_agent

    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")
    coder_id = db.create_agent(role="coder", pane_id="%c", harness="claude", repo_path="")
    research_id = db.create_agent(role="researcher", pane_id="%r", harness="claude", repo_path="")
    tid = db.create_thread("t", session_id="s")

    agent = acquire_agent(db, tid, role="researcher", _mgr=_fake_mgr())

    assert agent["id"] == research_id, "researcher dispatch must claim the researcher pane"
    assert agent["role"] == "researcher"
    assert db.get_agent(coder_id)["status"] == "idle", "coder pane must stay idle"
