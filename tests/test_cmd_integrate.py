"""Tests for the `juggle integrate` CLI wrapper (cmd_integrate) and the
vcs-route-integrate exit criterion: zero raw git subprocess calls remain in
juggle_cmd_integrate.py — the A3 pipeline routes entirely through
backend_for(repo).{dirty_files,refresh,trunk,has_changes,update_to,
describe_changes,submit,remove_workspace}.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

REPO_ROOT = Path(__file__).parent.parent
SRC_DIR = str(REPO_ROOT / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=check
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@t.t")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("one\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "first")
    return r


def _make_worktree(repo, tmp_path, label):
    wt = str(tmp_path / f"wt-{label}")
    _git(repo, "worktree", "add", "-b", f"cyc_{label}", wt)
    return wt


def _make_db():
    db = Mock()
    db.update_thread = Mock()
    db.add_action_item = Mock()
    return db


def test_cmd_integrate_invokes_run_integrate_on_success(repo, tmp_path):
    from juggle_cmd_integrate import cmd_integrate

    wt = _make_worktree(repo, tmp_path, "AB")
    (Path(wt) / "feat.py").write_text("y = 2\n")
    _git(Path(wt), "add", "feat.py")
    _git(Path(wt), "commit", "-q", "-m", "add feat.py")

    thread = {"id": "thread-uuid-1", "user_label": "AB",
              "worktree_path": wt, "worktree_branch": "cyc_AB",
              "main_repo_path": str(repo)}
    db = _make_db()
    db.get_thread.return_value = thread

    args = Mock()
    args.thread_id = "AB"
    args.allow_main = False

    with patch("juggle_cmd_integrate.get_db", return_value=db):
        with patch("juggle_cmd_integrate._resolve_thread", return_value="thread-uuid-1"):
            with patch("juggle_cmd_integrate.get_repo_config",
                       return_value={"push_mode": "none", "test_cmd": ""}):
                with patch("juggle_integrate_lock._get_lock_path",
                           return_value=tmp_path / "t.lock"):
                    with patch("juggle_cmd_integrate._restart_juggle_daemons"):
                        cmd_integrate(args)  # should not raise SystemExit


def test_cmd_integrate_exits_nonzero_on_failure(tmp_path):
    from juggle_cmd_integrate import cmd_integrate

    thread = {"id": "thread-uuid-1", "user_label": "AB",
              "worktree_path": str(tmp_path / "gone"),
              "worktree_branch": "cyc_AB",
              "main_repo_path": str(tmp_path / "norepo")}
    db = _make_db()
    db.get_thread.return_value = thread

    args = Mock()
    args.thread_id = "AB"
    args.allow_main = False

    with patch("juggle_cmd_integrate.get_db", return_value=db):
        with patch("juggle_cmd_integrate._resolve_thread", return_value="thread-uuid-1"):
            with patch("juggle_integrate_lock._get_lock_path",
                       return_value=tmp_path / "t.lock"):
                with pytest.raises(SystemExit) as exc:
                    cmd_integrate(args)
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# Exit criterion: zero raw git subprocess calls remain in the integrate module
# ---------------------------------------------------------------------------

_RAW_GIT_CALL = re.compile(r'subprocess\.(run|check_output)\(\s*\[\s*["\']git["\']')


def test_no_raw_git_subprocess_in_integrate_module():
    f = REPO_ROOT / "src" / "juggle_cmd_integrate.py"
    offenders = [
        f"{i}: {line.strip()}"
        for i, line in enumerate(f.read_text().splitlines(), 1)
        if _RAW_GIT_CALL.search(line)
    ]
    assert not offenders, "raw git subprocess call still present:\n" + "\n".join(offenders)


def test_run_integrate_uses_only_seam_methods_no_bare_git_string(repo, tmp_path):
    """The pipeline never assembles a bare ``["git", ...]`` list itself — every
    git mechanic crosses through backend_for(repo)."""
    import inspect
    import juggle_cmd_integrate as jci

    src = inspect.getsource(jci._run_integrate)
    assert '"git"' not in src and "'git'" not in src
