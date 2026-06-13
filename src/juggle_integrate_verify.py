"""juggle_integrate_verify — pre-merge task verification + diffstat capture.

Owns: running a graph task's ``verify_cmd`` inside the worktree (post-rebase,
pre-merge — DA M3: nothing merges unverified, a verify failure leaves main
untouched) with a hard timeout and exactly one retry, and capturing the
integrated-branch diffstat pre-merge onto the task for dependent hydration
(DA M4 — integrate deletes the branch+worktree on success, so this is the
only cheap moment to capture it).
Must not own: the integration pipeline (juggle_cmd_integrate), task state
transitions (dbops.db_graph — complete-agent maps the outcome via
``mark_completion``), or load-time verify_cmd lint (juggle_cmd_graph).
"""

from __future__ import annotations

import shlex
import subprocess

# Failure-reason prefix: the single deterministic channel telling
# complete-agent that an integrate failure was a VERIFY failure
# (task → failed-verify, not failed-integration).
VERIFY_FAIL_PREFIX = "verify_cmd failed"

VERIFY_TIMEOUT_SECS = 600
VERIFY_RETRIES = 1  # exactly one retry on failure
DIFFSTAT_MAX_CHARS = 2000


def run_verify_cmd(
    verify_cmd: str, cwd: str, *, timeout_secs: int | None = None
) -> tuple[bool, str]:
    """Run ``verify_cmd`` in ``cwd``. Returns (ok, failure_detail).

    shlex-split, shell=False — the command is lint-gated at graph load
    (allowlisted executable, no shell metacharacters), and never goes
    through a shell here regardless. Timeout per attempt; one retry.
    """
    timeout = VERIFY_TIMEOUT_SECS if timeout_secs is None else timeout_secs
    try:
        argv = shlex.split(verify_cmd)
    except ValueError as e:
        return False, f"unparseable command: {e}"
    if not argv:
        return False, "empty command"

    detail = ""
    for _attempt in range(VERIFY_RETRIES + 1):
        try:
            result = subprocess.run(
                argv, capture_output=True, text=True, cwd=cwd, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            detail = f"timed out after {timeout}s"
            continue
        except OSError as e:
            detail = str(e)
            continue
        if result.returncode == 0:
            return True, ""
        detail = (
            f"exit {result.returncode}. "
            f"stdout tail: {(result.stdout or '')[-300:].strip()} "
            f"stderr tail: {(result.stderr or '')[-200:].strip()}"
        ).strip()
    return False, detail


def capture_diffstat(worktree_path: str, rebase_onto: str) -> str:
    """Diffstat of the rebased branch vs the merge target (pre-merge)."""
    result = subprocess.run(
        ["git", "-C", worktree_path, "diff", "--stat", f"{rebase_onto}..HEAD"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()[:DIFFSTAT_MAX_CHARS]


def verify_task_premerge(
    db, task: dict | None, worktree_path: str, rebase_onto: str
) -> tuple[bool, str]:
    """Pre-merge task gate: store the diffstat, then run verify_cmd (if any).

    Returns (True, "") when the merge may proceed, else (False, reason) with
    the reason prefixed by VERIFY_FAIL_PREFIX. No-op for non-task threads.
    """
    if not task:
        return True, ""
    try:
        from dbops import db_graph
        diffstat = capture_diffstat(worktree_path, rebase_onto)
        if diffstat:
            db_graph.set_task_diffstat(db, task["id"], diffstat)
    except Exception:
        pass  # diffstat is best-effort hydration enrichment, never a gate

    cmd = (task.get("verify_cmd") or "").strip()
    if not cmd:
        return True, ""
    ok, detail = run_verify_cmd(cmd, worktree_path)
    if ok:
        return True, ""
    return False, (
        f"{VERIFY_FAIL_PREFIX} for task {task['id']} (`{cmd}`): {detail}. "
        f"Ran post-rebase in the worktree with one retry — "
        f"no merge performed, main untouched."
    )
