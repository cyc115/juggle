"""juggle_graph_hydration — pure dispatch-prompt builder for graph tasks.

Owns: ``build_hydration`` (DA M4: dep handoffs + pre-merge diffstat + project
objective + task prompt — NEVER ``thread.summary``) and the DB-reading wrapper
``hydrate_for_task``.
Must not own: claiming/dispatching (juggle_graph_dispatch) or task state
semantics (dbops.db_graph).

Extracted mechanically from juggle_graph_dispatch.py (2026-06-10, LOC gate);
juggle_graph_dispatch re-exports both names so existing imports keep working.
"""

from __future__ import annotations

from dbops import db_graph


def build_hydration(objective: str, task: dict, deps: list[dict]) -> str:
    """Build a dispatch prompt for ``task`` from its plan + upstream handoffs.

    Pure function. Inputs: project ``objective``, the task row, and dep rows
    ({id,title,handoff,diffstat}). Uses ONLY dep handoffs + the pre-merge
    diffstat integrate captured (autopilot Phase 3) + objective + the task's
    planned prompt — never thread.summary (80-char truncated junk, DA M4).
    """
    parts = [f"# Graph task {task['id']}: {task['title']}"]
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
    parts.append(f"## Task\n{task['prompt']}")
    if task.get("verify_cmd"):
        parts.append(
            f"Machine verification (runs pre-merge): `{task['verify_cmd']}`"
        )
    # Verify-fallback (self-heal): a FRESH agent re-dispatched after a prior
    # verify_cmd failure gets the prior failure output so it can fix the root
    # cause rather than repeat it. The real verify_cmd re-runs pre-merge as usual.
    prior = (task.get("verify_failure") or "").strip()
    if prior:
        parts.append(
            "## Previous attempt: verify_cmd FAILED — fix the root cause\n"
            "A prior agent's changes did not pass the machine verification below. "
            "You have fresh context; diagnose and fix what made it red:\n"
            f"```\n{prior}\n```"
        )
    parts.append(
        "## Completion contract\n"
        "When done, complete with:\n"
        f"`juggle agent complete <thread> \"<summary>\" --handoff '<contract>'`\n"
        "The handoff (files touched, interfaces added/changed, key decisions, "
        "follow-ups) is REQUIRED — dependent tasks are hydrated from it."
    )
    return "\n\n".join(parts)


def hydrate_for_task(db, project_id: str, task: dict) -> str:
    project = db.get_project(project_id) or {}
    deps = [
        d
        for dep_id in db_graph.get_deps(db, task["id"])
        if (d := db_graph.get_task(db, dep_id)) is not None
    ]
    return build_hydration(project.get("objective") or "", task, deps)


def build_topic_hydration(objective: str, topic: dict, deps: list[dict],
                          tasks: list[dict]) -> str:
    """Dispatch prompt for a TOPIC (R9 hybrid): project objective + dep-topic
    handoffs (+ diffstat) + the topic objective + the SEQUENTIAL task list with
    the per-task TDD/commit/mark-task contract. Pure; never thread.summary."""
    parts = []
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
            "## Upstream topic handoffs (verified dependencies, already "
            "integrated into main)\n" + "\n".join(chunks)
        )
    parts.append(f"## Topic {topic['id']}: {topic['title']}\n"
                 f"{(topic.get('objective') or '').strip()}")
    rows = []
    for n in tasks:
        flag = " [VERIFIED — skip]" if n.get("state") == "verified" else ""
        vc = f"\nverify_cmd: {n['verify_cmd']}" if n.get("verify_cmd") else ""
        mark = (
            f"\nMark: `juggle graph mark-task {n['id']} --handoff '<files "
            f"touched, interfaces changed, key decisions>'` (or `--fail` if "
            f"you must give up on this task)."
        )
        rows.append(f"### {n['id']} — {n['title']}{flag}{vc}\n{n['prompt']}{mark}")
    parts.append(
        "## Tasks — execute SEQUENTIALLY, in this order\n"
        "Per task: TDD (failing test first) → make it pass → run its "
        "verify_cmd → COMMIT → mark it with the task's own `Mark:` command "
        "below. Tasks flagged VERIFIED: skip them.\n\n"
        + "\n\n".join(rows)
    )
    parts.append(
        "## Finish\nWhen EVERY task above is marked, run "
        "`juggle agent complete <thread> \"<summary>\"` — integrate runs ONCE "
        "for the whole topic. agent complete REFUSES while tasks are unmarked."
    )
    return "\n\n".join(parts)


def build_source_of_truth_section(topic: dict | None) -> str:
    """'## Source of truth (READ FIRST)' section (T-fix-dispatch-plan-spec-
    provision, 2026-07-03): tells a dispatched agent which plan/spec file its
    tasks implement — the GAP this fixes is that plan/spec references never
    reached dispatch prompts (they lived only in the file-level spec preamble,
    which the loader discarded). Pure; returns "" when the topic has neither
    path (self-contained fix tasks) so no dangling section is emitted."""
    if not topic:
        return ""
    plan_path = (topic.get("plan_path") or "").strip()
    spec_path = (topic.get("spec_path") or "").strip()
    lines = []
    if plan_path:
        lines.append(
            f"{len(lines) + 1}. Read {plan_path} IN FULL — your tasks implement "
            "its sections; follow its files/seams/pins exactly."
        )
    if spec_path:
        lines.append(f"{len(lines) + 1}. Consult {spec_path} when intent is unclear.")
    if not lines:
        return ""
    return "## Source of truth (READ FIRST)\n" + "\n".join(lines) + "\n\n---\n\n"


def topic_source_of_truth_for_thread(db, thread_id: str | None) -> str:
    """DB wrapper: the bound topic's Source-of-truth section, or "" if the
    thread has no topic (bare/conversation threads). Best-effort — never
    breaks dispatch (matches the ledger-write convention in dispatch_core)."""
    if not thread_id:
        return ""
    from dbops import db_topics

    try:
        topic = db_topics.get_topic_by_thread(db, thread_id)
    except Exception:
        return ""
    return build_source_of_truth_section(topic)


def hydrate_for_topic(db, project_id: str, topic: dict) -> str:
    """DB wrapper: dep-topic rows + topo-ordered tasks → build_topic_hydration."""
    from dbops import db_topics

    project = db.get_project(project_id) or {}
    deps = [db_topics.get_topic(db, t)
            for t in db_topics.derived_topic_deps(db, topic["id"])]
    tasks = db_topics.list_topic_tasks(db, topic["id"])
    return build_topic_hydration(project.get("objective") or "", topic,
                                 [d for d in deps if d], tasks)
