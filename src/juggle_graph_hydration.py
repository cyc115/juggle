"""juggle_graph_hydration — pure dispatch-prompt builder for graph tasks.

Owns: ``build_hydration``/``build_topic_hydration`` (DA M4: dep handoffs +
pre-merge diffstat + project objective + task prompt(s) — NEVER
``thread.summary``) and the DB-reading wrappers ``hydrate_for_task`` /
``hydrate_for_topic``. Both builders return a ``TopicPromptPayload`` (PC2,
Agent Prompt Contract v2): the TASK-section narrative (objective + upstream
handoffs) plus resolved ``TaskSpec`` rows. The Lifecycle/Finish contract
(mark-task, finalize) is NOT rendered here — juggle_dispatch_core's
send_task_to_agent renders it once, centrally, via render_agent_prompt, using
context (thread/workspace/branch/vcs/profile) this pure module doesn't have.
Must not own: claiming/dispatching (juggle_graph_dispatch), task state
semantics (dbops.db_graph), or the Lifecycle/Guardrails render
(juggle_prompt_context).

Extracted mechanically from juggle_graph_dispatch.py (2026-06-10, LOC gate);
juggle_graph_dispatch re-exports both names so existing imports keep working.
"""

from __future__ import annotations

import json

from dbops import db_graph
from juggle_prompt_context import TaskSpec, TopicPromptPayload

# ── irl-repair T4: class playbooks ──────────────────────────────────────────
# Per-class repair instructions a watchdog-dispatched coder follows to clear a
# failed integrate in its PRESERVED worktree. Keyed by the fail_class the
# envelope carries (juggle_integrate_envelope.classify). `machinery` is absent
# on purpose — it is never auto-repaired (a pipeline bug is not the topic's
# fault; it escalates to the orchestrator instead).
_REPAIR_PLAYBOOKS: dict[str, str] = {
    "conflict": (
        "This branch failed to integrate on a REBASE CONFLICT. Reconcile it:\n"
        "1. `git fetch` and rebase your branch onto the latest trunk.\n"
        "2. Resolve the conflicts in the files listed in the envelope (keep BOTH "
        "sides' intent — the other side already landed on trunk).\n"
        "3. Commit the resolution, run the FULL test suite green, then re-run "
        "`juggle agent complete` so the watchdog re-integrates."
    ),
    "divergence": (
        "This branch failed to integrate because trunk MOVED (ff-merge refused — "
        "the branch is behind). Reconcile it:\n"
        "1. `git fetch` and rebase your branch onto the latest trunk.\n"
        "2. Resolve any conflicts in the listed files, keeping both sides' intent.\n"
        "3. Commit, run the FULL suite green, then re-run `juggle agent complete` "
        "so the watchdog re-integrates."
    ),
    "red-suite": (
        "This branch's work is committed but the FULL test suite went RED at "
        "integrate — most likely a SEMANTIC collision with work that landed on "
        "trunk after you branched. Diagnose it:\n"
        "1. Rebase onto the latest trunk so you see the collision.\n"
        "2. Read the failing tests in the envelope's log_tail, find the root "
        "cause, and fix the code (do NOT weaken or delete the tests).\n"
        "3. Get the FULL suite green, commit, then re-run `juggle agent complete`."
    ),
    "collision": (
        "This branch failed to integrate on a WORKTREE COLLISION (uncommitted / "
        "untracked files clashed with the merge). Reconcile the named paths by "
        "CONTENT:\n"
        "1. For each path in the envelope, diff your version against trunk's.\n"
        "2. **NEVER delete non-identical work** — if two versions differ, merge "
        "their intent; deleting the other side is data loss (incident cyc_CB, "
        "2026-07-03). Only drop a file when it is byte-identical to trunk's.\n"
        "3. Commit the reconciled tree, run the FULL suite green, then re-run "
        "`juggle agent complete` so the watchdog re-integrates."
    ),
}


def build_repair_prompt(envelope: dict) -> str:
    """Dispatch prompt for a watchdog repair agent (irl-repair T4).

    Pure. Selects the class playbook and embeds the full fail envelope JSON so
    the agent sees the conflicting files / failing-test log_tail / branch /
    worktree / attempt history. Falls back to the conflict playbook for an
    unknown class (fail-safe: rebase+resolve+rerun is the least-destructive
    recovery). The worktree is ALREADY preserved on the bound thread, so the
    prompt never tells the agent to create one — it works in place.
    """
    fail_class = (envelope or {}).get("class", "conflict")
    playbook = _REPAIR_PLAYBOOKS.get(fail_class, _REPAIR_PLAYBOOKS["conflict"])
    envelope_json = json.dumps(envelope or {}, indent=2, sort_keys=True)
    return (
        f"# Repair task — integrate failed ({fail_class})\n"
        "A prior integrate of this topic's branch was refused. You are dispatched "
        "into the SAME preserved worktree to fix it — do not start fresh, do not "
        "create a new branch.\n\n"
        f"## Playbook\n{playbook}\n\n"
        f"## Failure envelope\n```json\n{envelope_json}\n```"
    )


def _context_narrative(objective: str, deps: list[dict], *, heading: str) -> str | None:
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
            f"## Upstream {heading} (verified dependencies, already "
            "integrated into main)\n" + "\n\n".join(chunks)
        )
    return "\n\n".join(parts) or None


def build_hydration(objective: str, task: dict, deps: list[dict]) -> TopicPromptPayload:
    """Build the TASK-section payload for a single graph ``task``.

    Pure function. Inputs: project ``objective``, the task row, and dep rows
    ({id,title,handoff,diffstat}). Uses ONLY dep handoffs + the pre-merge
    diffstat integrate captured (autopilot Phase 3) + objective + the task's
    planned prompt — never thread.summary (80-char truncated junk, DA M4).
    """
    context = _context_narrative(objective, deps, heading="handoffs")

    body_parts = [task["prompt"]]
    # Verify-fallback (self-heal): a FRESH agent re-dispatched after a prior
    # verify_cmd failure gets the prior failure output so it can fix the root
    # cause rather than repeat it. The real verify_cmd re-runs pre-merge as usual.
    prior = (task.get("verify_failure") or "").strip()
    if prior:
        body_parts.append(
            "## Previous attempt: verify_cmd FAILED — fix the root cause\n"
            "A prior agent's changes did not pass the machine verification below. "
            "You have fresh context; diagnose and fix what made it red:\n"
            f"```\n{prior}\n```"
        )
    task_spec = TaskSpec(
        id=task["id"], title=task["title"],
        verify_cmd=task.get("verify_cmd") or "",
        body="\n\n".join(body_parts),
    )
    return TopicPromptPayload(context=context, tasks=(task_spec,))


def hydrate_for_task(db, project_id: str, task: dict) -> TopicPromptPayload:
    project = db.get_project(project_id) or {}
    deps = [
        d
        for dep_id in db_graph.get_deps(db, task["id"])
        if (d := db_graph.get_task(db, dep_id)) is not None
    ]
    return build_hydration(project.get("objective") or "", task, deps)


def build_topic_hydration(objective: str, topic: dict, deps: list[dict],
                          tasks: list[dict]) -> TopicPromptPayload:
    """TASK-section payload for a TOPIC (R9 hybrid): project objective +
    dep-topic handoffs (+ diffstat) + the topic objective as ``context``, and
    the SEQUENTIAL task list as resolved ``TaskSpec`` rows (VERIFIED tasks
    flagged, not dropped). Pure; never thread.summary."""
    narrative = _context_narrative(objective, deps, heading="topic handoffs")
    context = "\n\n".join(p for p in (
        narrative,
        f"## Topic {topic['id']}: {topic['title']}\n"
        f"{(topic.get('objective') or '').strip()}",
    ) if p)

    task_specs = tuple(
        TaskSpec(
            id=n["id"], title=n["title"],
            verify_cmd=n.get("verify_cmd") or "",
            body=n["prompt"],
            verified=(n.get("state") == "verified"),
        )
        for n in tasks
    )
    return TopicPromptPayload(context=context, tasks=task_specs)


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


def hydrate_for_topic(db, project_id: str, topic: dict) -> TopicPromptPayload:
    """DB wrapper: dep-topic rows + topo-ordered tasks → build_topic_hydration."""
    from dbops import db_topics

    project = db.get_project(project_id) or {}
    deps = [db_topics.get_topic(db, t)
            for t in db_topics.derived_topic_deps(db, topic["id"])]
    tasks = db_topics.list_topic_tasks(db, topic["id"])
    return build_topic_hydration(project.get("objective") or "", topic,
                                 [d for d in deps if d], tasks)
