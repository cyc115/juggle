"""juggle_dispatch_worktree_context — worktree auto-create + '## Working
Directory' prompt-section builder for send_task_to_agent.

Extracted from juggle_dispatch_core (2026-07-03, architecture-gate LOC
budget: dispatch_core was at the 300-line ceiling and needed headroom for
T-fix-dispatch-plan-spec-provision's Source-of-truth section).

Owns: worktree base resolution, auto-create-on-first-dispatch, the
'## Working Directory' section text.
Must not own: prompt template selection, tmux send, ledger writes.
"""
from __future__ import annotations

import logging

import juggle_cmd_agents_common as _com

_log = logging.getLogger("juggle-dispatch-core")


def build_worktree_context(
    db, *, role: str, agent: dict, pane_id: str, thread_id: str | None,
    allow_main: bool, worktree_path_override: str | None,
    worktree_branch_override: str | None, main_repo_override: str | None,
    default_worktree_root: str,
) -> str:
    """Auto-create (coder/planner only) or reuse the thread's worktree, and
    return the '## Working Directory' prompt section (or "" when the role
    doesn't use one, e.g. researcher/planner-without-repo)."""
    thread_wt = db.get_thread(thread_id) if thread_id else None
    if role not in ("coder", "planner") or not thread_wt:
        return ""

    thread_label_wt = thread_wt.get("user_label") or thread_wt["id"][:6]
    # Explicit CLI overrides: persist then reload
    if worktree_path_override:
        db.update_thread(
            thread_id,
            worktree_path=worktree_path_override,
            worktree_branch=worktree_branch_override or thread_wt.get("worktree_branch"),
            main_repo_path=main_repo_override or (agent.get("repo_path") or "").strip(),
        )
        thread_wt = db.get_thread(thread_id)

    # Worktree base resolution (reject-filtered ~/.claude / plugin dir).
    from juggle_repo_binding import resolve_worktree_base
    repo_path_wt = resolve_worktree_base(
        main_repo_override, agent.get("repo_path"),
        thread_wt.get("main_repo_path"), pane_id)

    existing_wt = (thread_wt.get("worktree_path") or "").strip()

    if not existing_wt and repo_path_wt and not allow_main:
        from juggle_stack_base import topic_id_for_thread
        ok_wt, wt_path_new, branch_new, msg_wt = _com._create_worktree(
            repo_path_wt, thread_label_wt, default_worktree_root,
            db=db, topic_id=topic_id_for_thread(db, thread_id))
        if ok_wt:
            db.update_thread(
                thread_id,
                worktree_path=wt_path_new,
                worktree_branch=branch_new,
                main_repo_path=repo_path_wt,
            )
            thread_wt = db.get_thread(thread_id)
            existing_wt = wt_path_new
            _log.info("[juggle] %s", msg_wt)
        else:
            _log.warning("[juggle] WARNING: worktree auto-create failed: %s", msg_wt)

    # Atomic-dispatch guard (df-atomic-dispatch, incident 2026-07-03 defect 1):
    # a coder MUST run in an isolated worktree. Refuse LOUDLY when none was
    # established — whether _create_worktree failed (repo_path_wt set) OR no
    # source repo could be resolved at all (repo_path_wt ''). Silently returning
    # "" here let the topic reach 'running' with no worktree and no live agent —
    # a permanent wedge the stale sweep can't reclaim (thread-bound) and reconcile
    # protects ('running'). The tick's dispatch except-path catches this raise and
    # rolls the topic back to a claimable state + files a failure action item.
    # (planner-without-repo stays lenient: it may legitimately run repo-less.)
    if not existing_wt and not allow_main and (repo_path_wt or role == "coder"):
        raise RuntimeError(
            f"cannot dispatch {role} task without an isolated worktree "
            f"(repo={repo_path_wt or '<none resolved>'}, thread={thread_label_wt}). "
            f"Worktree auto-create failed or no source repo resolved. "
            f"Use allow_main=True to override (bypass is logged)."
        )

    if allow_main and repo_path_wt:
        _log.warning(
            "[juggle] WARNING: allow_main used for %s on %s (thread %s) — "
            "main-worktree guard bypassed.",
            role, repo_path_wt, thread_label_wt,
        )

    if not existing_wt:
        return ""

    branch_label_wt = (thread_wt.get("worktree_branch") or "") if thread_wt else ""
    return (
        f"## Working Directory\n"
        f"This task runs in an isolated worktree. "
        f"cd into it before any git or file operations:\n"
        f"```bash\ncd {existing_wt}\n```\n"
        f"Branch: `{branch_label_wt}`\n\n---\n\n"
    )
