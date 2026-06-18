"""Regression: integrate must REFUSE a mis-bound autopilot topic.

Incident (2026-06-16, multi-repo mis-binding cascade): an autopilot topic bound
to ~/.claude (the plugin install dir, NOT juggle's source) reached integrate.
The topic branch was empty there, so the ff-merge/push either dropped the work
or advanced origin/main to an unrelated branch's HEAD out-of-band. This guard
refuses to integrate when an autopilot thread's main_repo_path does not resolve
to juggle's canonical source repo — BEFORE any git side effects.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import juggle_cmd_integrate as jci  # noqa: E402
import juggle_watchdog_singleton as ws  # noqa: E402


def _git_repo(path: Path, *, juggle_source: bool) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    if juggle_source:
        (path / "src").mkdir(exist_ok=True)
        (path / "src" / "juggle_cli.py").write_text("# juggle source marker\n")
    (path / "a.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "branch", "-M", "main"], check=True)
    return str(path)


@pytest.fixture
def db() -> Mock:
    d = Mock()
    d.add_action_item = Mock()
    d.update_thread = Mock()
    return d


def test_integrate_refuses_plugin_install_dir_binding(db, tmp_path, monkeypatch):
    """Autopilot topic mis-bound to the ~/.claude plugin install dir is refused
    before any git side effect (Check 1 — the exact incident binding)."""
    # Re-root HOME so ~/.claude is a controlled tmp dir.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    wrong = _git_repo(fake_home / ".claude", juggle_source=False)  # under ~/.claude

    canonical = _git_repo(tmp_path / "github-juggle", juggle_source=True)
    monkeypatch.setattr(ws, "canonical_repo_path", lambda start=None: canonical)
    monkeypatch.setattr(jci, "_graph_task_for_thread", lambda db_, tid: {"id": "T1"})

    wt = str(tmp_path / "wt")
    subprocess.run(["git", "-C", wrong, "worktree", "add", "-q", "-b", "cyc_AA", wt],
                   check=True, capture_output=True)
    thread = {"id": "t-1", "worktree_path": wt,
              "worktree_branch": "cyc_AA", "main_repo_path": wrong}

    ok, msg = jci._run_integrate(thread, db)

    assert ok is False
    assert ".claude" in msg and "mis-bound" in msg
    db.add_action_item.assert_called_once()


def test_integrate_refuses_juggle_worktree_copy_binding(db, tmp_path, monkeypatch):
    """Autopilot topic bound to a DIFFERENT juggle checkout (not canonical) is
    refused (Check 2 — worktree-copy mis-binding)."""
    canonical = _git_repo(tmp_path / "github-juggle", juggle_source=True)
    other = _git_repo(tmp_path / "stray-juggle", juggle_source=True)
    monkeypatch.setattr(ws, "canonical_repo_path", lambda start=None: canonical)

    err = jci._assert_source_binding(other, is_autopilot=True)
    assert err is not None and "canonical" in err


def test_integrate_allows_correctly_bound_autopilot_topic(db, tmp_path, monkeypatch):
    """Guard is a no-op when main_repo_path IS the canonical source repo."""
    canonical = _git_repo(tmp_path / "github-juggle", juggle_source=True)
    monkeypatch.setattr(ws, "canonical_repo_path", lambda start=None: canonical)

    assert jci._assert_source_binding(canonical, is_autopilot=True) is None


def test_guard_noop_for_external_project_autopilot(db, tmp_path, monkeypatch):
    """An external-project autopilot (non-juggle source repo, not under ~/.claude)
    is NOT blocked — guard must not false-positive on legitimate other projects."""
    canonical = _git_repo(tmp_path / "github-juggle", juggle_source=True)
    external = _git_repo(tmp_path / "some-other-project", juggle_source=False)
    monkeypatch.setattr(ws, "canonical_repo_path", lambda start=None: canonical)

    assert jci._assert_source_binding(external, is_autopilot=True) is None


def test_guard_noop_for_plain_thread(db, tmp_path, monkeypatch):
    """Non-autopilot (plain) threads are never blocked, even on a different repo."""
    canonical = _git_repo(tmp_path / "github-juggle", juggle_source=True)
    wrong = _git_repo(tmp_path / "other", juggle_source=False)
    monkeypatch.setattr(ws, "canonical_repo_path", lambda start=None: canonical)

    assert jci._assert_source_binding(wrong, is_autopilot=False) is None
