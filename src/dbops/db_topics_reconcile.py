"""dbops.db_topics_reconcile — derive topic state from member task states.

Split out of dbops.db_topics (2026-06-13, LOC gate) when the G1/G4a incident
guards pushed the parent module over the 300-line architecture limit. Holds the
reconcile pass and its safety guards:

* G1 (verified ⟺ merged): a topic only reaches 'verified' when its work is
  merged to main (delegates to db_topics._verified_allowed → graph_guards).
* G4a (no orphan demote): reconcile never demotes a 'running' topic that still
  has a live, healthy bound agent.

Must not own: the topic state machine (db_topics.topic_transition stays the sole
state writer for event-driven transitions), topic CRUD, or completion marking.
``reconcile_topic_state`` writes the derived state via the lockstep state_write
helper (nodes authoritative + legacy graph_topics mirror) — a derive-and-sync, not
an event — and is the only sanctioned writer for that derivation.
"""

from __future__ import annotations

from dbops.schema import _now
from dbops.db_graph import _cx
from dbops.db_topics import get_topic, list_topics
from dbops.state_write import write_state

_FAILED_TASK_STATES = frozenset({
    "failed-exec", "failed-integration", "failed-verify", "blocked-failed"
})
_ACTIVE_TASK_STATES = frozenset({"running", "dispatching", "integrating"})


def _has_live_bound_agent(db, topic: dict) -> bool:
    """G4a: True iff a busy agent is bound to the topic's thread.

    'busy' is the DB's live/healthy signal (get_agent_by_thread filters on it);
    reconcile must not demote a topic out from under such an agent.
    """
    thread_id = topic.get("thread_id")
    if not thread_id:
        return False
    try:
        return db.get_agent_by_thread(thread_id) is not None
    except Exception:
        return False


def reconcile_topic_state(db, topic_id: str) -> str:
    """Derive and sync a topic's state from its member task states.

    Priority: all verified AND (merged OR orphaned-integrating) → 'verified'
    (else 'integrating', G1/G5); any failed → 'failed-verify'; any active
    (running/dispatching/integrating) → 'running'; else leave open/ready
    unchanged (or reset a phantom non-terminal to 'open'). Never demotes a
    'running' topic with a live bound agent (G4a). Never writes a mirror topic
    (reflection-only). Idempotent; safe on terminal topics.
    """
    from dbops.db_topics import _verified_allowed

    topic = get_topic(db, topic_id)
    if topic is None:
        raise ValueError(f"graph topic not found: {topic_id!r}")

    # 'verified' is TERMINAL: never auto-demote a proven-merged topic. This is
    # the idempotency guarantee that previously rode on _orphan_recoverable
    # (removed T-verified-merged-sha) — without it, a verified topic whose
    # repo/branch is gone would derive 'integrating' and flap.
    if topic.get("state") == "verified":
        return "verified"

    with db._connect() as conn:
        rows = conn.execute(
            "SELECT state FROM nodes WHERE kind='task' AND parent_id=?", (topic_id,)
        ).fetchall()

    if not rows:
        return topic["state"]

    task_states = [r[0] for r in rows]

    if all(s == "verified" for s in task_states):
        # G1 single gate (T-verified-merged-sha): verified ⟺ a recorded
        # merged_sha that is an ancestor of main. Tasks 'verified' only means
        # committed-in-worktree; without merge proof the topic stays
        # 'integrating' (pre-verified). The old orphan-recovery bypass (settle
        # an agent-died topic at 'verified' without merge proof) is REMOVED — it
        # was a false-verified hole. An orphan with merged_sha NULL stays
        # 'integrating' (needs-attention), NEVER verified.
        if _verified_allowed(db, topic_id):
            target = "verified"
        else:
            target = "integrating"
    elif any(s in _FAILED_TASK_STATES for s in task_states):
        target = "failed-verify"
    elif any(s in _ACTIVE_TASK_STATES for s in task_states):
        target = "running"
    elif topic["state"] in ("open", "ready"):
        return topic["state"]
    else:
        target = "open"

    # G4a: never demote a 'running' topic that still has a live, healthy bound
    # agent (reconcile must not orphan a working agent). If the derived target
    # is weaker than 'running', keep 'running' until the agent is gone.
    if (
        topic["state"] == "running"
        and target in ("open", "ready")
        and _has_live_bound_agent(db, topic)
    ):
        return "running"

    if target == topic["state"]:
        return target

    # Lockstep writer: nodes (authoritative) + legacy graph_topics together (M3),
    # so the flipped get_topic read never drifts from the legacy mirror.
    with _cx(db) as conn:
        write_state(conn, topic_id, target, now=_now(),
                    verified=(target == "verified"))
    return target


def reconcile_project_topics(db, project_id: str) -> dict:
    """Reconcile all topics in a project from their member task states.

    Returns {topic_id: {"before": old_state, "after": new_state}}.
    """
    topics = list_topics(db, project_id)
    result = {}
    for topic in topics:
        before = topic["state"]
        after = reconcile_topic_state(db, topic["id"])
        result[topic["id"]] = {"before": before, "after": after}
    return result
