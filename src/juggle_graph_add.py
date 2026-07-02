"""
juggle_graph_add — validated, atomic, guarded single-task add into a live graph.

Backs `juggle graph add-task` (juggle_cmd_graph.cmd_graph_add_task): inject ONE
new task task into an EXISTING project graph mid-execution without disarming or
restarting. Split out of juggle_graph_upsert (2026-06-10) to keep both modules
under the 300-line architecture gate.

Owns: the single-task add path (validate_add_task + add_task + the readiness
demotion helper) and its guard rules (which existing-task states a new edge may
touch).
Must not own: task state semantics (dbops.db_graph — task_transition stays the
sole state writer), CLI parsing (juggle_cmd_graph), or whole-spec load
validation (juggle_graph_upsert.validate_graph).
"""

from __future__ import annotations

from dbops import db_graph
from juggle_graph_upsert import find_cycle, lint_verify_cmd


# Mutable states an EXISTING task touched by a new edge (a --required-by target
# gaining a dep, or a re-added --id) is allowed to be in. PROTECTED_STATES are
# refused; --deps targets (pure upstream) may be in ANY state.
MUTABLE_STATES = frozenset(
    {
        "open",
        "ready",
        "failed-exec",
        "failed-integration",
        "failed-verify",
        "blocked-failed",
    }
)


class AddTaskError(ValueError):
    """Validation/guard failure for a single-task add. Carries a clean message;
    the CLI maps it to a nonzero exit with no partial insert."""


def resolve_dispatch_topic(
    db, project_id: str, task_id: str, requested_topic: str | None
) -> tuple[str, bool]:
    """Resolve the graph-topic that will OWN (and thus dispatch) a new task.

    DEFAULT-DISPATCHABLE (2026-06-30 orphan-task dispatch gap): a task must ALWAYS
    live under a kind='topic' node — topics are the watchdog dispatch unit, so a
    parentless task is an undispatchable orphan that silently stalls its project.
    Rules, returning ``(topic_id, auto_create)`` — NEVER ``None``:
      * ``requested_topic`` names a REAL graph-topic → use it verbatim (False).
      * ``requested_topic`` missing, OR names a NON-graph-topic node (e.g. a
        kind='conversation' thread) → synthesize ``'T-<task-id>'`` and flag it
        for auto-create (True). A conversation owner is thus recorded only as the
        human-facing thread; the DISPATCH home is always a graph-topic.
    """
    from dbops import db_topics

    if requested_topic and db_topics.get_topic(db, requested_topic) is not None:
        return requested_topic, False
    return f"T-{task_id}", True


def record_surfacing_conversation(db, topic_id: str, requested_topic) -> None:
    """Bind an existing descriptive conversation as ``topic_id``'s dispatch thread.

    Dedup defect F (2026-07-01): ``add-task --topic <conversation>`` synthesizes a
    'T-<id>' graph-topic home (resolve_dispatch_topic), but the human conversation
    the user named must stay the SINGLE surfacing row — graph_tick reuses this
    binding instead of minting a "[T-<id>]" mirror. ``requested_topic`` (UUID or
    slug) resolving to no conversation is left untouched. Never raises."""
    from dbops import db_topics

    if not requested_topic:
        return
    try:
        conv = db.get_thread(requested_topic) or db.get_thread_by_user_label(
            requested_topic
        )
        if conv is not None:
            db_topics.set_topic_thread(db, topic_id, conv["id"])
    except Exception:
        pass  # a bad --topic never fails the already-committed add


def validate_add_task(
    db,
    project_id: str,
    *,
    task_id: str,
    title: str,
    prompt: str,
    deps: list[str],
    required_by: list[str],
    verify_cmd: str | None,
) -> None:
    """Validate adding one task into the LIVE graph. Raises AddTaskError on the
    first material problem; returns None when the add is legal. No DB writes.

    Checks (against the union of live graph + the new task/edges):
      * empty title / empty prompt
      * verify_cmd lint (same allowlist as load)
      * every --deps id exists (upstream may be in ANY state)
      * every --required-by id exists
      * re-adding an existing --id is allowed ONLY if that task is mutable
      * any --required-by target gaining a dep must be mutable (guard)
      * full cycle check over the resulting edge set (Kahn)
    """
    if not title.strip():
        raise AddTaskError(f"empty title for task {task_id!r}")
    if not prompt.strip():
        raise AddTaskError(f"empty prompt for task {task_id!r}")
    if verify_cmd:
        err = lint_verify_cmd(verify_cmd)
        if err:
            raise AddTaskError(f"task {task_id!r}: {err}")

    live = {n["id"]: n for n in db_graph.list_tasks(db, project_id)}

    # --deps must exist (any state OK — upstream).
    for dep in deps:
        if dep == task_id:
            raise AddTaskError(f"task {task_id!r} cannot depend on itself")
        if dep not in live:
            raise AddTaskError(f"unknown dep {dep!r} for task {task_id!r}")

    # --required-by must exist and be mutable (it gains a new dependency).
    for rb in required_by:
        if rb == task_id:
            raise AddTaskError(f"task {task_id!r} cannot be required by itself")
        if rb not in live:
            raise AddTaskError(f"unknown required-by target {rb!r} for task {task_id!r}")
        if live[rb]["state"] not in MUTABLE_STATES:
            raise AddTaskError(
                f"refusing to add a dependency to {rb!r}: it is "
                f"{live[rb]['state']!r} (protected) — required-by targets must be "
                f"open/ready/failed-*/blocked-failed"
            )

    # Re-adding an existing id: allowed only if that task is mutable.
    if task_id in live and live[task_id]["state"] not in MUTABLE_STATES:
        raise AddTaskError(
            f"task id {task_id!r} already exists in state "
            f"{live[task_id]['state']!r} (protected) — cannot re-add"
        )

    # Full cycle check over the resulting edge set (existing + new).
    all_ids = set(live) | {task_id}
    edges: list[tuple[str, str]] = []
    for n in live:
        if n == task_id:
            continue  # the new task's own edges are rebuilt below
        for d in db_graph.get_deps(db, n):
            edges.append((n, d))
    for d in deps:
        edges.append((task_id, d))
    for rb in required_by:
        edges.append((rb, task_id))
    cyc = find_cycle(sorted(all_ids), edges)
    if cyc:
        raise AddTaskError(f"dependency cycle would form involving: {', '.join(cyc)}")


def add_task(
    db,
    project_id: str,
    *,
    task_id: str,
    title: str,
    prompt: str,
    deps: list[str],
    required_by: list[str],
    verify_cmd: str | None,
    topic_id: str | None = None,
    auto_create_topic: bool = False,
    priority: int = 0,
) -> dict:
    """Validated, atomic, guarded insert of ONE task into a live graph.

    Validates first (validate_add_task — raises AddTaskError on any problem,
    nothing written). Then, in a single transaction: (G5) auto-create the owning
    topic when ``auto_create_topic`` and it doesn't yet exist, upsert the task as
    'open' (re-add resets a mutable existing task via reload), assign its
    ``topic_id`` FK, set its --deps edges, and add a depends_on edge from each
    --required-by target to the new task. Creating the topic + task + FK in the
    SAME transaction closes the empty-topic TOCTOU window (2026-06-13 incident
    defect #1): the tick can never observe a topic that has no member task.
    Commits, then runs the existing readiness recompute so the new task becomes
    'ready' iff all its deps are verified, and any downstream task that now waits
    on the unfinished new task is demoted (recompute_blocked + recompute_ready —
    the sanctioned seams; task_transition stays sole writer).

    Returns {"task_id", "state", "downstream_changed": [{"id","from","to"}]}.
    """
    validate_add_task(
        db, project_id, task_id=task_id, title=title, prompt=prompt,
        deps=deps, required_by=required_by, verify_cmd=verify_cmd,
    )

    live = {n["id"]: n for n in db_graph.list_tasks(db, project_id)}
    before = {n["id"]: n["state"] for n in live.values()}

    # One transaction for the whole insert INCLUDING the downstream demotion.
    # The demotion must be atomic with the edge write (not a post-commit step):
    # the dispatcher claims any task in state='ready' without re-checking deps,
    # so a crash between commit and a post-commit demote could leave a 'ready'
    # task with an unverified dep and dispatch it prematurely.
    conn = db._connect()
    try:
        # G5: create the owning topic FIRST, in this same transaction, so a topic
        # never exists without its first task (no empty-topic dispatch window).
        if auto_create_topic and topic_id:
            from dbops import db_topics
            if db_topics.get_topic(db, topic_id, conn=conn) is None:
                db_topics.create_topic(
                    db, topic_id=topic_id, project_id=project_id, title=title,
                    priority=priority, conn=conn,
                )
        if task_id in live:
            # Re-add of a mutable existing task: reset content + state to open.
            db_graph.update_task_content(
                db, task_id, title=title, prompt=prompt, verify_cmd=verify_cmd,
                conn=conn,
            )
            if live[task_id]["state"] != "open":
                db_graph.task_transition(db, task_id, "reload", conn=conn)
        else:
            db_graph.create_task(
                db, task_id=task_id, project_id=project_id, title=title,
                prompt=prompt, verify_cmd=verify_cmd, priority=priority, conn=conn,
            )
        if topic_id:
            db_graph.set_task_topic(db, task_id, topic_id, conn=conn)
        db_graph.replace_edges(db, task_id, sorted(deps), conn=conn)

        # Downstream inserts: each --required-by target gains a dep on task_id.
        # A target that was 'ready' now has an unverified dep → demote it to
        # 'open' in the SAME transaction (sole writer, 'unready' event).
        for rb in required_by:
            new_deps = sorted(set(db_graph.get_deps(db, rb)) | {task_id})
            db_graph.replace_edges(db, rb, new_deps, conn=conn)
            if live[rb]["state"] == "ready":
                db_graph.task_transition(db, rb, "unready", conn=conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Promote the new task (and resolve any blocked tail) through the existing
    # seams. Promotion lagging a crash is safe — the task merely waits a tick.
    db_graph.recompute_blocked(db, project_id)
    db_graph.recompute_ready(db, project_id)

    after = {n["id"]: n["state"] for n in db_graph.list_tasks(db, project_id)}
    downstream_changed = [
        {"id": nid, "from": before[nid], "to": after[nid]}
        for nid in before
        if nid != task_id and before[nid] != after[nid]
    ]
    return {
        "task_id": task_id,
        "state": after.get(task_id, "open"),
        "downstream_changed": downstream_changed,
    }
