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


# Reverse value-map state -> status. The LIVE thread vocab
# {active,running,closed,archived} is bijective with node state
# {open,running,done,archived} (set_thread_status._VALID_STATES), so the reverse
# is exact for all live data; legacy-only states pass through unchanged.
STATE_TO_STATUS = {"open": "active", "running": "running", "done": "closed",
                   "archived": "archived"}


def status_for_state(state: str) -> str:
    return STATE_TO_STATUS.get(state, state)


# SQL alias-shim (P8 Q1): reverse-map nodes.state back to the legacy
# threads.status vocab so the ~107 consumers that read row['status'] / compare to
# 'active'/'closed'/'archived' keep working untouched after the read-collapse.
# `topic`/`last_active` are pure renames; only `status` needs the value reverse-map.
STATE_AS_STATUS_SQL = (
    "CASE state WHEN 'open' THEN 'active' WHEN 'done' THEN 'closed' "
    "ELSE state END AS status"
)
# Full conversation alias-shim suffix for `SELECT *, <shim>` reads.
CONV_ALIAS_SHIM = f"{STATE_AS_STATUS_SQL}, title AS topic, last_active_at AS last_active"


# Column-name aliases (legacy -> nodes)
TOPIC_COL = "title"          # threads.topic / graph_topics.topic -> nodes.title
PROMPT_COL = "objective"     # graph_tasks.prompt -> nodes.objective
LAST_ACTIVE_COL = "last_active_at"   # added to nodes in Migration 50 (Task 5)
TOPIC_ID_COL = "parent_id"   # graph_tasks.topic_id -> nodes.parent_id

# kind/parent_id discriminators (every read must add one of these)
KIND_CONVERSATION = "kind='conversation'"
KIND_TOPIC = "kind='task' AND parent_id IS NULL"
KIND_TASK = "kind='task' AND parent_id IS NOT NULL"
