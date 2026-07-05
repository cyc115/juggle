"""juggle_loop_dispatch — cross-topic vault-handoff run-dir injection (loop-entity
V2 §1.5).

A loop whose steps span differing ``(role, delivery)`` topics must pass a payload
(e.g. a research report) from an upstream topic A to a downstream topic B. Learnings
and handoffs are capped ≤300 chars at the write surface and worktrees are isolated, so
the payload goes to a shared store (the vault) — the agent writes it to a run-scoped
path and records a POINTER (not the payload) in its handoff (§1.1/§1.5).

This module owns the DISPATCH-side seam: for a node belonging to a ``kind='loop'``
project, resolve the loop's run-output directory and inject it into the node prompt.
The base is resolved through the config-sourced ``get_loop_output_root`` at THIS
boundary (§1.6 — never a hardcoded vault literal); the path itself is the PURE
``loop_run_dir`` helper. A non-loop node gets NO injection.

Kept OUT of the fire/cap path (juggle_loop_fire): the run dir is injected where the
graph tick actually dispatches a topic (juggle_graph_dispatch._dispatch_via_pool), so
firing/budgeting policy and the handoff seam stay independent.
"""
from __future__ import annotations

from pathlib import Path

from juggle_vault_paths import get_loop_output_root, loop_run_dir


def loop_run_dir_for_node(db, node: dict | None) -> Path | None:
    """Resolve this node's loop run-output path, or None for a NON-loop node.

    The base is resolved ONCE here through the config-sourced resolver
    (``get_loop_output_root`` — the ONLY vault-root read, §1.6); the path is the pure
    ``loop_run_dir(base, loop_id, run_seq, topic_id)``. With the stable-topic model
    (§0b) ``topic_id`` (the node id) is stable across runs, so ``run_seq`` disambiguates
    each fire's directory."""
    if not node:
        return None
    project_id = node.get("project_id")
    topic_id = node.get("id")
    if not project_id or not topic_id:
        return None
    loop = db.get_loop_by_project(project_id)
    if not loop:
        return None  # a non-loop project node — no handoff dir
    base = get_loop_output_root()  # config boundary — resolved once, passed in as base
    return loop_run_dir(base, loop["id"], str(loop["run_seq"]), topic_id)


def build_loop_run_injection(db, node: dict | None) -> str:
    """Prompt section telling a LOOP topic where to write its cross-topic handoff
    payload, and to record a POINTER (not the payload) in its handoff (§1.5). Returns
    ``""`` for a non-loop node (no injection)."""
    path = loop_run_dir_for_node(db, node)
    if path is None:
        return ""
    return (
        "## Loop run output directory (cross-topic handoff)\n"
        "This topic runs inside a recurring loop. If a DOWNSTREAM topic needs a payload "
        "from you (a report, dataset, or artifact), write it to this run-scoped path:\n"
        f"  {path}\n"
        "Then record a POINTER to it in your **handoff** (e.g. "
        f'"output written to {path} — covers X/Y/Z"), NOT the payload itself — handoffs '
        "and learnings are capped and must stay small. A downstream topic reads your "
        "handoff pointer and opens the file.\n\n---\n\n"
    )


def inject_loop_run_dir(db, node: dict | None, prompt):
    """Prepend the loop run-dir injection (§1.5) to ``prompt`` and return it. A non-loop
    node returns ``prompt`` unchanged (empty injection).

    ``prompt`` is EITHER a plain ``str`` (ad-hoc / self-heal repair dispatch) OR a
    ``TopicPromptPayload`` (the graph tick's topic dispatch, whose free-text narrative
    lives in ``.context``). The injection is a narrative — for a payload it prepends to
    ``.context`` (a frozen-dataclass ``replace``), never string-concatenated onto the
    payload object."""
    injection = build_loop_run_injection(db, node)
    if not injection:
        return prompt
    from juggle_prompt_context import TopicPromptPayload

    if isinstance(prompt, TopicPromptPayload):
        from dataclasses import replace

        return replace(prompt, context=injection + (prompt.context or ""))
    return injection + prompt
