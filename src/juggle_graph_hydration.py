"""juggle_graph_hydration — pure dispatch-prompt builder for graph nodes.

Owns: ``build_hydration`` (DA M4: dep handoffs + pre-merge diffstat + project
objective + node prompt — NEVER ``thread.summary``) and the DB-reading wrapper
``hydrate_for_node``.
Must not own: claiming/dispatching (juggle_graph_dispatch) or node state
semantics (dbops.db_graph).

Extracted mechanically from juggle_graph_dispatch.py (2026-06-10, LOC gate);
juggle_graph_dispatch re-exports both names so existing imports keep working.
"""

from __future__ import annotations

from dbops import db_graph


def build_hydration(objective: str, node: dict, deps: list[dict]) -> str:
    """Build a dispatch prompt for ``node`` from its plan + upstream handoffs.

    Pure function. Inputs: project ``objective``, the node row, and dep rows
    ({id,title,handoff,diffstat}). Uses ONLY dep handoffs + the pre-merge
    diffstat integrate captured (autopilot Phase 3) + objective + the node's
    planned prompt — never thread.summary (80-char truncated junk, DA M4).
    """
    parts = [f"# Graph node {node['id']}: {node['title']}"]
    if (objective or "").strip():
        parts.append(f"## Project objective\n{objective.strip()}")
    if deps:
        chunks = []
        for d in deps:
            handoff = (d.get("handoff") or "").strip() or "(no handoff recorded)"
            diffstat = (d.get("diffstat") or "").strip()
            if diffstat:
                handoff += f"\nIntegrated diffstat:\n{diffstat}"
            chunks.append(f"### {d['id']} — {d['title']}\n{handoff}")
        parts.append(
            "## Upstream handoffs (verified dependencies, already integrated "
            "into main)\n" + "\n\n".join(chunks)
        )
    parts.append(f"## Task\n{node['prompt']}")
    if node.get("verify_cmd"):
        parts.append(
            f"Machine verification (runs pre-merge): `{node['verify_cmd']}`"
        )
    parts.append(
        "## Completion contract\n"
        "When done, complete with:\n"
        f"`juggle complete-agent <thread> \"<summary>\" --handoff '<contract>'`\n"
        "The handoff (files touched, interfaces added/changed, key decisions, "
        "follow-ups) is REQUIRED — dependent nodes are hydrated from it."
    )
    return "\n\n".join(parts)


def hydrate_for_node(db, project_id: str, node: dict) -> str:
    project = db.get_project(project_id) or {}
    deps = [
        d
        for dep_id in db_graph.get_deps(db, node["id"])
        if (d := db_graph.get_node(db, dep_id)) is not None
    ]
    return build_hydration(project.get("objective") or "", node, deps)
