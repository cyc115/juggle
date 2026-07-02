"""T-fix-reuse-before-cap: acquire_agent reuses an idle agent before raising cap.

Incident (2026-07-01): a ready task stalled at the pool cap even though an
idle, role-matching agent sat in the pool. Root cause — acquire_agent's cap
check (count ALL agents) ran BEFORE the idle-reuse CAS scan, so a pool-neutral
reuse was wrongly refused with CapacityError. Reuse does not grow the pool, so
the cap must only gate the spawn branch.

These tests pin the reorder: idle-reuse scan first; CapacityError only when no
role-matching idle agent can be claimed AND the pool is at cap.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB  # noqa: E402
from juggle_graph_dispatch import CapacityError  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "reuse.db"))
    d.init_db()
    d.set_active(True)
    return d


@pytest.fixture
def thread_id(db):
    return db.create_thread("reuse-test", session_id="")


def _fake_mgr():
    mgr = MagicMock()
    mgr.wait_for_ready_to_paste.return_value = True
    mgr._run_tmux.return_value = MagicMock(returncode=0, stdout="")
    return mgr


def test_reuse_idle_agent_at_cap_does_not_raise(db, thread_id, monkeypatch):
    """Pool at cap with an idle role-matching agent → reuse it, never CapacityError.

    Regression pin for the 2026-07-01 incident: ready task stalled at cap
    despite an idle agent in the pool.
    """
    from juggle_dispatch_core import acquire_agent

    # Shrink the cap to 1 and fill the pool with a single idle coder — the pool
    # is now AT cap, but the agent is reusable (pool-neutral).
    monkeypatch.setattr("juggle_db.MAX_BACKGROUND_AGENTS", 1)
    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")
    db.create_agent(role="coder", pane_id="%pool1", harness="claude", repo_path="")

    mgr = _fake_mgr()
    # Must NOT raise: the idle agent is reused rather than refused at cap.
    result = acquire_agent(db, thread_id, role="coder", harness="claude", _mgr=mgr)

    assert result is not None
    assert result["assigned_thread"] == thread_id
    # spawn must NOT have been called — the pool did not grow.
    mgr.spawn_agent.assert_not_called()


def test_cap_still_raises_when_no_reusable_agent(db, thread_id, monkeypatch):
    """Pool at cap with NO role-matching idle agent → CapacityError (spawn gated)."""
    from juggle_dispatch_core import acquire_agent

    monkeypatch.setattr("juggle_db.MAX_BACKGROUND_AGENTS", 1)
    monkeypatch.setattr("juggle_tmux._spawn_repo_path", lambda: "")
    # Fill the pool with a harness-mismatched idle agent — cannot be reused for
    # a 'claude' request, and the pool is at cap so no spawn is allowed.
    db.create_agent(role="coder", pane_id="%pool1", harness="deepseek", repo_path="")

    mgr = _fake_mgr()
    with pytest.raises(CapacityError):
        acquire_agent(db, thread_id, role="coder", harness="claude", _mgr=mgr)
