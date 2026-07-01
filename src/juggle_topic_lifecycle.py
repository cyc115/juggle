"""juggle_topic_lifecycle — conversation-topic lifecycle decisions.

Owns the close/preserve decision extracted from juggle_cmd_agents_complete
(2026-06-30 topic-graph-state-unify R1), plus the forward-link hook
(ensure_topic_child, F1). Pure decision helpers here take a db for reads only.
"""
from __future__ import annotations


def decide_thread_close(db, thread: dict, thread_uuid: str) -> str | None:
    """Status to write on agent-complete, or None to leave untouched.

    Preserves the 2026-06-21 anti-hijack fix: a feature topic (>=1 human message)
    is never force-closed; an in-flight wrongful bind is un-hijacked to 'active';
    an already-terminal preserved topic is left as-is (idempotency, Codex 2026-06-21).
    """
    if not db.has_human_user_message(thread_uuid):
        return "closed"
    if (thread.get("state") or "") in ("background", "running"):
        return "active"
    return None


def ensure_topic_child(
    db,
    *,
    topic_id: str,
    agent_thread_id: str,
    prompt: str,
    verify_cmd: str | None = None,
) -> str:
    """Idempotent forward-link: ensure a child kind='task' node parented to
    ``topic_id`` carries the dispatched work (2026-06-30 topic-graph-state-unify F1).

    Three paths, all idempotent:
      1. Graph-first — the agent thread is already bound to a real project task:
         reparent that task onto the topic (NO new node), return its id.
      2. Re-dispatch — a synthetic child for this (topic, agent-thread) already
         exists: reparent (no-op) and return it.
      3. Ad-hoc — create ONE synthetic child (project INBOX so graph_tick never
         auto-dispatches it, OQ-3), bind it to the agent thread, and drive it
         open→ready→dispatching→running so the EXISTING completion path
         (mark_graph_task → mark_completion) marks it terminal.
    Returns the child task id.
    """
    from dbops import db_graph

    existing = db_graph.get_task_by_thread(db, agent_thread_id)
    if existing is not None:
        db_graph.set_task_topic(db, existing["id"], topic_id)
        return existing["id"]

    task_id = f"conv-{topic_id[:8]}-{agent_thread_id[:8]}"
    if db_graph.get_task(db, task_id) is not None:
        db_graph.set_task_topic(db, task_id, topic_id)
        return task_id

    db_graph.create_task(
        db,
        task_id=task_id,
        project_id="INBOX",
        title=f"work for {topic_id[:8]}",
        prompt=prompt,
        verify_cmd=verify_cmd,
    )
    db_graph.set_task_topic(db, task_id, topic_id)
    db_graph.set_task_thread(db, task_id, agent_thread_id)
    # Drive the machine to 'running' so completion can mark it verified.
    for event in ("deps_ready", "claim", "dispatch"):
        db_graph.task_transition(db, task_id, event)
    return task_id
