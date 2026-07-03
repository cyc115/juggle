"""juggle_dispatch_worktree — worktree auto-create for coder/planner dispatch.

Extracted from juggle_dispatch_core.send_task_to_agent (2026-07-03, LOC gate —
T-fix-literal-finalize-line). Owns: resolving/creating the isolated worktree a
coder/planner task runs in and rendering its "## Working Directory" prompt
block.
Must not own: pane/prompt/ledger plumbing (juggle_dispatch_core).
"""

from __future__ import annotations

import logging

import juggle_cmd_agents_common as _com

_log = logging.getLogger("juggle-dispatch-core")


def prepare_worktree_context(
    db,
    agent: dict,
    thread_id: str,
    role: str | None,
    thread_wt: dict,
    thread_label: str,
    pane_id: str,
    default_worktree_root: str,
    *,
    allow_main: bool = False,
    worktree_path_override: str | None = None,
    worktree_branch_override: str | None = None,
    main_repo_override: str | None = None,
) -> tuple[str, dict]:
    """Returns (worktree_context_prompt_block, refreshed_thread_row).

    Raises RuntimeError if a coder/planner task cannot get an isolated
    worktree and ``allow_main`` wasn't set.
    """
    if role not in ("coder", "planner") or not thread_wt:
        return "", thread_wt

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
            repo_path_wt, thread_label, default_worktree_root,
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

    if not existing_wt and repo_path_wt and not allow_main:
        raise RuntimeError(
            f"cannot dispatch {role} task without an isolated worktree "
            f"(repo={repo_path_wt}). Worktree auto-create failed. "
            f"Use allow_main=True to override (bypass is logged)."
        )

    if allow_main and repo_path_wt:
        _log.warning(
            "[juggle] WARNING: allow_main used for %s on %s (thread %s) — "
            "main-worktree guard bypassed.",
            role, repo_path_wt, thread_label,
        )

    worktree_context = ""
    if existing_wt:
        branch_label_wt = (thread_wt.get("worktree_branch") or "") if thread_wt else ""
        worktree_context = (
            f"## Working Directory\n"
            f"This task runs in an isolated worktree. "
            f"cd into it before any git or file operations:\n"
            f"```bash\ncd {existing_wt}\n```\n"
            f"Branch: `{branch_label_wt}`\n\n---\n\n"
        )
    return worktree_context, thread_wt
