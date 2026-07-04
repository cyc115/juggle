"""
juggle_cmd_graph — `juggle project-graph` command handlers (autopilot Phase 1).

Owns: the PR-mode refusal policy (shared with load) and re-exports of the graph
CLI surface (load, parsers, the mutator handlers extracted to
juggle_cmd_graph_ops) so existing ``from juggle_cmd_graph import …`` callers and
test monkeypatches keep working unchanged.
Must not own: pure spec parsing/validation or single-task upsert (extracted to
juggle_graph_upsert), the mutator command handlers (juggle_cmd_graph_ops), task
state semantics (dbops.db_graph), or dispatching.

Spec format (markdown), one `##` section per task:

    ## <task-id>: <Title>
    deps: dep1, dep2              (optional; `- deps:` also accepted)
    <remaining lines = dispatch prompt>
"""

from __future__ import annotations

# get_db kept as the module-level patch surface: juggle_cmd_graph_ops resolves it
# as cg.get_db at call time, and tests monkeypatch cg.get_db.
from juggle_cli_common import get_db  # noqa: F401
# db_graph re-exported: add-task + the atomicity regression pin monkeypatch cg.db_graph.
from dbops import db_graph, db_topics  # noqa: F401

# Re-exported for backward compatibility (tests + callers import these from here).
from juggle_graph_upsert import (  # noqa: F401
    MAX_TASKS,
    VERIFY_CMD_ALLOWLIST,
    find_cycle,
    lint_verify_cmd,
    parse_graph_spec,
    parse_topics_spec,
    validate_graph,
    validate_topics,
)
from juggle_graph_upsert import content_changed as _content_changed  # noqa: F401

# Load handler lives in juggle_graph_load; re-exported for callers/tests.
from juggle_graph_load import cmd_project_graph_load  # noqa: F401


def _is_synthetic_topic(topic_id: str) -> bool:
    """Synthetic single-task topics (migration-37 / flat-spec fallback) are
    named 'T-<task-id>' or 'T#<task-id>'. A project with ONLY synthetic topics
    is treated as a flat graph for add-task (topic optional)."""
    return topic_id.startswith("T-") or topic_id.startswith("T#")


def _git_root(cwd: str) -> str | None:
    """Toplevel of the git repo containing ``cwd``, or None."""
    from vcs import backend_for

    try:
        return backend_for(cwd).repo_root(cwd)
    except Exception:
        return None


def pr_mode_refusal(repo_path: str | None = None) -> str | None:
    """Refusal message when the target repo is push_mode='pr', else None.

    DA round-2 MAJOR-2 (2026-06-10): on PR-mode repos _run_integrate returns
    success after only pushing the branch — the task went 'verified' WITHOUT
    any merge, and dependents were hydrated with "already integrated into
    main" (false). Policy: autopilot (project-graph load / autopilot arm)
    refuses PR-mode repos until verified-means-merged holds for them.
    The target repo is the one the command runs in (worktrees are created
    from it on dispatch).
    """
    import os

    from juggle_settings import get_repo_config

    root = repo_path or _git_root(os.getcwd())
    if not root or get_repo_config(root)["push_mode"] != "pr":
        return None
    return (
        f"repo {root} is configured push_mode='pr' — integrate only pushes "
        "the branch for a PR (no merge into main), so autopilot would mark "
        "tasks 'verified' that are NOT in main and hydrate dependents with a "
        "false 'already integrated' claim. PR-mode repos are not supported "
        "by project autopilot: set push_mode to 'direct' or 'none', or drive "
        "this project without autopilot."
    )


# Parser registration lives in juggle_graph_cli_parsers (2026-07-03 LOC-gate
# extraction); re-exported so `from juggle_cmd_graph import
# register_graph_parsers` (juggle_cli.py) keeps working unchanged.
from juggle_graph_cli_parsers import register_graph_parsers  # noqa: E402,F401


# Mutator command handlers (add-task / reconcile / mark-task) live in
# juggle_cmd_graph_ops (2026-07-03 Phase3 LOC-gate extraction); re-exported so
# `from juggle_cmd_graph import cmd_graph_*` callers and `cg.cmd_graph_*` test
# monkeypatches keep working unchanged. Placed last: the ops module resolves
# cg.get_db / cg.pr_mode_refusal at call time, so cg must be fully defined here.
from juggle_cmd_graph_ops import (  # noqa: E402,F401
    cmd_graph_add_task,
    cmd_graph_mark_task,
    cmd_graph_reconcile,
)

# graph show (READ) — pure-read view, extracted to its own module (Phase4).
from juggle_cmd_graph_show import cmd_graph_show  # noqa: E402,F401
# graph cancel-node (mutator) — own module (Phase5, LOC-gate: graph_ops full).
from juggle_cmd_graph_cancel import cmd_graph_cancel_node  # noqa: E402,F401
