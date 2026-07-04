"""juggle_graph_add_surfacing — dispatch-topic resolution + conversation
surfacing helpers for `graph add-task`.

Extracted from juggle_graph_add (2026-07-03, architecture-gate LOC budget:
juggle_graph_add was near the 300-line ceiling and needed headroom for
T-fix-dispatch-plan-spec-provision's plan_path/spec_path threading).

Owns: resolving the dispatch-topic a new task attaches to, and binding a
user-named conversation as that topic's surfacing thread.
Must not own: task/edge validation or the add_task transaction (juggle_graph_add).
"""
from __future__ import annotations


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
        if conv is None:
            return
        # Fan-in guard (2026-07-04 dual-dispatch-binding RCA): refuse to bind a
        # conversation that is ALREADY another topic's dispatch thread. Without
        # it, N `add-task --topic <conv>` calls each synthesize a distinct
        # 'T-<id>' topic and bound ALL of them to the one conversation (bind_
        # dispatch_thread dedups only on node_id, never the thread) — graph_tick
        # then reused that single thread per topic, dispatching multiple coders
        # into one worktree (corruption). The FIRST topic keeps the surfacing
        # binding; a later topic is left unbound, so graph_tick mints it a fresh
        # '[T-<id>]' mirror. The owner rebinding itself is still allowed.
        owner = db_topics.get_topic_by_thread(db, conv["id"])
        if owner is not None and owner["id"] != topic_id:
            return
        db_topics.set_topic_thread(db, topic_id, conv["id"])
    except Exception:
        pass  # a bad --topic never fails the already-committed add
