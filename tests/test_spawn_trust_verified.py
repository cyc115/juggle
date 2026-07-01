"""Phase A — trust-safe verified agent spawn.

Pins the 2026-06-20 watchdog/agent-pane leak incident: a fresh worktree dir
triggers Claude Code's "Do you trust the files in this folder?" gate (NOT
bypassed by --permission-mode bypassPermissions). An agent spawned into the
idle pool but stuck at that trust screen was registered as a normal idle agent
and leaked its pane forever, because:

  1. the pre-trust helper wrote `allowedTools` but never
     `hasTrustDialogAccepted: true` (the field Claude Code actually reads), so
     the trust dialog still fired; and
  2. `spawn_agent` returned a live DB agent without ever confirming the Claude
     UI came up (no `wait_for_ready_to_paste`).
"""

import json
import sys
from pathlib import Path
from unittest import mock

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


# ---------------------------------------------------------------------------
# Pin 1: the trust helper must set the field Claude Code actually reads.
# ---------------------------------------------------------------------------


def test_ensure_dir_trusted_sets_has_trust_dialog_accepted(tmp_path):
    """A pre-trusted dir must carry hasTrustDialogAccepted=true.

    2026-06-20 leak incident: the old helper wrote only {"allowedTools": []};
    Claude Code keys folder-trust off `hasTrustDialogAccepted`, so the dialog
    still fired and the agent hung at the trust screen.
    """
    from juggle_claude_trust import ensure_dir_trusted

    cfg = tmp_path / "claude.json"
    target = "/tmp/juggle-some-worktree-AB"

    ensure_dir_trusted(target, claude_json_path=cfg)

    data = json.loads(cfg.read_text())
    entry = data["projects"][target]
    assert entry.get("hasTrustDialogAccepted") is True


def test_ensure_dir_trusted_preserves_existing_projects(tmp_path):
    """Pre-trusting one dir must not clobber other project entries."""
    from juggle_claude_trust import ensure_dir_trusted

    cfg = tmp_path / "claude.json"
    cfg.write_text(json.dumps({
        "projects": {"/existing/repo": {"allowedTools": ["Bash"], "foo": 1}},
        "numStartups": 7,
    }))

    ensure_dir_trusted("/tmp/new-dir", claude_json_path=cfg)

    data = json.loads(cfg.read_text())
    # existing entry intact
    assert data["projects"]["/existing/repo"]["foo"] == 1
    assert data["projects"]["/existing/repo"]["allowedTools"] == ["Bash"]
    # unrelated top-level keys intact
    assert data["numStartups"] == 7
    # new entry trusted
    assert data["projects"]["/tmp/new-dir"]["hasTrustDialogAccepted"] is True


def test_ensure_dir_trusted_upgrades_untrusted_existing_entry(tmp_path):
    """An entry that exists but lacks trust must be upgraded in place.

    The 1826 juggle-created entries had {"allowedTools": []} with no trust
    flag — re-running pre-trust must flip the flag, not skip because the key
    is already present.
    """
    from juggle_claude_trust import ensure_dir_trusted

    cfg = tmp_path / "claude.json"
    target = "/tmp/juggle-juggle-A"
    cfg.write_text(json.dumps({"projects": {target: {"allowedTools": []}}}))

    ensure_dir_trusted(target, claude_json_path=cfg)

    data = json.loads(cfg.read_text())
    assert data["projects"][target]["hasTrustDialogAccepted"] is True
    # original keys preserved
    assert data["projects"][target]["allowedTools"] == []


def test_register_worktree_trust_backcompat_sets_trust(tmp_path, monkeypatch):
    """Back-compat shim _register_worktree_trust must now actually trust."""
    from juggle_cmd_agents_worktree import _register_worktree_trust

    cfg = tmp_path / "claude.json"
    monkeypatch.setenv("JUGGLE_CLAUDE_JSON_PATH", str(cfg))

    _register_worktree_trust("/tmp/juggle-wt-XY")

    data = json.loads(cfg.read_text())
    assert data["projects"]["/tmp/juggle-wt-XY"]["hasTrustDialogAccepted"] is True


# ---------------------------------------------------------------------------
# Pin 2: a spawn that never becomes ready must NOT leak (no DB agent, pane
# killed, failure raised).
# ---------------------------------------------------------------------------


def _make_mgr_with_db():
    """Real JuggleTmuxManager with tmux calls + readiness mocked out."""
    from juggle_tmux import JuggleTmuxManager

    mgr = JuggleTmuxManager()
    db = mock.MagicMock()
    db.get_all_agents.return_value = []
    # create_agent returns an id and get_agent echoes a minimal record
    db.create_agent.return_value = "agent-xyz"
    db.get_agent.return_value = {"id": "agent-xyz", "pane_id": "%99"}
    return mgr, db


def test_spawn_agent_never_ready_does_not_create_db_agent(monkeypatch):
    """A spawn stuck at trust/never-ready must not register a usable agent.

    2026-06-20 leak incident: spawn_agent created the DB agent unconditionally,
    so a pane hung at the trust screen looked like a healthy idle agent.
    """
    mgr, db = _make_mgr_with_db()

    monkeypatch.setattr(mgr, "ensure_session", lambda: None)
    monkeypatch.setattr(mgr, "spawn_pane", lambda: "%99")
    monkeypatch.setattr(mgr, "start_agent_in_pane", lambda *a, **k: None)
    # Never becomes ready.
    monkeypatch.setattr(mgr, "wait_for_ready_to_paste", lambda *a, **k: False)
    killed = []
    monkeypatch.setattr(mgr, "kill_pane", lambda pid: killed.append(pid))
    # Pre-trust is best-effort; stub it so no real ~/.claude.json write happens.
    monkeypatch.setattr(
        "juggle_tmux._pretrust_spawn_dir", lambda *a, **k: None, raising=False
    )

    with pytest.raises(RuntimeError):
        mgr.spawn_agent(db, role="general")

    # Never registered a DB agent…
    db.create_agent.assert_not_called()
    # …and every orphan pane was killed. The mandatory model-fallback
    # (T-coder-model-resolution) retries ONCE on the harness default when the
    # first boot never readies, so a never-ready spawn kills both panes.
    assert killed == ["%99", "%99"]


def test_spawn_agent_ready_creates_agent(monkeypatch):
    """The happy path still creates an agent once the UI is ready."""
    mgr, db = _make_mgr_with_db()

    monkeypatch.setattr(mgr, "ensure_session", lambda: None)
    monkeypatch.setattr(mgr, "spawn_pane", lambda: "%99")
    monkeypatch.setattr(mgr, "start_agent_in_pane", lambda *a, **k: None)
    monkeypatch.setattr(mgr, "wait_for_ready_to_paste", lambda *a, **k: True)
    monkeypatch.setattr(mgr, "kill_pane", lambda pid: None)
    monkeypatch.setattr(
        "juggle_tmux._pretrust_spawn_dir", lambda *a, **k: None, raising=False
    )

    agent = mgr.spawn_agent(db, role="general")

    db.create_agent.assert_called_once()
    assert agent["id"] == "agent-xyz"
