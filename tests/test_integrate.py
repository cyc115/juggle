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


# ── Lock tests ────────────────────────────────────────────────────────────────

def test_acquire_lock_creates_pidfile_owned_by_current_process(tmp_path):
    from juggle_cmd_integrate import acquire_repo_lock, release_repo_lock
    with patch("juggle_integrate_lock._get_lock_path", return_value=tmp_path / "t.lock"):
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
    with patch("juggle_integrate_lock._get_lock_path", return_value=lock_file):
        lp = acquire_repo_lock("/repo", timeout_secs=5)
    pid = int(lp.read_text().strip().splitlines()[0])
    assert pid == os.getpid()
    release_repo_lock(lp)


def test_acquire_lock_times_out_on_alive_pid(tmp_path):
    from juggle_cmd_integrate import acquire_repo_lock
    lock_file = tmp_path / "alive.lock"
    # PID 1 (init/launchd) is always alive; recent timestamp so not aged-out
    lock_file.write_text(f"1\n{time.time()}\n")
    with patch("juggle_integrate_lock._get_lock_path", return_value=lock_file):
        with pytest.raises(RuntimeError, match="Cannot acquire lock"):
            acquire_repo_lock("/repo", timeout_secs=0.3)


def test_lock_live_holder_is_never_stolen_pin(tmp_path):
    """Regression pin (2026-06-10, DA M2): acquire_repo_lock stole LIVE locks
    aged >300s — under autopilot fan-in, concurrent completions stole the
    merge-queue lock from a holder still running test_cmd (full pytest exceeds
    300s) and interleaved rebases on main. Steal is now dead-PID-only: a live
    holder, however old its lockfile, must NOT be stolen; the waiter times out
    with RuntimeError and the lockfile stays owned by the live holder."""
    from juggle_cmd_integrate import acquire_repo_lock
    from juggle_integrate_lock import _read_lock

    holder = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    try:
        lock_file = tmp_path / "live.lock"
        # Lock timestamp far older than the waiter's deadline (simulates a
        # holder mid-test_cmd, aged past any steal threshold).
        lock_file.write_text(f"{holder.pid}\n{time.time() - 4000}\n")
        with patch("juggle_integrate_lock._get_lock_path", return_value=lock_file):
            with pytest.raises(RuntimeError, match="Cannot acquire lock"):
                acquire_repo_lock("/repo", timeout_secs=0.3)
        pid, _ = _read_lock(lock_file)
        assert pid == holder.pid, "live holder's lock was stolen"
    finally:
        holder.kill()
        holder.wait()


def test_lock_holder_heartbeats_lockfile_while_held(tmp_path):
    """DA M2: the holder refreshes the lockfile timestamp periodically during
    long operations (e.g. test_cmd), so observers can tell it is live."""
    from juggle_cmd_integrate import acquire_repo_lock, release_repo_lock
    from juggle_integrate_lock import _read_lock

    lock_file = tmp_path / "hb.lock"
    with patch("juggle_integrate_lock._get_lock_path", return_value=lock_file):
        lp = acquire_repo_lock("/repo", timeout_secs=5, heartbeat_interval=0.05)
    try:
        _, ts0 = _read_lock(lp)
        deadline = time.time() + 2.0
        ts1 = ts0
        while ts1 <= ts0 and time.time() < deadline:
            time.sleep(0.05)
            _, ts1 = _read_lock(lp)
        assert ts1 > ts0, "lockfile timestamp never heartbeat-refreshed"
    finally:
        release_repo_lock(lp)
    # Heartbeat stops with release: file gone and never recreated.
    time.sleep(0.2)
    assert not lp.exists()


def test_integrate_task_bound_thread_uses_autopilot_lock_deadline(tmp_path, git_repo):
    """DA M2: autopilot-context integrations (thread bound to a graph task)
    wait up to 30 min for the merge-queue lock; lock timeout files a HIGH
    action item instead of failing silently."""
    import juggle_cmd_integrate as jci
    from juggle_db import JuggleDB
    from dbops import db_graph

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    tid = db.create_thread("task thread", session_id="sessL")
    db_graph.create_task(db, task_id="n1", project_id="INBOX",
                         title="N1", prompt="do n1")
    db_graph.set_task_thread(db, "n1", tid)

    wt = _make_worktree(git_repo, str(tmp_path), "NB")
    thread = {"id": tid, "worktree_path": wt,
              "worktree_branch": "cyc_NB", "main_repo_path": git_repo}

    seen = {}

    def fake_acquire(repo_path, timeout_secs=300.0):
        seen["timeout"] = timeout_secs
        raise RuntimeError(f"Cannot acquire lock for {repo_path}: held by PID 1")

    with patch.object(jci, "acquire_repo_lock", side_effect=fake_acquire):
        ok, msg = jci._run_integrate(thread, db)

    assert not ok and "Lock acquisition failed" in msg
    assert seen["timeout"] == jci.AUTOPILOT_LOCK_TIMEOUT_SECS == 1800.0
    items = db.get_open_action_items()
    assert any("lock" in i["message"].lower() and i["priority"] == "high"
               for i in items), "no action item filed on lock timeout"


def test_integrate_plain_thread_keeps_default_lock_deadline(tmp_path, git_repo):
    """Non-autopilot integrations keep the 300s lock wait."""
    import juggle_cmd_integrate as jci

    wt = _make_worktree(git_repo, str(tmp_path), "PL")
    thread = {"id": "t-plain", "worktree_path": wt,
              "worktree_branch": "cyc_PL", "main_repo_path": git_repo}
    db = _make_db()
    seen = {}

    def fake_acquire(repo_path, timeout_secs=300.0):
        seen["timeout"] = timeout_secs
        raise RuntimeError("Cannot acquire lock: held by PID 1")

    with patch.object(jci, "acquire_repo_lock", side_effect=fake_acquire):
        ok, _ = jci._run_integrate(thread, db)
    assert not ok
    assert seen["timeout"] == 300.0


def test_release_lock_noop_when_not_owner(tmp_path):
    from juggle_cmd_integrate import release_repo_lock
    lock_file = tmp_path / "other.lock"
    lock_file.write_text(f"1\n{time.time()}\n")  # owned by PID 1
    release_repo_lock(lock_file)
    assert lock_file.exists()  # not removed


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
        with patch("juggle_integrate_lock._get_lock_path", return_value=tmp_path / "t.lock"):
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
        with patch("juggle_integrate_lock._get_lock_path", return_value=tmp_path / "t.lock"):
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
        with patch("juggle_integrate_lock._get_lock_path", return_value=tmp_path / "t.lock"):
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
        with patch("juggle_integrate_lock._get_lock_path", return_value=tmp_path / "t.lock"):
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
        with patch("juggle_integrate_lock._get_lock_path", return_value=tmp_path / "t.lock"):
            ok, msg = _run_integrate(thread, db)

    assert not ok
    assert not (Path(git_repo) / "new.py").exists()   # NOT merged
    db.add_action_item.assert_called_once()


def test_integrate_already_merged_skips_straight_to_cleanup(git_repo, tmp_path):
    """Branch with 0 commits ahead of main → G2 refuses, worktree preserved.

    G2 (empty-branch guard, 2026-06-20) replaces the old auto-cleanup path:
    0-commit branches are now always refused so the agent must explicitly
    decide (commit / PARTIAL / manual cleanup).  Worktree is preserved.
    """
    from juggle_cmd_integrate import _run_integrate

    wt = _make_worktree(git_repo, str(tmp_path), "GH")

    thread = {"id": "t-1", "worktree_path": wt,
               "worktree_branch": "cyc_GH", "main_repo_path": git_repo}
    db = _make_db()

    with patch("juggle_cmd_integrate.get_repo_config", return_value={"push_mode": "none", "test_cmd": ""}):
        with patch("juggle_integrate_lock._get_lock_path", return_value=tmp_path / "t.lock"):
            with patch("juggle_cmd_integrate._restart_juggle_daemons"):
                ok, msg = _run_integrate(thread, db)

    assert not ok, "G2 must refuse 0-commit branch"
    assert "nothing to merge" in msg.lower() or "0 commits" in msg.lower()
    assert Path(wt).exists(), "worktree must be preserved on G2 refusal"


def test_integrate_idempotent_missing_worktree_returns_error(git_repo, tmp_path):
    """If worktree path doesn't exist, integrate returns failure gracefully."""
    from juggle_cmd_integrate import _run_integrate

    thread = {"id": "t-1",
               "worktree_path": str(tmp_path / "nonexistent"),
               "worktree_branch": "cyc_ZZ",
               "main_repo_path": git_repo}
    db = _make_db()

    with patch("juggle_integrate_lock._get_lock_path", return_value=tmp_path / "t.lock"):
        ok, msg = _run_integrate(thread, db)

    assert not ok
    assert "does not exist" in msg.lower() or "nonexistent" in msg.lower()


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
    _cm = Mock()
    _cm.__enter__ = Mock(return_value=Mock(
        execute=Mock(return_value=Mock(fetchone=Mock(return_value=None)))
    ))
    _cm.__exit__ = Mock(return_value=False)
    db._connect = Mock(return_value=_cm)

    integrate_calls = []

    def fake_run_integrate(t, d, allow_main=False):
        integrate_calls.append(t)
        return True, "integrated"

    args = Mock()
    args.thread_id = "thread-uuid-1"
    args.result_summary = "done"
    args.retain = None

    with patch("juggle_cli_common.get_db", return_value=db):
        with patch("juggle_cli_common._resolve_thread", return_value="thread-uuid-1"):
            with patch("juggle_cmd_agents_common.juggle_cmd_integrate") as mock_mod:
                mock_mod._run_integrate.side_effect = fake_run_integrate
                cmd_complete_agent(args)

    assert integrate_calls, "_run_integrate was not called"
    assert integrate_calls[0]["worktree_branch"] == "cyc_AB"


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
                with patch("juggle_integrate_lock._get_lock_path",
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
            with patch("juggle_integrate_lock._get_lock_path",
                       return_value=tmp_path / "t.lock"):
                with pytest.raises(SystemExit) as exc:
                    cmd_integrate(args)
    assert exc.value.code == 1


# ── Regression pins: direct-mode main-un-merged bugs (2026-06-14) ────────────

def test_integrate_direct_graphify_untracked_new_file_blocks_merge_pin(git_repo_with_remote, tmp_path):
    """Regression pin (2026-06-14): graphify watch writes a NEW file to
    graphify-out/ in main_repo_path (untracked, not yet in main HEAD). The
    feature branch ALSO adds that same file as a tracked commit. git checkout
    -- graphify-out/ does NOT remove untracked files → ff-merge fails with
    'untracked working tree file would be overwritten'. Fix: also run
    git clean -fd -- graphify-out/ before the merge."""
    from juggle_cmd_integrate import _run_integrate

    local, remote = git_repo_with_remote
    wt = _make_worktree(local, str(tmp_path), "GU")

    # Feature branch adds a NEW tracked file in graphify-out/
    graphify_dir = Path(wt) / "graphify-out"
    graphify_dir.mkdir()
    (graphify_dir / ".graphify_chunk_03.json").write_text('{"chunk": 3}')
    subprocess.run(["git", "-C", wt, "add", "graphify-out/"], check=True, capture_output=True)
    subprocess.run(["git", "-C", wt, "commit", "-m", "feat: add chunk_03"], check=True, capture_output=True)

    # Simulate graphify watch: same file created in MAIN working tree (untracked)
    main_graphify_dir = Path(local) / "graphify-out"
    main_graphify_dir.mkdir(exist_ok=True)
    (main_graphify_dir / ".graphify_chunk_03.json").write_text('{"chunk": "stale"}')

    thread = {"id": "t-gu", "worktree_path": wt,
              "worktree_branch": "cyc_GU", "main_repo_path": local}
    db = _make_db()

    with patch("juggle_cmd_integrate.get_repo_config", return_value={"push_mode": "direct", "test_cmd": ""}):
        with patch("juggle_integrate_lock._get_lock_path", return_value=tmp_path / "t.lock"):
            with patch("juggle_cmd_integrate._restart_juggle_daemons"):
                ok, msg = _run_integrate(thread, db)

    assert ok, f"integrate failed due to untracked graphify-out/ file: {msg}"
    # The feature branch's committed file should be present in main now
    assert (Path(local) / "graphify-out" / ".graphify_chunk_03.json").exists()
    # Verify remote was updated (direct push)
    remote_log = subprocess.run(
        ["git", "-C", remote, "log", "--oneline", "-1"],
        capture_output=True, text=True,
    ).stdout
    assert "chunk_03" in remote_log


def test_integrate_direct_main_checked_out_on_wrong_branch_fails_loudly_pin(git_repo_with_remote, tmp_path):
    """Regression pin (2026-06-14, ZA incident): if main_repo_path's HEAD is on
    a branch other than the expected main branch, integrate must fail loudly
    before attempting any merge — not silently push the wrong branch or no-op."""
    from juggle_cmd_integrate import _run_integrate

    local, remote = git_repo_with_remote
    wt = _make_worktree(local, str(tmp_path), "WB")
    _add_commit(wt, "feat.py", "y = 2\n", "feat: add feature")

    main_sha_before = subprocess.run(
        ["git", "-C", local, "rev-parse", "main"],
        capture_output=True, text=True,
    ).stdout.strip()

    # Put main repo's HEAD on a non-main branch (simulates ZA external state)
    subprocess.run(
        ["git", "-C", local, "checkout", "-b", "stray-branch"],
        check=True, capture_output=True,
    )

    thread = {"id": "t-wb", "worktree_path": wt,
              "worktree_branch": "cyc_WB", "main_repo_path": local}
    db = _make_db()

    with patch("juggle_cmd_integrate.get_repo_config", return_value={"push_mode": "direct", "test_cmd": ""}):
        with patch("juggle_integrate_lock._get_lock_path", return_value=tmp_path / "t.lock"):
            ok, msg = _run_integrate(thread, db)

    assert not ok, "integrate should fail when main_repo is on the wrong branch"
    assert "stray-branch" in msg or "main" in msg.lower(), f"error msg should name the problem branch: {msg}"
    # main must be un-touched
    main_sha_after = subprocess.run(
        ["git", "-C", local, "rev-parse", "main"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert main_sha_after == main_sha_before, "main was advanced despite wrong HEAD branch"
    db.add_action_item.assert_called_once()


def test_integrate_direct_test_retry_merges_on_transient_failure_pin(git_repo_with_remote, tmp_path):
    """Regression pin (2026-06-14): spurious single test_cmd failure (pytest
    flake under load) left main un-merged. Fix: retry test_cmd once; only
    abort if both attempts fail."""
    from juggle_cmd_integrate import _run_integrate

    local, remote = git_repo_with_remote
    wt = _make_worktree(local, str(tmp_path), "RT")
    _add_commit(wt, "feat.py", "y = 2\n", "feat: add feature")

    # Script that fails on first invocation, succeeds on second
    counter_file = tmp_path / "count.txt"
    counter_file.write_text("0")
    script = tmp_path / "flaky_test.sh"
    script.write_text(
        f"#!/bin/sh\n"
        f"n=$(cat {counter_file}); n=$((n+1)); echo $n > {counter_file}\n"
        f"if [ $n -le 1 ]; then exit 1; fi\n"
        f"exit 0\n"
    )
    script.chmod(0o755)

    thread = {"id": "t-rt", "worktree_path": wt,
              "worktree_branch": "cyc_RT", "main_repo_path": local}
    db = _make_db()

    with patch("juggle_cmd_integrate.get_repo_config",
               return_value={"push_mode": "direct", "test_cmd": str(script)}):
        with patch("juggle_integrate_lock._get_lock_path", return_value=tmp_path / "t.lock"):
            with patch("juggle_cmd_integrate._restart_juggle_daemons"):
                ok, msg = _run_integrate(thread, db)

    assert ok, f"integrate should succeed after test retry: {msg}"
    assert int(counter_file.read_text().strip()) == 2, "test_cmd should be invoked exactly twice"
    # Verify merge + push
    remote_log = subprocess.run(
        ["git", "-C", remote, "log", "--oneline", "-1"],
        capture_output=True, text=True,
    ).stdout
    assert "add feature" in remote_log


# ── Regression pin: full-suite directive (2026-06-20) ────────────────────────

def test_integrate_runs_full_suite_no_deselect_no_scoping_pin(git_repo, tmp_path):
    """Regression pin (2026-06-20): user directive 'always run the FULL test
    suite, never a subset'. Symptom pre-fix: integrate scoped tests to
    branch-changed files (test_scope='changed') and prepended --deselect flags
    for integrate.quarantine_tests, so a green integrate could skip whole
    swaths of the suite. After the flip the assembled command under DEFAULT
    config must be the bare test_cmd: NO --deselect anywhere, and NO extra
    path-scoping args appended (the command the agent configured runs verbatim)."""
    import juggle_cmd_integrate as jci

    wt = _make_worktree(git_repo, str(tmp_path), "FS")
    # Change a SOURCE file with no obvious test mapping — pre-fix this would
    # have triggered scoped/full-fallback selection logic; post-fix it is
    # irrelevant because the full test_cmd always runs verbatim.
    _add_commit(wt, "src_thing.py", "q = 9\n", "feat: add src_thing")

    base_cmd = "uv run pytest -q"
    captured = {}
    real_run = jci.subprocess.run

    def spy_run(cmd, *a, **k):
        # The test_cmd invocation is the shell=True call whose cmd contains pytest.
        if k.get("shell") and isinstance(cmd, str) and "pytest" in cmd:
            captured["cmd"] = cmd
            # Pretend the suite passed so the rest of integrate proceeds.
            class _R:
                returncode = 0
                stdout = ""
                stderr = ""
            return _R()
        return real_run(cmd, *a, **k)

    thread = {"id": "t-fs", "worktree_path": wt,
              "worktree_branch": "cyc_FS", "main_repo_path": git_repo}
    db = _make_db()

    with patch("juggle_cmd_integrate.get_repo_config",
               return_value={"push_mode": "direct", "test_cmd": base_cmd}):
        with patch.object(jci.subprocess, "run", side_effect=spy_run):
            with patch("juggle_integrate_lock._get_lock_path", return_value=tmp_path / "t.lock"):
                with patch("juggle_cmd_integrate._restart_juggle_daemons"):
                    jci._run_integrate(thread, db)

    assert "cmd" in captured, "test_cmd (pytest) was never invoked by integrate"
    run_cmd = captured["cmd"]
    # The directive: no deselect, no path-scoping — the bare configured command.
    assert "--deselect" not in run_cmd, (
        f"integrate still deselects tests under default config: {run_cmd!r}"
    )
    assert run_cmd == base_cmd, (
        f"integrate scoped/altered the test command (must run it verbatim): {run_cmd!r}"
    )
