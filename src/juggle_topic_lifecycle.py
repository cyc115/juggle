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
