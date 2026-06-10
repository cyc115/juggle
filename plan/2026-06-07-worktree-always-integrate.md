# Worktree-Always + Integrate Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make isolated git worktrees the enforced default for all coder/planner agents, and add `juggle integrate <thread>` — an atomic, serialized command that replaces the bare ff-merge in `_finalize_worktree` with fetch → rebase → test → ff-merge → push.

**Architecture:** `cmd_send_task` auto-creates `/tmp/juggle-<repo>-<label>` worktrees on `cyc_<label>` branches when `role ∈ {coder, planner}` and `repo_path` is known; a hard guard refuses main-worktree dispatch without `--allow-main`. A new `juggle_cmd_integrate.py` holds per-repo file-locking, the full `_run_integrate()` pipeline, and the CLI entry point. `cmd_complete_agent` is refactored to route through `_run_integrate` instead of the bare ff-merge.

**Tech Stack:** Python 3.11+, `subprocess`, `pathlib`, `os.kill` for PID liveness, `symlink` for `.venv` sharing, file-rename atomicity for lock acquisition.

**Target version:** 1.50.0 (minor bump — new feature)

---

## Scope Boundaries

- **Semantic line-conflicts are NOT auto-resolved.** Rebase conflict → fail-closed (action_item lists conflicting files). Automatic ordering of concurrent threads is a future `depends_on`-DAG backlog item.
- **`pr` push mode** pushes only the feature branch to origin; it does NOT open a GitHub PR. That is left to the agent or operator.
- **Duplicate watchdog+monitor processes** (pre-existing singleton-hygiene bug) are flagged in risks but not fixed here. The self-repo restart calls `_start_watchdog()` which already does a global sweep.

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/juggle_settings.py` | MODIFY | `"repos": {}` in DEFAULTS + `get_repo_config()` helper |
| `src/juggle_cmd_agents.py` | MODIFY | `_create_worktree()`; auto-create+guard in `cmd_send_task`; route `cmd_complete_agent` through integrate |
| `src/juggle_cmd_integrate.py` | CREATE | Lock helpers, `_run_integrate()`, `_restart_juggle_daemons()`, `cmd_integrate()` |
| `src/juggle_cli.py` | MODIFY | `integrate` subparser + `--allow-main` on `send-task` |
| `.claude-plugin/plugin.json` | MODIFY | 1.49.0 → 1.50.0 |
| `tests/test_worktree_setup.py` | CREATE | `_create_worktree` + auto-create + guard tests |
| `tests/test_integrate.py` | CREATE | Lock + `_run_integrate` scenario tests |

---

## Devil's Advocate — Resolved Findings (now acceptance criteria)

| Finding | Resolution baked into plan |
|---------|--------------------------|
| Semantic line-conflicts | Fail-closed → action_item with file list; scope boundary stated |
| CLI-swap under orchestrator | Orchestrator is fresh `uv run` per call (safe). Watchdog + monitor hold stale code → explicit post-step: after ff-merge of juggle's own repo, restart both via `_start_watchdog()` + `_maybe_start_talkback()`. Gated on `target==self`. Note: pre-existing duplicate-daemon bug unrelated to this feature. |
| Lock staleness / partial rebase | PID liveness + age timeout for stale reclaim. In-progress rebase aborted on entry (`rebase-merge`/`rebase-apply` check). Idempotent: branch with 0 commits ahead → skip straight to cleanup. |
| FF-merge fails when main moved | Replaced by rebase-then-ff-merge. Rebase conflict → fail-closed (never merges broken state). |
| Migration back-compat | `repos: {}` is an optional config.json key; unconfigured repos default to `push_mode=none`, `test_cmd=""`. Existing `--worktree-*` flags on `send-task` preserved and now actually persist to thread. |

---

## Task 1: Repo config schema in settings

**Files:**
- Modify: `src/juggle_settings.py`
- Create: `tests/test_integrate.py` (bootstraps the file with fixtures + settings tests)

- [ ] **Step 1.1: Write failing tests**

Create `tests/test_integrate.py`:

```python
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest


# ── Shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def git_repo(tmp_path):
    """Local git repo with one commit on branch 'main'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in [
        ["git", "init", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
        ["git", "-C", str(repo), "config", "user.name", "T"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True, capture_output=True)
    return str(repo)


@pytest.fixture
def git_repo_with_remote(tmp_path):
    """Bare remote + local clone on branch 'main', remote tracking set up."""
    remote = tmp_path / "remote.git"
    local = tmp_path / "local"
    local.mkdir()
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", str(local)], check=True, capture_output=True)
    for cmd in [
        ["git", "-C", str(local), "config", "user.email", "t@t.com"],
        ["git", "-C", str(local), "config", "user.name", "T"],
        ["git", "-C", str(local), "remote", "add", "origin", str(remote)],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)
    (local / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(local), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(local), "branch", "-M", "main"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(local), "push", "-u", "origin", "main"],
        check=True, capture_output=True,
    )
    return str(local), str(remote)


# ── Helper used by multiple test tasks ───────────────────────────────────────

def _add_commit(repo_path: str, filename: str, content: str, message: str) -> None:
    (Path(repo_path) / filename).write_text(content)
    subprocess.run(["git", "-C", repo_path, "add", filename], check=True, capture_output=True)
    subprocess.run(["git", "-C", repo_path, "commit", "-m", message], check=True, capture_output=True)


def _make_worktree(repo_path: str, worktree_root: str, label: str) -> str:
    """Create linked worktree on cyc_<label>. Returns worktree path."""
    wt = str(Path(worktree_root) / f"wt-{label}")
    subprocess.run(
        ["git", "-C", repo_path, "worktree", "add", "-b", f"cyc_{label}", wt],
        check=True, capture_output=True,
    )
    for cmd in [
        ["git", "-C", wt, "config", "user.email", "t@t.com"],
        ["git", "-C", wt, "config", "user.name", "T"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)
    return wt


def _make_db() -> Mock:
    db = Mock()
    db.update_thread = Mock()
    db.add_action_item = Mock()
    return db


# ── Settings tests ────────────────────────────────────────────────────────────

def test_get_repo_config_defaults_for_unknown_repo():
    from juggle_settings import get_repo_config
    with patch("juggle_settings.get_settings", return_value={"repos": {}}):
        cfg = get_repo_config("/unknown/repo")
    assert cfg["push_mode"] == "none"
    assert cfg["test_cmd"] == ""


def test_get_repo_config_reads_configured_repo():
    from juggle_settings import get_repo_config
    repos = {"/my/repo": {"push_mode": "direct", "test_cmd": "pytest -x"}}
    with patch("juggle_settings.get_settings", return_value={"repos": repos}):
        cfg = get_repo_config("/my/repo")
    assert cfg["push_mode"] == "direct"
    assert cfg["test_cmd"] == "pytest -x"


def test_get_repo_config_partial_override_falls_back():
    from juggle_settings import get_repo_config
    repos = {"/my/repo": {"push_mode": "pr"}}
    with patch("juggle_settings.get_settings", return_value={"repos": repos}):
        cfg = get_repo_config("/my/repo")
    assert cfg["push_mode"] == "pr"
    assert cfg["test_cmd"] == ""
```

- [ ] **Step 1.2: Run tests to verify they fail**

```bash
cd ~/github/juggle && uv run pytest tests/test_integrate.py::test_get_repo_config_defaults_for_unknown_repo tests/test_integrate.py::test_get_repo_config_reads_configured_repo tests/test_integrate.py::test_get_repo_config_partial_override_falls_back -v
```

Expected: `ImportError: cannot import name 'get_repo_config' from 'juggle_settings'`

- [ ] **Step 1.3: Add `repos` to DEFAULTS and add `get_repo_config()`**

In `src/juggle_settings.py`, add to the `DEFAULTS` dict (near the top-level keys, after `"summary_max_chars"` for example):

```python
    # Per-repo integration config. Key = absolute repo path.
    # Example: {"/home/user/juggle": {"push_mode": "direct", "test_cmd": "pytest"}}
    # push_mode: "direct" = ff-merge+push main | "pr" = push branch only | "none" = local merge only
    "repos": {},
```

At the bottom of the file, after `get_nested`, add:

```python
def get_repo_config(repo_path: str) -> dict:
    """Return integration config for repo_path with safe defaults.

    Unknown repos get push_mode='none' and test_cmd='' — intentionally safe.
    """
    repos = get_settings().get("repos", {})
    cfg = repos.get(str(repo_path), {})
    return {
        "push_mode": cfg.get("push_mode", "none"),
        "test_cmd": cfg.get("test_cmd", ""),
    }
```

- [ ] **Step 1.4: Run tests to verify pass**

```bash
cd ~/github/juggle && uv run pytest tests/test_integrate.py::test_get_repo_config_defaults_for_unknown_repo tests/test_integrate.py::test_get_repo_config_reads_configured_repo tests/test_integrate.py::test_get_repo_config_partial_override_falls_back -v
```

Expected: 3 passed

- [ ] **Step 1.5: Commit**

```bash
cd ~/github/juggle && git add src/juggle_settings.py tests/test_integrate.py
git commit -m "feat: add repos config schema and get_repo_config() to settings"
```

---

## Task 2: `_create_worktree` helper

**Files:**
- Modify: `src/juggle_cmd_agents.py`
- Create: `tests/test_worktree_setup.py`

- [ ] **Step 2.1: Write failing tests**

Create `tests/test_worktree_setup.py`:

```python
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    for cmd in [
        ["git", "init", str(repo)],
        ["git", "-C", str(repo), "config", "user.email", "t@t.com"],
        ["git", "-C", str(repo), "config", "user.name", "T"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "branch", "-M", "main"], check=True, capture_output=True)
    return str(repo)


# ── _create_worktree ──────────────────────────────────────────────────────────

def test_create_worktree_creates_linked_worktree(git_repo, tmp_path):
    from juggle_cmd_agents import _create_worktree
    ok, wt_path, branch, msg = _create_worktree(git_repo, "AB", worktree_root=str(tmp_path))
    assert ok, msg
    assert Path(wt_path).is_dir()
    assert branch == "cyc_AB"
    wt_list = subprocess.run(
        ["git", "-C", git_repo, "worktree", "list", "--porcelain"],
        capture_output=True, text=True,
    ).stdout
    assert wt_path in wt_list


def test_create_worktree_branch_starts_from_repo_head(git_repo, tmp_path):
    from juggle_cmd_agents import _create_worktree
    repo_head = subprocess.run(
        ["git", "-C", git_repo, "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    ok, wt_path, branch, _ = _create_worktree(git_repo, "AB", worktree_root=str(tmp_path))
    assert ok
    wt_head = subprocess.run(
        ["git", "-C", wt_path, "rev-parse", "HEAD"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert wt_head == repo_head


def test_create_worktree_symlinks_venv(git_repo, tmp_path):
    from juggle_cmd_agents import _create_worktree
    (Path(git_repo) / ".venv").mkdir()
    ok, wt_path, branch, _ = _create_worktree(git_repo, "CD", worktree_root=str(tmp_path))
    assert ok
    venv_link = Path(wt_path) / ".venv"
    assert venv_link.is_symlink()
    assert venv_link.resolve() == (Path(git_repo) / ".venv").resolve()


def test_create_worktree_no_venv_skips_silently(git_repo, tmp_path):
    from juggle_cmd_agents import _create_worktree
    assert not (Path(git_repo) / ".venv").exists()
    ok, wt_path, branch, _ = _create_worktree(git_repo, "EF", worktree_root=str(tmp_path))
    assert ok
    assert not (Path(wt_path) / ".venv").exists()


def test_create_worktree_idempotent(git_repo, tmp_path):
    from juggle_cmd_agents import _create_worktree
    _create_worktree(git_repo, "GH", worktree_root=str(tmp_path))
    ok, wt_path, branch, msg = _create_worktree(git_repo, "GH", worktree_root=str(tmp_path))
    assert ok
    assert "already exists" in msg
```

- [ ] **Step 2.2: Run to verify fail**

```bash
cd ~/github/juggle && uv run pytest tests/test_worktree_setup.py -k "create_worktree" -v
```

Expected: `ImportError: cannot import name '_create_worktree' from 'juggle_cmd_agents'`

- [ ] **Step 2.3: Add `_create_worktree` to `juggle_cmd_agents.py`**

Insert immediately after `_finalize_worktree` (after line 133, the `return True, f"Worktree {worktree_path} finalized..."` line):

```python
def _create_worktree(
    repo_path: str, thread_label: str, worktree_root: str = "/tmp"
) -> tuple[bool, str, str, str]:
    """Create an isolated git worktree for a thread.

    Returns (success, worktree_path, branch, message).
    worktree_path and branch are empty strings on failure.
    Idempotent: if worktree_path already exists, returns (True, path, branch, "already exists").
    """
    basename = Path(repo_path).name
    worktree_path = str(Path(worktree_root) / f"juggle-{basename}-{thread_label}")
    branch = f"cyc_{thread_label}"

    if Path(worktree_path).exists():
        return True, worktree_path, branch, f"Worktree already exists: {worktree_path}"

    result = subprocess.run(
        ["git", "-C", repo_path, "worktree", "add", "-b", branch, worktree_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False, "", "", f"git worktree add failed: {result.stderr.strip()}"

    # Symlink .venv for immediate test runs — skip silently when absent
    main_venv = Path(repo_path) / ".venv"
    worktree_venv = Path(worktree_path) / ".venv"
    if main_venv.exists() and not worktree_venv.exists():
        try:
            worktree_venv.symlink_to(main_venv)
        except OSError:
            pass

    return True, worktree_path, branch, f"Worktree created: {worktree_path} on branch {branch}"
```

- [ ] **Step 2.4: Run tests to verify pass**

```bash
cd ~/github/juggle && uv run pytest tests/test_worktree_setup.py -k "create_worktree" -v
```

Expected: 5 passed

- [ ] **Step 2.5: Commit**

```bash
cd ~/github/juggle && git add src/juggle_cmd_agents.py tests/test_worktree_setup.py
git commit -m "feat: add _create_worktree helper with .venv symlink and idempotent create"
```

---

## Task 3: Per-repo lock helpers

**Files:**
- Create: `src/juggle_cmd_integrate.py` (lock section only)
- Modify: `tests/test_integrate.py` (append lock tests)

- [ ] **Step 3.1: Append failing lock tests to `tests/test_integrate.py`**

```python
# ── Lock tests ────────────────────────────────────────────────────────────────

def test_acquire_lock_creates_pidfile_owned_by_current_process(tmp_path):
    from juggle_cmd_integrate import acquire_repo_lock, release_repo_lock
    with patch("juggle_cmd_integrate._get_lock_path", return_value=tmp_path / "t.lock"):
        lp = acquire_repo_lock("/repo", timeout_secs=5)
    assert lp.exists()
    pid = int(lp.read_text().strip().splitlines()[0])
    assert pid == os.getpid()
    release_repo_lock(lp)
    assert not lp.exists()


def test_acquire_lock_steals_dead_pid(tmp_path):
    from juggle_cmd_integrate import acquire_repo_lock, release_repo_lock
    lock_file = tmp_path / "dead.lock"
    lock_file.write_text("99999999\n0.0\n")  # nonexistent PID, epoch timestamp
    with patch("juggle_cmd_integrate._get_lock_path", return_value=lock_file):
        lp = acquire_repo_lock("/repo", timeout_secs=5)
    pid = int(lp.read_text().strip().splitlines()[0])
    assert pid == os.getpid()
    release_repo_lock(lp)


def test_acquire_lock_times_out_on_alive_pid(tmp_path):
    from juggle_cmd_integrate import acquire_repo_lock
    lock_file = tmp_path / "alive.lock"
    # PID 1 (init/launchd) is always alive; recent timestamp so not aged-out
    lock_file.write_text(f"1\n{time.time()}\n")
    with patch("juggle_cmd_integrate._get_lock_path", return_value=lock_file):
        with pytest.raises(RuntimeError, match="Cannot acquire lock"):
            acquire_repo_lock("/repo", timeout_secs=0.3)


def test_acquire_lock_steals_aged_out_alive_pid(tmp_path):
    from juggle_cmd_integrate import acquire_repo_lock, release_repo_lock
    lock_file = tmp_path / "old.lock"
    # PID 1 alive but timestamp is 400s ago — older than 300s default
    lock_file.write_text(f"1\n{time.time() - 400}\n")
    with patch("juggle_cmd_integrate._get_lock_path", return_value=lock_file):
        lp = acquire_repo_lock("/repo", timeout_secs=300)
    assert lp.exists()
    release_repo_lock(lp)


def test_release_lock_noop_when_not_owner(tmp_path):
    from juggle_cmd_integrate import release_repo_lock
    lock_file = tmp_path / "other.lock"
    lock_file.write_text(f"1\n{time.time()}\n")  # owned by PID 1
    release_repo_lock(lock_file)
    assert lock_file.exists()  # not removed
```

- [ ] **Step 3.2: Run to verify fail**

```bash
cd ~/github/juggle && uv run pytest tests/test_integrate.py -k "lock" -v
```

Expected: `ModuleNotFoundError: No module named 'juggle_cmd_integrate'`

- [ ] **Step 3.3: Create `src/juggle_cmd_integrate.py` with lock helpers**

```python
#!/usr/bin/env python3
"""Juggle — integrate command: rebase-aware atomic worktree finalization."""

import os
import subprocess
import sys
import time
from pathlib import Path


# ── Lock helpers ──────────────────────────────────────────────────────────────

def _get_lock_path(repo_path: str) -> Path:
    from juggle_settings import get_settings
    config_dir = Path(get_settings()["paths"]["config_dir"]).expanduser()
    locks_dir = config_dir / "locks"
    locks_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(repo_path).name.replace(" ", "_")
    return locks_dir / f"{safe_name}.lock"


def _read_lock(lock_path: Path) -> tuple[int, float]:
    """Return (pid, timestamp) from lock file; (0, 0.0) on any parse error."""
    try:
        parts = lock_path.read_text().strip().splitlines()
        return int(parts[0]), float(parts[1])
    except (OSError, ValueError, IndexError):
        return 0, 0.0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def acquire_repo_lock(repo_path: str, timeout_secs: float = 300.0) -> Path:
    """Acquire a per-repo file lock. Returns the lock path.

    Steals locks with a dead PID or age > timeout_secs.
    Raises RuntimeError if a live lock cannot be acquired within timeout_secs.
    Uses atomic rename to avoid races between concurrent integrations.
    """
    lock_path = _get_lock_path(repo_path)
    deadline = time.monotonic() + timeout_secs

    while True:
        if lock_path.exists():
            existing_pid, lock_ts = _read_lock(lock_path)
            lock_age = time.time() - lock_ts
            if not _pid_alive(existing_pid) or lock_age > timeout_secs:
                lock_path.unlink(missing_ok=True)
            elif time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Cannot acquire lock for {repo_path}: "
                    f"held by PID {existing_pid} for {lock_age:.0f}s"
                )
            else:
                time.sleep(0.5)
                continue

        # Atomic write: write temp then rename
        tmp = lock_path.with_suffix(".lock.tmp")
        tmp.write_text(f"{os.getpid()}\n{time.time()}\n")
        try:
            tmp.rename(lock_path)
        except OSError:
            tmp.unlink(missing_ok=True)
            continue  # Race lost — retry

        # Verify we won (another writer could have clobbered via rename race)
        pid, _ = _read_lock(lock_path)
        if pid == os.getpid():
            return lock_path


def release_repo_lock(lock_path: Path) -> None:
    """Remove the lock only if owned by the current process."""
    if not lock_path or not lock_path.exists():
        return
    pid, _ = _read_lock(lock_path)
    if pid == os.getpid():
        lock_path.unlink(missing_ok=True)
```

- [ ] **Step 3.4: Run lock tests to verify pass**

```bash
cd ~/github/juggle && uv run pytest tests/test_integrate.py -k "lock" -v
```

Expected: 5 passed

- [ ] **Step 3.5: Commit**

```bash
cd ~/github/juggle && git add src/juggle_cmd_integrate.py tests/test_integrate.py
git commit -m "feat: add per-repo integration lock with pid-liveness and stale-steal"
```

---

## Task 4: `_run_integrate` core

**Files:**
- Modify: `src/juggle_cmd_integrate.py` (append `_restart_juggle_daemons` + `_run_integrate`)
- Modify: `tests/test_integrate.py` (append integrate scenario tests)

- [ ] **Step 4.1: Append failing integrate scenario tests to `tests/test_integrate.py`**

```python
# ── _run_integrate tests ──────────────────────────────────────────────────────

def test_integrate_happy_path_none_mode(git_repo, tmp_path):
    """rebase + ff-merge + no push; worktree + branch removed after."""
    from juggle_cmd_integrate import _run_integrate

    wt = _make_worktree(git_repo, str(tmp_path), "AB")
    _add_commit(wt, "feat.py", "y = 2\n", "feat: add feature")

    thread = {"id": "t-1", "worktree_path": wt,
               "worktree_branch": "cyc_AB", "main_repo_path": git_repo}
    db = _make_db()

    with patch("juggle_cmd_integrate.get_repo_config", return_value={"push_mode": "none", "test_cmd": ""}):
        with patch("juggle_cmd_integrate._get_lock_path", return_value=tmp_path / "t.lock"):
            with patch("juggle_cmd_integrate._restart_juggle_daemons"):
                ok, msg = _run_integrate(thread, db)

    assert ok, msg
    assert (Path(git_repo) / "feat.py").exists()      # commit merged into main
    assert not Path(wt).exists()                        # worktree removed
    branches = subprocess.run(
        ["git", "-C", git_repo, "branch"], capture_output=True, text=True
    ).stdout
    assert "cyc_AB" not in branches                    # branch deleted
    db.update_thread.assert_called()


def test_integrate_happy_path_direct_mode(git_repo_with_remote, tmp_path):
    """rebase + ff-merge + git push; commit visible in remote after."""
    from juggle_cmd_integrate import _run_integrate

    local, remote = git_repo_with_remote
    wt = _make_worktree(local, str(tmp_path), "AB")
    _add_commit(wt, "feat.py", "y = 2\n", "feat: add feature")

    thread = {"id": "t-1", "worktree_path": wt,
               "worktree_branch": "cyc_AB", "main_repo_path": local}
    db = _make_db()

    with patch("juggle_cmd_integrate.get_repo_config", return_value={"push_mode": "direct", "test_cmd": ""}):
        with patch("juggle_cmd_integrate._get_lock_path", return_value=tmp_path / "t.lock"):
            with patch("juggle_cmd_integrate._restart_juggle_daemons"):
                ok, msg = _run_integrate(thread, db)

    assert ok, msg
    remote_log = subprocess.run(
        ["git", "-C", remote, "log", "--oneline", "-1"],
        capture_output=True, text=True,
    ).stdout
    assert "feat: add feature" in remote_log


def test_integrate_happy_path_pr_mode(git_repo_with_remote, tmp_path):
    """pr mode: branch pushed to origin, local main NOT advanced."""
    from juggle_cmd_integrate import _run_integrate

    local, remote = git_repo_with_remote
    wt = _make_worktree(local, str(tmp_path), "AB")
    _add_commit(wt, "feat.py", "y = 2\n", "feat: add feature")

    main_head_before = subprocess.run(
        ["git", "-C", local, "rev-parse", "main"],
        capture_output=True, text=True,
    ).stdout.strip()

    thread = {"id": "t-1", "worktree_path": wt,
               "worktree_branch": "cyc_AB", "main_repo_path": local}
    db = _make_db()

    with patch("juggle_cmd_integrate.get_repo_config", return_value={"push_mode": "pr", "test_cmd": ""}):
        with patch("juggle_cmd_integrate._get_lock_path", return_value=tmp_path / "t.lock"):
            with patch("juggle_cmd_integrate._restart_juggle_daemons"):
                ok, msg = _run_integrate(thread, db)

    assert ok, msg
    # Local main NOT advanced (no ff-merge for pr mode)
    main_head_after = subprocess.run(
        ["git", "-C", local, "rev-parse", "main"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert main_head_after == main_head_before
    # Branch pushed to remote
    remote_branches = subprocess.run(
        ["git", "-C", remote, "branch"], capture_output=True, text=True
    ).stdout
    assert "cyc_AB" in remote_branches


def test_integrate_rebase_conflict_aborts_files_action_item(git_repo, tmp_path):
    """Rebase conflict → rebase --abort, branch kept, action_item with file list."""
    from juggle_cmd_integrate import _run_integrate

    wt = _make_worktree(git_repo, str(tmp_path), "CD")
    _add_commit(wt, "conflict.py", "branch version\n", "branch: edit conflict.py")
    # Advance main with conflicting change to same file after branch diverged
    _add_commit(git_repo, "conflict.py", "main version\n", "main: edit conflict.py")

    thread = {"id": "t-1", "worktree_path": wt,
               "worktree_branch": "cyc_CD", "main_repo_path": git_repo}
    db = _make_db()

    with patch("juggle_cmd_integrate.get_repo_config", return_value={"push_mode": "none", "test_cmd": ""}):
        with patch("juggle_cmd_integrate._get_lock_path", return_value=tmp_path / "t.lock"):
            ok, msg = _run_integrate(thread, db)

    assert not ok
    assert "conflict.py" in msg                    # file listed in failure message
    assert Path(wt).is_dir()                        # worktree preserved
    branches = subprocess.run(
        ["git", "-C", git_repo, "branch"], capture_output=True, text=True
    ).stdout
    assert "cyc_CD" in branches                    # branch preserved
    db.add_action_item.assert_called_once()
    ai_msg = db.add_action_item.call_args[1]["message"]
    assert "conflict.py" in ai_msg


def test_integrate_red_tests_prevents_merge(git_repo, tmp_path):
    """test_cmd exits nonzero → no ff-merge performed, action_item filed."""
    from juggle_cmd_integrate import _run_integrate

    wt = _make_worktree(git_repo, str(tmp_path), "EF")
    _add_commit(wt, "new.py", "z = 3\n", "add new.py")

    thread = {"id": "t-1", "worktree_path": wt,
               "worktree_branch": "cyc_EF", "main_repo_path": git_repo}
    db = _make_db()

    with patch("juggle_cmd_integrate.get_repo_config", return_value={"push_mode": "direct", "test_cmd": "exit 1"}):
        with patch("juggle_cmd_integrate._get_lock_path", return_value=tmp_path / "t.lock"):
            ok, msg = _run_integrate(thread, db)

    assert not ok
    assert not (Path(git_repo) / "new.py").exists()   # NOT merged
    db.add_action_item.assert_called_once()


def test_integrate_already_merged_skips_straight_to_cleanup(git_repo, tmp_path):
    """Branch with 0 commits ahead of main → skip rebase/merge, clean up."""
    from juggle_cmd_integrate import _run_integrate

    # Worktree on branch that has no extra commits (== main HEAD)
    wt = _make_worktree(git_repo, str(tmp_path), "GH")

    thread = {"id": "t-1", "worktree_path": wt,
               "worktree_branch": "cyc_GH", "main_repo_path": git_repo}
    db = _make_db()

    with patch("juggle_cmd_integrate.get_repo_config", return_value={"push_mode": "none", "test_cmd": ""}):
        with patch("juggle_cmd_integrate._get_lock_path", return_value=tmp_path / "t.lock"):
            with patch("juggle_cmd_integrate._restart_juggle_daemons"):
                ok, msg = _run_integrate(thread, db)

    assert ok, msg
    assert "already merged" in msg.lower() or "no commits ahead" in msg.lower()
    assert not Path(wt).exists()   # worktree cleaned up


def test_integrate_idempotent_missing_worktree_returns_error(git_repo, tmp_path):
    """If worktree path doesn't exist, integrate returns failure gracefully."""
    from juggle_cmd_integrate import _run_integrate

    thread = {"id": "t-1",
               "worktree_path": str(tmp_path / "nonexistent"),
               "worktree_branch": "cyc_ZZ",
               "main_repo_path": git_repo}
    db = _make_db()

    with patch("juggle_cmd_integrate._get_lock_path", return_value=tmp_path / "t.lock"):
        ok, msg = _run_integrate(thread, db)

    assert not ok
    assert "does not exist" in msg.lower() or "nonexistent" in msg.lower()
```

- [ ] **Step 4.2: Run to verify fail**

```bash
cd ~/github/juggle && uv run pytest tests/test_integrate.py -k "integrate" -v 2>&1 | tail -15
```

Expected: `ImportError: cannot import name '_run_integrate' from 'juggle_cmd_integrate'`

- [ ] **Step 4.3: Append `_restart_juggle_daemons` and `_run_integrate` to `juggle_cmd_integrate.py`**

```python
# ── Self-repo daemon restart ───────────────────────────────────────────────────

def _restart_juggle_daemons() -> None:
    """Restart watchdog + talkback after a ff-merge of juggle's own repo.

    The watchdog and monitor are long-running processes that hold stale Python
    bytecode after a juggle self-update.  _start_watchdog() does a global sweep
    before spawning, so calling it here is safe.

    Risk: duplicate watchdog+monitor processes are a pre-existing singleton-
    hygiene bug (unrelated to this feature) — _start_watchdog() mitigates it
    incidentally via its pkill sweep but that bug is not fully fixed here.
    """
    try:
        from juggle_cmd_threads import _start_watchdog, _maybe_start_talkback
        _start_watchdog()
        _maybe_start_talkback()
    except Exception as e:
        print(
            f"[juggle] WARNING: daemon restart after self-integrate failed: {e}",
            file=sys.stderr,
        )


# ── Core integration pipeline ─────────────────────────────────────────────────

def _run_integrate(thread: dict, db, allow_main: bool = False) -> tuple[bool, str]:
    """Atomic fetch → rebase → test → ff-merge → push → cleanup for a worktree.

    Fail-closed: rebase conflict or test failure → action_item, branch+worktree preserved.
    Idempotent:
      - In-progress rebase aborted on entry before retrying.
      - Branch 0 commits ahead of main → skip merge, go straight to cleanup.
    push_mode controls post-merge: direct=push main, pr=push branch only, none=local only.
    """
    from juggle_settings import get_repo_config

    worktree_path = (thread.get("worktree_path") or "").strip()
    worktree_branch = (thread.get("worktree_branch") or "").strip()
    main_repo_path = (thread.get("main_repo_path") or "").strip()
    thread_uuid = thread.get("id", "")

    if not worktree_path or not worktree_branch or not main_repo_path:
        return False, "Missing worktree fields — nothing to integrate"

    if not Path(worktree_path).exists():
        return False, f"Worktree path does not exist: {worktree_path}"

    repo_cfg = get_repo_config(main_repo_path)
    push_mode = repo_cfg["push_mode"]
    test_cmd = repo_cfg["test_cmd"]

    try:
        lock_path = acquire_repo_lock(main_repo_path)
    except RuntimeError as e:
        return False, f"Lock acquisition failed: {e}"

    def _fail(reason: str) -> tuple[bool, str]:
        db.add_action_item(
            thread_id=thread_uuid,
            message=f"⚠️ integrate failed [{worktree_branch}]: {reason}",
            type_="manual_step",
            priority="high",
        )
        release_repo_lock(lock_path)
        return False, reason

    try:
        # ── 0. Abort any in-progress rebase (idempotency) ────────────────────
        git_dir_result = subprocess.run(
            ["git", "-C", worktree_path, "rev-parse", "--git-dir"],
            capture_output=True, text=True,
        )
        if git_dir_result.returncode == 0:
            gd = git_dir_result.stdout.strip()
            git_dir = gd if Path(gd).is_absolute() else str(Path(worktree_path) / gd)
            if Path(git_dir, "rebase-merge").exists() or Path(git_dir, "rebase-apply").exists():
                subprocess.run(
                    ["git", "-C", worktree_path, "rebase", "--abort"],
                    capture_output=True, text=True,
                )

        # ── 1. Fetch (non-fatal for repos without remotes) ───────────────────
        subprocess.run(
            ["git", "-C", main_repo_path, "fetch", "--prune"],
            capture_output=True, text=True,
        )

        # ── 2. Determine rebase target ────────────────────────────────────────
        rebase_onto = None
        for candidate in ("origin/main", "origin/master", "main", "master"):
            if subprocess.run(
                ["git", "-C", main_repo_path, "rev-parse", "--verify", candidate],
                capture_output=True, text=True,
            ).returncode == 0:
                rebase_onto = candidate
                break
        if rebase_onto is None:
            return _fail("Cannot determine main branch (no main/master ref found)")

        # ── 3. Idempotency: already merged? skip to cleanup ───────────────────
        ahead_result = subprocess.run(
            ["git", "-C", main_repo_path, "rev-list", "--count",
             f"{rebase_onto}..{worktree_branch}"],
            capture_output=True, text=True,
        )
        ahead_count = (
            int(ahead_result.stdout.strip() or "0")
            if ahead_result.returncode == 0 else 1
        )

        if ahead_count == 0:
            subprocess.run(
                ["git", "-C", main_repo_path, "worktree", "remove", "--force", worktree_path],
                capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "-C", main_repo_path, "branch", "-D", worktree_branch],
                capture_output=True, text=True,
            )
            db.update_thread(thread_uuid, worktree_path="", worktree_branch="", main_repo_path="")
            release_repo_lock(lock_path)
            return True, f"Branch {worktree_branch} already merged into {rebase_onto} — cleaned up."

        # ── 4. Rebase ─────────────────────────────────────────────────────────
        result = subprocess.run(
            ["git", "-C", worktree_path, "rebase", rebase_onto],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            conflicts_result = subprocess.run(
                ["git", "-C", worktree_path, "diff", "--name-only", "--diff-filter=U"],
                capture_output=True, text=True,
            )
            conflict_files = conflicts_result.stdout.strip() or "(see git status)"
            subprocess.run(
                ["git", "-C", worktree_path, "rebase", "--abort"],
                capture_output=True, text=True,
            )
            return _fail(
                f"Rebase conflict on {worktree_branch} onto {rebase_onto}.\n"
                f"Conflicting files:\n{conflict_files}\n"
                f"Branch preserved at {worktree_path}. "
                f"Sequence this thread after the one writing those files, "
                f"or resolve manually and re-run `juggle integrate`.\n"
                f"NOTE: semantic line-conflicts are not auto-resolved — this is expected behavior."
            )

        # ── 5. Run test_cmd (only when configured AND push_mode != none) ──────
        if test_cmd and push_mode != "none":
            result = subprocess.run(
                test_cmd, shell=True, capture_output=True, text=True, cwd=worktree_path,
            )
            if result.returncode != 0:
                return _fail(
                    f"Tests failed (exit {result.returncode}) for {worktree_branch}. "
                    f"No merge performed. "
                    f"stdout tail: {result.stdout[-300:].strip()}"
                )

        # ── 6. Resolve local main branch name ────────────────────────────────
        local_main = subprocess.run(
            ["git", "-C", main_repo_path, "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip() or "main"

        # ── 7. Merge + push (mode-dependent) ─────────────────────────────────
        if push_mode == "pr":
            # Push feature branch to origin; do NOT ff-merge local main
            push_result = subprocess.run(
                ["git", "-C", worktree_path, "push", "origin",
                 f"{worktree_branch}:{worktree_branch}", "--force-with-lease"],
                capture_output=True, text=True,
            )
            if push_result.returncode != 0:
                return _fail(f"Push branch for PR failed: {push_result.stderr.strip()}")
            # Remove worktree; leave branch ref on remote for PR
            subprocess.run(
                ["git", "-C", main_repo_path, "worktree", "remove", "--force", worktree_path],
                capture_output=True, text=True,
            )
            db.update_thread(thread_uuid, worktree_path="", worktree_branch=worktree_branch,
                             main_repo_path=main_repo_path)
            release_repo_lock(lock_path)
            return True, f"Branch {worktree_branch} pushed to origin for PR (no local merge)"

        # direct or none: ff-merge into local main
        result = subprocess.run(
            ["git", "-C", main_repo_path, "merge", "--ff-only", worktree_branch],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return _fail(f"FF-merge of {worktree_branch} failed: {result.stderr.strip()}")

        if push_mode == "direct":
            push_result = subprocess.run(
                ["git", "-C", main_repo_path, "push", "origin",
                 f"{local_main}:{local_main}"],
                capture_output=True, text=True,
            )
            if push_result.returncode != 0:
                return _fail(f"Push failed: {push_result.stderr.strip()}")

        # ── 8. Remove worktree + branch ───────────────────────────────────────
        subprocess.run(
            ["git", "-C", main_repo_path, "worktree", "remove", "--force", worktree_path],
            capture_output=True, text=True,
        )
        subprocess.run(
            ["git", "-C", main_repo_path, "branch", "-d", worktree_branch],
            capture_output=True, text=True,
        )

        # ── 9. Clear worktree fields on thread ────────────────────────────────
        db.update_thread(thread_uuid, worktree_path="", worktree_branch="", main_repo_path="")

        # ── 10. Self-repo: restart watchdog + monitor ─────────────────────────
        from juggle_cli_common import SRC_DIR as _SRC_DIR
        juggle_own_repo = str(Path(_SRC_DIR).parent.resolve())
        if Path(main_repo_path).resolve() == Path(juggle_own_repo).resolve():
            _restart_juggle_daemons()

        release_repo_lock(lock_path)
        return True, f"Integrated {worktree_branch} → {local_main} (push_mode={push_mode})"

    except Exception as e:
        return _fail(f"Unexpected error during integrate: {e}")
```

- [ ] **Step 4.4: Run integrate scenario tests to verify pass**

```bash
cd ~/github/juggle && uv run pytest tests/test_integrate.py -k "integrate" -v
```

Expected: All 7 integrate scenario tests pass.

- [ ] **Step 4.5: Commit**

```bash
cd ~/github/juggle && git add src/juggle_cmd_integrate.py tests/test_integrate.py
git commit -m "feat: add _run_integrate with rebase+test+push, fail-closed, idempotency"
```

---

## Task 5: Auto-create + guard in `cmd_send_task`

**Files:**
- Modify: `src/juggle_cmd_agents.py`
- Modify: `src/juggle_cli.py` (add `--allow-main` to `send-task` parser)
- Modify: `tests/test_worktree_setup.py` (append guard tests)

- [ ] **Step 5.1: Append failing guard + auto-create tests to `tests/test_worktree_setup.py`**

```python
# ── cmd_send_task auto-create + guard tests ───────────────────────────────────

def _minimal_send_task_args(prompt_file: str, role: str, repo_path: str,
                             allow_main: bool = False) -> object:
    args = Mock()
    args.agent_id = "aabbccdd-1234"
    args.prompt_file = prompt_file
    args.no_template = True
    args.allow_main = allow_main
    args.worktree_path = None
    args.worktree_branch = None
    args.main_repo_path = None
    return args


def _minimal_agent(role: str, repo_path: str) -> dict:
    return {
        "id": "aabbccdd-1234",
        "pane_id": "juggle:0.1",
        "role": role,
        "repo_path": repo_path,
        "assigned_thread": "thread-uuid-1",
        "model": None,
        "harness": "claude",
        "oneshot_pid": None,
    }


def _minimal_thread(worktree_path=None) -> dict:
    return {
        "id": "thread-uuid-1",
        "user_label": "AB",
        "worktree_path": worktree_path,
        "worktree_branch": None,
        "main_repo_path": None,
    }


def _run_send_task(args, agent, thread, create_worktree_result=None, git_repo_path=None):
    """Drive cmd_send_task with mocked DB and tmux/adapter."""
    import juggle_cmd_agents as _mod
    db = Mock()
    db.get_agent.return_value = agent
    db.get_thread.return_value = thread
    db.update_thread = Mock()
    db.update_agent = Mock()

    patchers = [
        patch.object(_mod, "get_db", return_value=db),
        patch.object(_mod, "JuggleTmuxManager") if hasattr(_mod, "JuggleTmuxManager") else
            patch("juggle_tmux.JuggleTmuxManager"),
    ]

    with patch("juggle_cmd_agents.get_db", return_value=db):
        with patch("juggle_cmd_agents.JuggleTmuxManager") as MockMgr:
            MockMgr.return_value.verify_pane.return_value = True
            MockMgr.return_value.send_task.return_value = "hash123"
            with patch("juggle_cmd_agents.get_adapter") as mock_adapter:
                mock_adapter.return_value.is_interactive = True
                mock_adapter.return_value.decorate_task = lambda role, p: p
                mock_adapter.return_value._cfg = {}
                with patch("juggle_cmd_agents._get_settings", return_value={
                    "agent": {"quality_gate_skill": ""},
                    "task_templates": {},
                }):
                    if create_worktree_result is not None:
                        with patch("juggle_cmd_agents._create_worktree",
                                   return_value=create_worktree_result):
                            _mod.cmd_send_task(args)
                    else:
                        _mod.cmd_send_task(args)
    return db


def test_auto_create_triggered_for_coder_with_repo(git_repo, tmp_path):
    """coder + repo_path → _create_worktree called, worktree fields persisted to thread."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("do stuff")
    args = _minimal_send_task_args(str(prompt_file), "coder", git_repo)
    agent = _minimal_agent("coder", git_repo)
    thread = _minimal_thread()

    wt_path = str(tmp_path / "juggle-repo-AB")
    fake_result = (True, wt_path, "cyc_AB", "Worktree created")

    db = _run_send_task(args, agent, thread, create_worktree_result=fake_result)

    # update_thread must be called with the new worktree fields
    calls = [str(c) for c in db.update_thread.call_args_list]
    assert any("cyc_AB" in c for c in calls), f"Expected worktree_branch in calls: {calls}"


def test_auto_create_not_triggered_for_researcher(git_repo, tmp_path):
    """researcher role is exempt from auto-create."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("research this")
    args = _minimal_send_task_args(str(prompt_file), "researcher", git_repo)
    agent = _minimal_agent("researcher", git_repo)
    thread = _minimal_thread()

    with patch("juggle_cmd_agents.get_db"):
        with patch("juggle_cmd_agents._create_worktree") as mock_create:
            with patch("juggle_cmd_agents.JuggleTmuxManager") as MockMgr:
                MockMgr.return_value.verify_pane.return_value = True
                MockMgr.return_value.send_task.return_value = "hash"
                with patch("juggle_cmd_agents.get_adapter") as mock_adapter:
                    mock_adapter.return_value.is_interactive = True
                    mock_adapter.return_value.decorate_task = lambda r, p: p
                    mock_adapter.return_value._cfg = {}
                    with patch("juggle_cmd_agents.get_db") as mock_get_db:
                        db = Mock()
                        db.get_agent.return_value = _minimal_agent("researcher", git_repo)
                        db.get_thread.return_value = _minimal_thread()
                        db.update_thread = Mock()
                        db.update_agent = Mock()
                        mock_get_db.return_value = db
                        with patch("juggle_cmd_agents._get_settings", return_value={
                            "agent": {"quality_gate_skill": ""},
                            "task_templates": {},
                        }):
                            from juggle_cmd_agents import cmd_send_task
                            cmd_send_task(args)
    mock_create.assert_not_called()


def test_guard_exits_nonzero_when_create_fails(tmp_path):
    """coder + repo_path + create fails + no --allow-main → exit(1)."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("do stuff")
    args = _minimal_send_task_args(str(prompt_file), "coder", "/fake/repo")
    agent = _minimal_agent("coder", "/fake/repo")
    thread = _minimal_thread()

    with pytest.raises(SystemExit) as exc:
        _run_send_task(args, agent, thread,
                       create_worktree_result=(False, "", "", "git error"))
    assert exc.value.code == 1


def test_allow_main_bypasses_guard(tmp_path):
    """--allow-main lets coder dispatch even when create fails."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("do stuff")
    args = _minimal_send_task_args(str(prompt_file), "coder", "/fake/repo", allow_main=True)
    agent = _minimal_agent("coder", "/fake/repo")
    thread = _minimal_thread()

    # Should NOT raise SystemExit
    _run_send_task(args, agent, thread,
                   create_worktree_result=(False, "", "", "git error"))


def test_worktree_preamble_injected_into_prompt(tmp_path):
    """When worktree is set on thread, cd preamble appears in sent prompt."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("implement feature X")
    wt_path = str(tmp_path / "juggle-repo-AB")
    args = _minimal_send_task_args(str(prompt_file), "coder", "/fake/repo")
    agent = _minimal_agent("coder", "/fake/repo")
    thread = _minimal_thread(worktree_path=wt_path)
    thread["worktree_branch"] = "cyc_AB"

    sent_prompts = []

    with patch("juggle_cmd_agents.get_db") as mock_get_db:
        db = Mock()
        db.get_agent.return_value = agent
        db.get_thread.return_value = thread
        db.update_thread = Mock()
        db.update_agent = Mock()
        mock_get_db.return_value = db
        with patch("juggle_cmd_agents.JuggleTmuxManager") as MockMgr:
            MockMgr.return_value.verify_pane.return_value = True
            MockMgr.return_value.send_task.side_effect = lambda pane, prompt, **kw: (
                sent_prompts.append(prompt) or "hash"
            )
            with patch("juggle_cmd_agents.get_adapter") as mock_adapter:
                mock_adapter.return_value.is_interactive = True
                mock_adapter.return_value.decorate_task = lambda r, p: p
                mock_adapter.return_value._cfg = {}
                with patch("juggle_cmd_agents._get_settings", return_value={
                    "agent": {"quality_gate_skill": ""},
                    "task_templates": {},
                }):
                    with patch("juggle_cmd_agents._create_worktree",
                               return_value=(True, wt_path, "cyc_AB", "exists")):
                        from juggle_cmd_agents import cmd_send_task
                        cmd_send_task(args)

    assert sent_prompts, "send_task was not called"
    assert wt_path in sent_prompts[0], "Worktree path missing from prompt"
    assert "cd" in sent_prompts[0].lower()
```

- [ ] **Step 5.2: Run to verify fail**

```bash
cd ~/github/juggle && uv run pytest tests/test_worktree_setup.py -k "auto_create or guard or preamble or allow_main or researcher" -v 2>&1 | tail -20
```

Expected: Tests fail because the guard/auto-create logic doesn't exist yet.

- [ ] **Step 5.3: Add auto-create + guard block to `cmd_send_task`**

In `src/juggle_cmd_agents.py`, find `cmd_send_task`. After the pane-verify/recreate block (after the `else: is_new = False` line, before `prompt = prompt_path.read_text()`), insert:

```python
    # ── Worktree auto-create + hard guard (coder/planner only) ───────────────
    thread_uuid_wt = agent.get("assigned_thread")
    thread_wt = db.get_thread(thread_uuid_wt) if thread_uuid_wt else None
    _worktree_context = ""

    if _role in ("coder", "planner") and thread_wt:
        thread_label_wt = (thread_wt.get("user_label") or thread_wt["id"][:6])
        repo_path_wt = (agent.get("repo_path") or "").strip()
        allow_main_wt = getattr(args, "allow_main", False)

        # Explicit CLI overrides: persist to thread and reload
        cli_wt_path = (getattr(args, "worktree_path", None) or "").strip()
        cli_wt_branch = (getattr(args, "worktree_branch", None) or "").strip()
        cli_main_repo = (getattr(args, "main_repo_path", None) or "").strip()
        if cli_wt_path:
            db.update_thread(
                thread_uuid_wt,
                worktree_path=cli_wt_path,
                worktree_branch=cli_wt_branch or thread_wt.get("worktree_branch"),
                main_repo_path=cli_main_repo or repo_path_wt,
            )
            thread_wt = db.get_thread(thread_uuid_wt)

        existing_wt = (thread_wt.get("worktree_path") or "").strip()

        if not existing_wt and repo_path_wt and not allow_main_wt:
            ok_wt, wt_path_new, branch_new, msg_wt = _create_worktree(
                repo_path_wt, thread_label_wt
            )
            if ok_wt:
                db.update_thread(
                    thread_uuid_wt,
                    worktree_path=wt_path_new,
                    worktree_branch=branch_new,
                    main_repo_path=repo_path_wt,
                )
                thread_wt = db.get_thread(thread_uuid_wt)
                existing_wt = wt_path_new
                print(f"[juggle] {msg_wt}", file=sys.stderr)
            else:
                print(f"[juggle] WARNING: worktree auto-create failed: {msg_wt}", file=sys.stderr)

        # Hard guard: refuse main-worktree dispatch for coder/planner
        if not existing_wt and repo_path_wt and not allow_main_wt:
            print(
                f"Error: Cannot dispatch {_role} task without an isolated worktree "
                f"(repo={repo_path_wt}). Worktree auto-create failed. "
                f"Use --allow-main to override (bypass is logged)."
            )
            sys.exit(1)

        if allow_main_wt and repo_path_wt:
            print(
                f"[juggle] WARNING: --allow-main used for {_role} on {repo_path_wt} "
                f"(thread {thread_label_wt}) — main-worktree guard bypassed.",
                file=sys.stderr,
            )

        # Inject worktree CWD preamble (comes after UNIVERSAL_PREAMBLE)
        if existing_wt:
            branch_label_wt = (thread_wt.get("worktree_branch") or "") if thread_wt else ""
            _worktree_context = (
                f"## Working Directory\n"
                f"This task runs in an isolated worktree. "
                f"cd into it before any git or file operations:\n"
                f"```bash\ncd {existing_wt}\n```\n"
                f"Branch: `{branch_label_wt}`\n\n---\n\n"
            )
    # ── End worktree guard ────────────────────────────────────────────────────
```

Then find the line `full_prompt = UNIVERSAL_PREAMBLE + prompt.rstrip()` and change it to:

```python
    full_prompt = UNIVERSAL_PREAMBLE + _worktree_context + prompt.rstrip()
```

- [ ] **Step 5.4: Add `--allow-main` to `send-task` parser in `juggle_cli.py`**

In `src/juggle_cli.py`, find the `p_send_task` block (around line 610). After the existing `--main-repo-path` argument and before `p_send_task.set_defaults(func=cmd_send_task)`, add:

```python
    p_send_task.add_argument(
        "--allow-main",
        action="store_true",
        dest="allow_main",
        default=False,
        help="Bypass worktree guard and allow coder/planner to run in main worktree (logged)",
    )
```

- [ ] **Step 5.5: Run guard + auto-create tests to verify pass**

```bash
cd ~/github/juggle && uv run pytest tests/test_worktree_setup.py -k "auto_create or guard or preamble or allow_main or researcher" -v
```

Expected: All 5 tests pass.

- [ ] **Step 5.6: Commit**

```bash
cd ~/github/juggle && git add src/juggle_cmd_agents.py src/juggle_cli.py tests/test_worktree_setup.py
git commit -m "feat: auto-create worktree in send-task for coder/planner; add hard guard + --allow-main"
```

---

## Task 6: Route `_finalize_worktree` through `_run_integrate`

**Files:**
- Modify: `src/juggle_cmd_agents.py` (change `cmd_complete_agent` worktree finalization)
- Modify: `tests/test_integrate.py` (append complete-agent routing test)

- [ ] **Step 6.1: Append failing test to `tests/test_integrate.py`**

```python
# ── cmd_complete_agent routing test ──────────────────────────────────────────

def test_complete_agent_routes_through_run_integrate(git_repo, tmp_path):
    """cmd_complete_agent calls _run_integrate (not bare ff-merge) when worktree fields set."""
    from juggle_cmd_agents import cmd_complete_agent

    wt = _make_worktree(git_repo, str(tmp_path), "AB")
    _add_commit(wt, "feat.py", "y = 2\n", "add feat.py")

    thread = {
        "id": "thread-uuid-1",
        "user_label": "AB",
        "worktree_path": wt,
        "worktree_branch": "cyc_AB",
        "main_repo_path": git_repo,
        "summary": "test",
        "open_questions": "[]",
        "status": "background",
    }
    agent = {
        "id": "agent-uuid-1",
        "role": "coder",
        "status": "busy",
        "busy_since": None,
        "pane_id": "juggle:0.1",
    }

    db = Mock()
    db.get_thread.return_value = thread
    db.get_agent_by_thread.return_value = agent
    db.get_all_threads.return_value = []
    db.get_open_action_items.return_value = []
    db.add_message = Mock()
    db.update_thread = Mock()
    db.set_thread_status = Mock()
    db.update_agent = Mock()
    db.add_notification_v2 = Mock()
    db.add_action_item = Mock()
    db.get_last_exchange.return_value = {"last_user": "", "last_assistant": ""}
    db.insert_agent_completion = Mock()
    db._connect = Mock().__enter__ = Mock(return_value=Mock(
        execute=Mock(return_value=Mock(fetchone=Mock(return_value=None)))
    ))

    integrate_calls = []

    def fake_run_integrate(t, d, allow_main=False):
        integrate_calls.append(t)
        return True, "integrated"

    args = Mock()
    args.thread_id = "thread-uuid-1"
    args.result_summary = "done"
    args.retain = None

    with patch("juggle_cmd_agents.get_db", return_value=db):
        with patch("juggle_cmd_agents._resolve_thread", return_value="thread-uuid-1"):
            with patch("juggle_cmd_agents.juggle_cmd_integrate") as mock_mod:
                mock_mod._run_integrate.side_effect = fake_run_integrate
                # We need to make the import work inside cmd_complete_agent
                with patch.dict("sys.modules", {"juggle_cmd_integrate": mock_mod}):
                    cmd_complete_agent(args)

    assert integrate_calls, "_run_integrate was not called"
    assert integrate_calls[0]["worktree_branch"] == "cyc_AB"
```

- [ ] **Step 6.2: Run to verify fail**

```bash
cd ~/github/juggle && uv run pytest tests/test_integrate.py::test_complete_agent_routes_through_run_integrate -v
```

Expected: Test fails (currently calls `_finalize_worktree`, not `_run_integrate`)

- [ ] **Step 6.3: Update `cmd_complete_agent` in `juggle_cmd_agents.py`**

Find the section in `cmd_complete_agent` that calls `_finalize_worktree` (around lines 148-157):

```python
    # Finalize worktree BEFORE closing the thread
    ft_success, ft_msg = _finalize_worktree(thread)
    if not ft_success:
        db.add_action_item(...)
        args.result_summary = f"{args.result_summary} [WARNING: worktree not finalized — {ft_msg}]"
```

Replace it with:

```python
    # Finalize worktree BEFORE closing the thread.
    # Route through _run_integrate (rebase-aware) when worktree fields are present;
    # fall back to bare _finalize_worktree for pre-migration threads.
    if thread.get("worktree_path") and thread.get("worktree_branch") and thread.get("main_repo_path"):
        try:
            import juggle_cmd_integrate as _integrate_mod
            ft_success, ft_msg = _integrate_mod._run_integrate(thread, db)
        except ImportError:
            ft_success, ft_msg = _finalize_worktree(thread)
    else:
        ft_success, ft_msg = _finalize_worktree(thread)

    if not ft_success:
        db.add_action_item(
            thread_id=thread_uuid,
            message=f"⚠️ Worktree finalization failed: {ft_msg}",
            type_="manual_step",
            priority="high",
        )
        args.result_summary = f"{args.result_summary} [WARNING: worktree not finalized — {ft_msg}]"
```

- [ ] **Step 6.4: Run routing test to verify pass**

```bash
cd ~/github/juggle && uv run pytest tests/test_integrate.py::test_complete_agent_routes_through_run_integrate -v
```

Expected: PASS

- [ ] **Step 6.5: Run full test suite to check for regressions**

```bash
cd ~/github/juggle && uv run pytest tests/ -x -q 2>&1 | tail -20
```

Expected: No new failures (pre-existing failures are acceptable per universal rules).

- [ ] **Step 6.6: Commit**

```bash
cd ~/github/juggle && git add src/juggle_cmd_agents.py tests/test_integrate.py
git commit -m "refactor: route cmd_complete_agent worktree finalization through _run_integrate"
```

---

## Task 7: `cmd_integrate` CLI entry point + version bump

**Files:**
- Modify: `src/juggle_cmd_integrate.py` (add `cmd_integrate`)
- Modify: `src/juggle_cli.py` (register `integrate` subparser)
- Modify: `.claude-plugin/plugin.json` (1.49.0 → 1.50.0)
- Modify: `tests/test_integrate.py` (append CLI smoke test)

- [ ] **Step 7.1: Append CLI smoke test to `tests/test_integrate.py`**

```python
# ── cmd_integrate CLI test ────────────────────────────────────────────────────

def test_cmd_integrate_invokes_run_integrate_on_success(git_repo, tmp_path):
    """cmd_integrate resolves thread and calls _run_integrate; exits 0 on success."""
    from juggle_cmd_integrate import cmd_integrate

    wt = _make_worktree(git_repo, str(tmp_path), "AB")
    _add_commit(wt, "feat.py", "y = 2\n", "add feat.py")

    thread = {"id": "thread-uuid-1", "user_label": "AB",
               "worktree_path": wt, "worktree_branch": "cyc_AB",
               "main_repo_path": git_repo}
    db = _make_db()
    db.get_thread.return_value = thread

    args = Mock()
    args.thread_id = "AB"
    args.allow_main = False

    with patch("juggle_cmd_integrate.get_db", return_value=db):
        with patch("juggle_cmd_integrate._resolve_thread", return_value="thread-uuid-1"):
            with patch("juggle_cmd_integrate.get_repo_config",
                       return_value={"push_mode": "none", "test_cmd": ""}):
                with patch("juggle_cmd_integrate._get_lock_path",
                           return_value=tmp_path / "t.lock"):
                    with patch("juggle_cmd_integrate._restart_juggle_daemons"):
                        cmd_integrate(args)  # should not raise SystemExit


def test_cmd_integrate_exits_nonzero_on_failure(tmp_path):
    """cmd_integrate exits 1 when _run_integrate returns failure."""
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
            with patch("juggle_cmd_integrate._get_lock_path",
                       return_value=tmp_path / "t.lock"):
                with pytest.raises(SystemExit) as exc:
                    cmd_integrate(args)
    assert exc.value.code == 1
```

- [ ] **Step 7.2: Run to verify fail**

```bash
cd ~/github/juggle && uv run pytest tests/test_integrate.py -k "cmd_integrate" -v
```

Expected: `ImportError: cannot import name 'cmd_integrate' from 'juggle_cmd_integrate'`

- [ ] **Step 7.3: Add `cmd_integrate` to `juggle_cmd_integrate.py`**

Append at the end of `src/juggle_cmd_integrate.py`:

```python
# ── CLI imports needed by cmd_integrate ──────────────────────────────────────

def _resolve_thread(db, thread_id: str) -> str:
    from juggle_cli_common import _resolve_thread as _rt
    return _rt(db, thread_id)


def get_db():
    from juggle_cli_common import get_db as _get_db
    return _get_db()


# ── CLI entry point ───────────────────────────────────────────────────────────

def cmd_integrate(args):
    """juggle integrate <thread> — rebase-aware atomic worktree finalization."""
    db = get_db()
    thread_uuid = _resolve_thread(db, args.thread_id)
    thread = db.get_thread(thread_uuid)
    if not thread:
        print(f"Error: Thread {args.thread_id} not found.")
        sys.exit(1)

    allow_main = getattr(args, "allow_main", False)
    success, msg = _run_integrate(thread, db, allow_main=allow_main)

    if success:
        print(f"[juggle] integrate OK: {msg}")
    else:
        print(f"Error: integrate failed — {msg}")
        sys.exit(1)
```

- [ ] **Step 7.4: Register `integrate` in `juggle_cli.py`**

Find the `p_stop_watchdog` block (last agent command, around line 663). After `p_stop_watchdog.set_defaults(...)`, add:

```python
    # integrate
    p_integrate = subparsers.add_parser(
        "integrate",
        help="Rebase + test + merge + push a thread's worktree branch into main",
    )
    p_integrate.add_argument("thread_id", help="Thread ID or label")
    p_integrate.add_argument(
        "--allow-main",
        action="store_true",
        dest="allow_main",
        default=False,
        help="Skip worktree guard (operator use only; logged)",
    )
    p_integrate.set_defaults(
        func=lambda a: __import__("juggle_cmd_integrate").cmd_integrate(a)
    )
```

- [ ] **Step 7.5: Bump version in `.claude-plugin/plugin.json`**

Change `"version": "1.49.0"` to `"version": "1.50.0"`.

- [ ] **Step 7.6: Run CLI tests to verify pass**

```bash
cd ~/github/juggle && uv run pytest tests/test_integrate.py -k "cmd_integrate" -v
```

Expected: 2 passed

- [ ] **Step 7.7: Run full test suite**

```bash
cd ~/github/juggle && uv run pytest tests/ -x -q 2>&1 | tail -20
```

Expected: No new failures.

- [ ] **Step 7.8: Verify CLI help shows `integrate`**

```bash
cd ~/github/juggle && uv run src/juggle_cli.py integrate --help
```

Expected output contains:
```
usage: juggle_cli.py integrate [-h] [--allow-main] thread_id
```

- [ ] **Step 7.9: Commit**

```bash
cd ~/github/juggle && git add src/juggle_cmd_integrate.py src/juggle_cli.py .claude-plugin/plugin.json tests/test_integrate.py
git commit -m "feat: add juggle integrate command; bump v1.50.0"
```

---

## Harness smoke gate (CLAUDE.md requirement)

Run before `complete-agent`:

```bash
cd ~/github/juggle && uv run pytest tests/ -q && uv run src/juggle_cli.py doctor --dry-run --db /tmp/juggle-smoke-$$.db
```

Expected: pytest suite passes (ignoring pre-existing failures), doctor exits 0.

---

## Acceptance Criteria (agent-verifiable)

| Scenario | Verification command | Expected |
|----------|---------------------|----------|
| Auto-create: worktree exists at right path | `ls /tmp/juggle-<repo>-<label>` after send-task | dir exists |
| Auto-create: branch name | `git -C /tmp/juggle-<repo>-<label> branch --show-current` | `cyc_<label>` |
| Auto-create: .venv symlinked | `ls -la /tmp/juggle-<repo>-<label>/.venv` | symlink to main repo .venv |
| Hard guard: coder on main → exit 1 | `juggle send-task <agent> <prompt>` with coder + no worktree + no --allow-main | exit 1 + "main worktree" message |
| Researcher exempt | send-task with researcher role | no error, no worktree created |
| `--allow-main` bypass | send-task with `--allow-main` | succeeds, warning logged |
| Integrate happy path (none) | `juggle integrate <thread>` on worktree with commit | feat.py in main, worktree gone, branch gone |
| Integrate happy path (direct) | same, push_mode=direct | commit in remote |
| Integrate happy path (pr) | same, push_mode=pr | branch in remote, main unchanged |
| Conflict → fail-closed | two threads edit same file | exit 1, worktree preserved, action_item filed |
| Red tests → no merge | test_cmd=exit 1 | no ff-merge, action_item filed |
| Idempotent: already merged | integrate on branch with 0 ahead commits | success, cleanup only, "already merged" |
| Lock serialization | `juggle integrate` called twice concurrently | second waits or times out (nonzero) |
| complete-agent uses rebase path | complete-agent on thread with worktree fields | _run_integrate called, not bare ff-merge |
| Self-repo restart | integrate of juggle's own repo | watchdog + monitor restarted |

---

## Risks

1. **Duplicate daemon processes (pre-existing):** `_start_watchdog()` does a `pkill -f juggle-agent-watchdog` sweep before spawning, which mitigates most cases. The root cause (not all start paths check for existing daemons) is a separate bug.
2. **`pr` mode leaves branch ref locally:** The branch stays in the main repo until the PR is merged. Operators should clean up with `git branch -d cyc_<label>` post-merge.
3. **Long-running integrate blocks concurrency:** The per-repo lock serializes integrations. If a test_cmd takes minutes, subsequent integrations for the same repo queue. Tune `test_cmd` to be fast (smoke tests only, not full suite).
4. **`/tmp` path conflicts:** Two different Juggle instances (users) could collide on `/tmp/juggle-<repo>-<label>`. The per-repo lock prevents race conditions, but `/tmp` is not namespaced. Operators sharing a machine should set `JUGGLE_WORKTREE_ROOT` (future enhancement; for now `/tmp` is the default).
