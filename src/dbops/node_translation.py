"""Single source of truth for legacy(threads/graph_*) -> nodes translation.
Used by every P8 read-collapse site so the value map is defined exactly once."""
from __future__ import annotations

STATUS_TO_STATE = {
    "active": "open", "closed": "done", "background": "running",
    "running": "running", "failed": "failed-exec", "done": "done",
    "archived": "archived",
}


def state_for_status(status: str) -> str:
    return STATUS_TO_STATE[status]   # fail-loud on unknown status


# Column-name aliases (legacy -> nodes)
TOPIC_COL = "title"          # threads.topic / graph_topics.topic -> nodes.title
PROMPT_COL = "objective"     # graph_tasks.prompt -> nodes.objective
LAST_ACTIVE_COL = "last_active_at"   # added to nodes in Migration 50 (Task 5)
TOPIC_ID_COL = "parent_id"   # graph_tasks.topic_id -> nodes.parent_id

# kind/parent_id discriminators (every read must add one of these)
KIND_CONVERSATION = "kind='conversation'"
KIND_TOPIC = "kind='task' AND parent_id IS NULL"
KIND_TASK = "kind='task' AND parent_id IS NOT NULL"
