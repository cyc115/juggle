"""Regression pins: send-task base-repo resolution must never root a worktree in
the Claude plugin install dir (~/.claude).

Incident (2026-06-29): a coder dispatched via the INSTALLED plugin copy
(~/.claude/plugins/cache/juggle/juggle/<ver>/src/juggle_cli.py) built its worktree
from ~/.claude instead of the juggle source. Root cause: spawn_repo_path resolved
the base via canonical_repo_path(), which anchors on Path(__file__).parent.parent
— under ~/.claude/plugins/... when installed — and `git worktree list` walked up
into the enclosing ~/.claude git repo, returning /Users/mikechen/.claude. That
value flowed to `git worktree add`, producing /tmp/juggle-.claude-<thread>.

Fix (user-decided): PRIMARY anchor = the agent PANE's cwd (where the coder
actually runs), with a bad-base rejection (~/.claude / plugin dir / basename
'.claude') as defense-in-depth. These pins fail RED on the pre-fix code and
pass GREEN after.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import juggle_repo_binding as rb  # noqa: E402
import juggle_watchdog_singleton as ws  # noqa: E402


def test_is_bad_base_flags_claude_plugin_and_basename(tmp_path):
    """_is_bad_base rejects ~/.claude, anything under it, any '.claude'-basename
    path, and the empty string — but accepts an ordinary repo path."""
    assert rb._is_bad_base("") is True
    assert rb._is_bad_base(str(Path.home() / ".claude")) is True
    assert rb._is_bad_base(str(Path.home() / ".claude/plugins/cache/juggle")) is True
    fake = tmp_path / ".claude"
    fake.mkdir()
    assert rb._is_bad_base(str(fake)) is True       # basename tripwire (RCA §7)
    ok = tmp_path / "github-juggle"
    ok.mkdir()
    assert rb._is_bad_base(str(ok)) is False


def test_spawn_repo_path_rejects_plugin_install_dir(monkeypatch):
    """2026-06-29: send-task built worktrees from ~/.claude when run from the
    installed plugin copy. spawn_repo_path must REJECT a ~/.claude canonical
    resolution and fall through to the (real, non-plugin) cwd git toplevel.

    RED on pre-fix code: the old short-circuit returned canonical (~/.claude)
    verbatim with no rejection."""
    monkeypatch.setattr(ws, "canonical_repo_path",
                        lambda *a, **k: str(Path.home() / ".claude"))
    result = rb.spawn_repo_path()  # no pane → canonical rejected → cwd toplevel
    assert result != str(Path.home() / ".claude")
    assert not rb.is_plugin_install_dir(result)


def test_pane_cwd_anchor_yields_juggle_toplevel_when_canonical_is_claude(monkeypatch):
    """A pane whose cwd is a real juggle checkout must yield that checkout's
    toplevel even when canonical_repo_path() (anchored on __file__ under the
    plugin dir) resolves to ~/.claude. Proves the PRIMARY pane-cwd anchor.

    RED on pre-fix code: spawn_repo_path took no pane_id and had no pane anchor."""
    monkeypatch.setattr(ws, "canonical_repo_path",
                        lambda *a, **k: str(Path.home() / ".claude"))
    # The agent pane's cwd is THIS repo (a real juggle checkout); let the real
    # `git rev-parse --show-toplevel` resolve it.
    repo_top = rb._git_toplevel(str(Path(__file__).resolve().parent))
    assert repo_top  # sanity: tests run inside a git checkout
    monkeypatch.setattr(rb, "_pane_cwd", lambda pane_id: repo_top)

    result = rb.spawn_repo_path(pane_id="%agentpane")
    assert result == repo_top
    assert not rb.is_plugin_install_dir(result)


def test_pane_repo_path_rejects_bad_pane_cwd(monkeypatch):
    """pane_repo_path must reject a pane cwd that resolves to a bad base
    (~/.claude), returning '' so the caller falls through to a safe anchor."""
    monkeypatch.setattr(rb, "_pane_cwd", lambda pane_id: "/anything")
    monkeypatch.setattr(rb, "_git_toplevel", lambda p: str(Path.home() / ".claude"))
    assert rb.pane_repo_path("%p") == ""


def test_pane_repo_path_empty_when_no_pane():
    """No pane id → no anchor (empty), so spawn_repo_path falls to canonical/cwd."""
    assert rb.pane_repo_path(None) == ""
    assert rb.pane_repo_path("") == ""
