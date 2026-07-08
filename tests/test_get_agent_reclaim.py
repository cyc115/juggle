"""Tests for `agent get` auto-reclaim of an idle slot at the pool cap.

Incident (2026-07-07): `agent get --role coder` hard-failed with "Agent pool
full" while 3 idle researchers sat in the pool doing nothing — the cap should
reclaim ONE reclaimable idle agent (LRU) and retry, not refuse. HARD safety
guard: never reclaim an idle agent that owns a worktree with unmerged commits
(a done-but-unreleased agent can hold unintegrated work — the L1/ST pattern).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB
from juggle_graph_dispatch import CapacityError


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


def _agent(id_, *, status="idle", assigned_thread=None, last_active="2026-01-01",
           repo_path=None, role="researcher"):
    return {
        "id": id_, "status": status, "assigned_thread": assigned_thread,
        "last_active": last_active, "repo_path": repo_path, "role": role,
        "pane_id": "%1", "harness": "claude", "model": None,
    }


# ── selector (pure, no DB) ──────────────────────────────────────────────────

def test_selector_returns_none_when_no_agents():
    from juggle_agent_reclaim import pick_reclaimable_idle_agent
    assert pick_reclaimable_idle_agent([]) is None


def test_selector_picks_lru_idle_agent():
    """LRU: the oldest last_active idle+unowned+clean agent is chosen."""
    from juggle_agent_reclaim import pick_reclaimable_idle_agent
    agents = [
        _agent("newest", last_active="2026-03-01"),
        _agent("oldest", last_active="2026-01-01"),
        _agent("middle", last_active="2026-02-01"),
    ]
    victim = pick_reclaimable_idle_agent(agents, has_unmerged=lambda a: False)
    assert victim["id"] == "oldest"


def test_selector_guard_excludes_busy_assigned_and_unmerged():
    """Guard: busy, assigned_thread, and unmerged-worktree agents are excluded."""
    from juggle_agent_reclaim import pick_reclaimable_idle_agent
    agents = [
        _agent("busy", status="busy", last_active="2026-01-01"),
        _agent("assigned", assigned_thread="t-1", last_active="2026-01-02"),
        _agent("unmerged", repo_path="/wt", last_active="2026-01-03"),
        _agent("clean", last_active="2026-01-04"),
    ]
    victim = pick_reclaimable_idle_agent(
        agents, has_unmerged=lambda a: a["id"] == "unmerged"
    )
    assert victim["id"] == "clean"


def test_selector_returns_none_when_only_unmerged_idle():
    """Only idle candidate owns unmerged work → None (pool treated as full)."""
    from juggle_agent_reclaim import pick_reclaimable_idle_agent
    agents = [_agent("unmerged", repo_path="/wt")]
    assert pick_reclaimable_idle_agent(agents, has_unmerged=lambda a: True) is None


# ── reclaim_one_idle_agent (real tmp DB) ────────────────────────────────────

def test_reclaim_decommissions_lru_idle_and_logs(db, capsys):
    """Reclaims the LRU idle agent, deletes it, returns True, logs to stderr."""
    from juggle_agent_reclaim import reclaim_one_idle_agent

    old = db.create_agent(role="researcher", pane_id="%1")
    new = db.create_agent(role="researcher", pane_id="%2")
    db.update_agent(old, last_active="2026-01-01")
    db.update_agent(new, last_active="2026-06-01")

    mock_cls = MagicMock()
    mock_mgr = mock_cls.return_value
    mock_mgr.decommission_agent.side_effect = lambda d, aid: d.delete_agent(aid)

    with patch("juggle_tmux.JuggleTmuxManager", mock_cls):
        ok = reclaim_one_idle_agent(db)

    assert ok is True
    assert db.get_agent(old) is None, "LRU idle agent must be decommissioned"
    assert db.get_agent(new) is not None, "newer idle agent must be retained"
    err = capsys.readouterr().err
    assert f"decommissioned idle agent {old[:8]}" in err


def test_reclaim_returns_false_when_all_busy(db):
    """No reclaimable agent → returns False, deletes nothing."""
    from juggle_agent_reclaim import reclaim_one_idle_agent

    busy = db.create_agent(role="coder", pane_id="%1")
    db.update_agent(busy, status="busy", assigned_thread="t-1")

    mock_cls = MagicMock()
    with patch("juggle_tmux.JuggleTmuxManager", mock_cls):
        ok = reclaim_one_idle_agent(db)

    assert ok is False
    mock_cls.return_value.decommission_agent.assert_not_called()
    assert db.get_agent(busy) is not None


def test_reclaim_refuses_idle_agent_with_unmerged_worktree(db):
    """HARD guard: an idle agent owning unmerged work is NOT reclaimed."""
    from juggle_agent_reclaim import reclaim_one_idle_agent

    idle = db.create_agent(role="researcher", pane_id="%1", repo_path="/wt")

    mock_cls = MagicMock()
    with patch("juggle_tmux.JuggleTmuxManager", mock_cls), \
         patch("juggle_agent_reclaim._agent_owns_unmerged_worktree",
               return_value=True):
        ok = reclaim_one_idle_agent(db)

    assert ok is False
    mock_cls.return_value.decommission_agent.assert_not_called()
    assert db.get_agent(idle) is not None, "unmerged-work agent must survive"


# ── cmd_get_agent control flow ──────────────────────────────────────────────

def _get_args(**kw):
    args = MagicMock()
    args.thread_id = kw.get("thread_id", "SW")
    args.role = kw.get("role", "coder")
    args.repo = kw.get("repo", "/repo")
    args.harness = kw.get("harness", None)
    args.fresh = kw.get("fresh", False)
    args.model = kw.get("model", None)
    args.effort = kw.get("effort", None)
    args.db_path = None
    return args


def _spawned(id_="agent-new"):
    return {
        "id": id_, "pane_id": "%9", "role": "coder", "harness": "claude",
        "repo_path": "/repo", "status": "busy", "assigned_thread": "t-SW",
        "model": None,
    }


def test_cmd_get_agent_reclaims_at_cap_then_succeeds(capsys):
    """At cap with an idle reclaimable agent: reclaim ONE, retry, succeed."""
    from juggle_cmd_agents_lifecycle import cmd_get_agent

    db = MagicMock()
    db.get_all_agents.return_value = []

    with patch("juggle_cmd_agents_common.get_db", return_value=db), \
         patch("juggle_cmd_agents_common._resolve_thread", return_value="t-SW"), \
         patch("juggle_dispatch_core.acquire_agent",
               side_effect=[CapacityError("full"), _spawned()]) as acq, \
         patch("juggle_agent_reclaim.reclaim_one_idle_agent",
               return_value=True) as reclaim:
        cmd_get_agent(_get_args())

    assert reclaim.call_count == 1, "must reclaim exactly one idle agent"
    assert acq.call_count == 2, "must retry acquire exactly once after reclaim"
    out = capsys.readouterr().out
    assert "agent-new" in out


def test_cmd_get_agent_errors_when_nothing_reclaimable(capsys):
    """At cap with ALL agents busy: no reclaim possible → pool-full error, exit 1."""
    from juggle_cmd_agents_lifecycle import cmd_get_agent

    db = MagicMock()

    with patch("juggle_cmd_agents_common.get_db", return_value=db), \
         patch("juggle_cmd_agents_common._resolve_thread", return_value="t-SW"), \
         patch("juggle_dispatch_core.acquire_agent",
               side_effect=CapacityError("full")) as acq, \
         patch("juggle_agent_reclaim.reclaim_one_idle_agent",
               return_value=False):
        with pytest.raises(SystemExit) as exc:
            cmd_get_agent(_get_args())

    assert exc.value.code == 1
    assert acq.call_count == 1, "no retry when nothing reclaimable"
    assert "pool full" in capsys.readouterr().out.lower()


def test_cmd_get_agent_reuse_path_never_reclaims(capsys):
    """Normal reuse/spawn (no CapacityError) must NOT touch the reclaim path."""
    from juggle_cmd_agents_lifecycle import cmd_get_agent

    db = MagicMock()
    db.get_all_agents.return_value = []

    with patch("juggle_cmd_agents_common.get_db", return_value=db), \
         patch("juggle_cmd_agents_common._resolve_thread", return_value="t-SW"), \
         patch("juggle_dispatch_core.acquire_agent", return_value=_spawned()), \
         patch("juggle_agent_reclaim.reclaim_one_idle_agent") as reclaim:
        cmd_get_agent(_get_args())

    reclaim.assert_not_called()
    assert "agent-new" in capsys.readouterr().out
