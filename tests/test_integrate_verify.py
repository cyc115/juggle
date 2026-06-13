"""Autopilot Phase 3 — verify_cmd pre-merge inside _run_integrate (DA M3)
plus pre-merge diffstat capture for dependent hydration (DA M4 deferred item).
"""
# ruff: noqa: F811  (fixtures imported from test_integrate shadow at param sites)
import shlex
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest

from test_integrate import git_repo, _add_commit, _make_worktree  # noqa: F401

PY = shlex.quote(sys.executable)


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    from juggle_db import JuggleDB
    d = JuggleDB(db_path=str(tmp_path / "j.db"))
    d.init_db()
    return d


def _task_thread(db, git_repo, tmp_path, label, verify_cmd):
    """Real worktree with one commit + a graph task bound to a real thread."""
    from dbops import db_graph
    wt = _make_worktree(git_repo, str(tmp_path), label)
    _add_commit(wt, f"feat_{label}.py", "y = 2\n", f"feat: {label}")
    tid = db.create_thread(f"task {label}", session_id="sessV")
    db_graph.create_task(db, task_id=f"n{label}", project_id="INBOX",
                         title=label, prompt=f"do {label}", verify_cmd=verify_cmd)
    db_graph.set_task_thread(db, f"n{label}", tid)
    thread = {"id": tid, "worktree_path": wt,
              "worktree_branch": f"cyc_{label}", "main_repo_path": git_repo}
    return thread, wt


def _integrate(thread, db, tmp_path):
    from juggle_cmd_integrate import _run_integrate
    with patch("juggle_cmd_integrate.get_repo_config",
               return_value={"push_mode": "none", "test_cmd": ""}):
        with patch("juggle_integrate_lock._get_lock_path",
                   return_value=tmp_path / "v.lock"):
            with patch("juggle_cmd_integrate._restart_juggle_daemons"):
                return _run_integrate(thread, db)


# ── run_verify_cmd unit ───────────────────────────────────────────────────────

def test_run_verify_cmd_retries_exactly_once_on_failure(tmp_path):
    """DA M3: timeout/flake policy = one retry. A persistently failing
    verify_cmd is attempted exactly twice."""
    from juggle_integrate_verify import run_verify_cmd
    marker = tmp_path / "attempts"
    cmd = (f"{PY} -c \"import sys,pathlib;"
           f"p=pathlib.Path({str(marker)!r});"
           f"p.write_text(p.read_text()+'x' if p.exists() else 'x');"
           f"sys.exit(1)\"")
    ok, detail = run_verify_cmd(cmd, str(tmp_path))
    assert not ok
    assert "exit 1" in detail
    assert marker.read_text() == "xx", "expected exactly 2 attempts"


def test_run_verify_cmd_flake_passes_on_retry(tmp_path):
    """First attempt fails, retry passes → verify is green (flake policy)."""
    from juggle_integrate_verify import run_verify_cmd
    marker = tmp_path / "flake"
    cmd = (f"{PY} -c \"import sys,pathlib;"
           f"p=pathlib.Path({str(marker)!r});"
           f"sys.exit(0) if p.exists() else (p.write_text('x'), sys.exit(1))\"")
    ok, _ = run_verify_cmd(cmd, str(tmp_path))
    assert ok


def test_run_verify_cmd_times_out(tmp_path):
    from juggle_integrate_verify import run_verify_cmd
    cmd = f"{PY} -c \"import time; time.sleep(30)\""
    t0 = time.monotonic()
    ok, detail = run_verify_cmd(cmd, str(tmp_path), timeout_secs=1)
    assert not ok
    assert "timed out" in detail
    assert time.monotonic() - t0 < 10  # 2 attempts x 1s, not 30s


def test_run_verify_cmd_never_uses_a_shell(tmp_path):
    """Shell metacharacters are data, not operators (lint-gated at load AND
    shlex/exec here — defense in depth against the shell=True DA M3 hole)."""
    from juggle_integrate_verify import run_verify_cmd
    evil = tmp_path / "pwned"
    ok, _ = run_verify_cmd(f"{PY} -c pass ; touch {evil}", str(tmp_path))
    assert not evil.exists()


# ── pre-merge pipeline (pin) ──────────────────────────────────────────────────

def test_failed_verify_leaves_main_untouched_pin(db, git_repo, tmp_path):
    """Regression pin (2026-06-10, DA M3): verify_cmd ran post-merge in rev 1
    of the design — a failure would have surfaced AFTER code shipped to main.
    verify_cmd now runs in the worktree, post-rebase, PRE-merge: a failing
    verify_cmd must abort the integrate with a VERIFY_FAIL_PREFIX reason,
    leave main untouched, and preserve the worktree + branch."""
    from juggle_integrate_verify import VERIFY_FAIL_PREFIX

    thread, wt = _task_thread(db, git_repo, tmp_path, "VF",
                              f"{PY} -c \"import sys; sys.exit(1)\"")
    ok, msg = _integrate(thread, db, tmp_path)

    assert not ok
    assert msg.startswith(VERIFY_FAIL_PREFIX)
    assert not (Path(git_repo) / "feat_VF.py").exists(), "merged despite verify failure"
    assert Path(wt).is_dir(), "worktree not preserved"
    branches = subprocess.run(["git", "-C", git_repo, "branch"],
                              capture_output=True, text=True).stdout
    assert "cyc_VF" in branches, "branch not preserved"
    items = db.get_open_action_items()
    assert any(VERIFY_FAIL_PREFIX in i["message"] and i["priority"] == "high"
               for i in items)


def test_passing_verify_merges_and_stores_diffstat(db, git_repo, tmp_path):
    from dbops import db_graph
    thread, wt = _task_thread(db, git_repo, tmp_path, "VP",
                              f"{PY} -c \"import sys; sys.exit(0)\"")
    ok, msg = _integrate(thread, db, tmp_path)
    assert ok, msg
    assert (Path(git_repo) / "feat_VP.py").exists()
    task = db_graph.get_task(db, "nVP")
    assert "feat_VP.py" in (task["diffstat"] or ""), "pre-merge diffstat not stored"


def test_failed_verify_still_stores_diffstat(db, git_repo, tmp_path):
    """Diffstat is captured before verify runs — available for diagnosis."""
    from dbops import db_graph
    thread, _ = _task_thread(db, git_repo, tmp_path, "VD",
                             f"{PY} -c \"import sys; sys.exit(1)\"")
    ok, _ = _integrate(thread, db, tmp_path)
    assert not ok
    assert "feat_VD.py" in (db_graph.get_task(db, "nVD")["diffstat"] or "")


def test_verify_and_test_cmd_both_required_when_both_set(db, git_repo, tmp_path):
    """Repo test_cmd green but task verify_cmd red → no merge (both must pass)."""
    from juggle_cmd_integrate import _run_integrate
    thread, _ = _task_thread(db, git_repo, tmp_path, "VB",
                             f"{PY} -c \"import sys; sys.exit(1)\"")
    with patch("juggle_cmd_integrate.get_repo_config",
               return_value={"push_mode": "direct", "test_cmd": "true"}):
        with patch("juggle_integrate_lock._get_lock_path",
                   return_value=tmp_path / "v.lock"):
            ok, msg = _run_integrate(thread, db)
    assert not ok
    assert not (Path(git_repo) / "feat_VB.py").exists()


def test_task_without_verify_cmd_merges_normally(db, git_repo, tmp_path):
    from dbops import db_graph
    thread, _ = _task_thread(db, git_repo, tmp_path, "VN", None)
    ok, msg = _integrate(thread, db, tmp_path)
    assert ok, msg
    assert (Path(git_repo) / "feat_VN.py").exists()
    # diffstat still captured for hydration
    assert "feat_VN.py" in (db_graph.get_task(db, "nVN")["diffstat"] or "")


# ── completion mapping ────────────────────────────────────────────────────────

def test_verify_failure_marks_task_failed_verify_not_failed_integration(db):
    """The VERIFY_FAIL_PREFIX channel maps to 'failed-verify' on the task —
    distinct from 'failed-integration' — and main stays untouched (pin)."""
    from dbops import db_graph
    from juggle_cmd_agents_graph import mark_graph_task

    tid = db.create_thread("t", session_id="sessV")
    db_graph.create_task(db, task_id="vx", project_id="INBOX",
                         title="VX", prompt="p")
    db_graph.set_task_thread(db, "vx", tid)
    mark_graph_task(db, tid, False, None, "sessV", verify_failed=True)
    assert db_graph.get_task(db, "vx")["state"] == "failed-verify"


def test_diffstat_column_round_trip(db):
    from dbops import db_graph
    db_graph.create_task(db, task_id="ds", project_id="INBOX",
                         title="DS", prompt="p")
    db_graph.set_task_diffstat(db, "ds", " 1 file changed")
    assert db_graph.get_task(db, "ds")["diffstat"] == " 1 file changed"


def test_hydration_includes_dep_diffstat(db):
    """Phase 2 deferred the integrated-branch diffstat from hydration; it is
    now captured pre-merge and must flow into dependent prompts."""
    from juggle_graph_dispatch import build_hydration

    task = {"id": "child", "title": "Child", "prompt": "do child",
            "verify_cmd": None}
    deps = [{"id": "parent", "title": "Parent", "handoff": "api: use foo()",
             "diffstat": " src/foo.py | 10 +++++"}]
    prompt = build_hydration("objective", task, deps)
    assert "api: use foo()" in prompt
    assert "src/foo.py | 10" in prompt
