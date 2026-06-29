"""Single source of truth for legacy(threads/graph_*) -> nodes translation.
Used by every P8 read-collapse site so the value map is defined exactly once."""
from __future__ import annotations

STATUS_TO_STATE = {
    "active": "open", "closed": "done", "background": "background",
    "running": "running", "failed": "failed-exec", "done": "done",
    "archived": "archived",
}


def state_for_status(status: str) -> str:
    return STATUS_TO_STATE[status]   # fail-loud on unknown status


# Reverse value-map state -> status. BIJECTIVE over the LIVE thread vocab
# {active,background,running,closed,archived} <-> node state
# {open,background,running,done,archived} (set_thread_status._VALID_STATES).
# `background` stays first-class (R2-1) — NOT collapsed to 'running' — so the
# watchdog reaper + the two cockpit panels (2a Running / 2b Background) stay
# correct. Legacy-only states pass through unchanged.
STATE_TO_STATUS = {"open": "active", "background": "background",
                   "running": "running", "done": "closed",
                   "archived": "archived"}


def status_for_state(state: str) -> str:
    return STATE_TO_STATUS.get(state, state)


# SQL reverse-map (P8 Q1): re-express nodes.state back to the legacy
# threads.status vocab for any SELECT that still needs it during the conversation
# read-collapse. The permanent CONV_ALIAS_SHIM was deleted in Step 3 (H2) —
# consumers adopt state/title/last_active_at directly; no permanent re-aliasing.
# GENERATED from STATE_TO_STATUS (H1) so the dict and the SQL can never diverge —
# editing the dict is the SOLE place the value map is defined.
def _build_state_as_status_sql() -> str:
    whens = " ".join(f"WHEN '{s}' THEN '{st}'" for s, st in STATE_TO_STATUS.items())
    return f"CASE state {whens} ELSE state END AS status"


STATE_AS_STATUS_SQL = _build_state_as_status_sql()


# Column-name aliases (legacy -> nodes)
TOPIC_COL = "title"          # threads.topic / graph_topics.topic -> nodes.title
PROMPT_COL = "objective"     # graph_tasks.prompt -> nodes.objective
LAST_ACTIVE_COL = "last_active_at"   # added to nodes in Migration 50 (Task 5)
TOPIC_ID_COL = "parent_id"   # graph_tasks.topic_id -> nodes.parent_id

# kind discriminators (every read must add one of these). P8 M2 (Migration 53):
# topics are their own kind='topic' node, so a topic is no longer a parent-less
# kind='task' row — the discriminator is the kind alone.
KIND_CONVERSATION = "kind='conversation'"
KIND_TOPIC = "kind='topic'"
KIND_TASK = "kind='task'"
