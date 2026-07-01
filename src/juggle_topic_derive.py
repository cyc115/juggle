"""juggle_topic_derive — pure derivation of a conversation-topic state from its
child task states (2026-06-30 topic-graph-state-unify R2). No DB, no imports of
graph/db modules — the single testable seam for the close rule."""
from __future__ import annotations

_ACTIVE = frozenset(
    {"open", "ready", "dispatching", "running", "integrating", "background"}
)
_MERGED_TERMINAL = frozenset({"verified", "done"})
_FAILED = frozenset(
    {"failed-exec", "failed-integration", "failed-verify", "blocked-failed"}
)


def derive_topic_state(
    child_states: list[str],
    *,
    minutes_since_human_msg: float | None,
    close_idle_min: int,
) -> str | None:
    """Derived conversation-topic state, or None to leave unchanged.

    None  → no children (childless human-facing topic keeps manual-close).
    open  → work in flight, a failed child, or merged-but-recently-active
            (idle guard / reopen).
    done  → every child merged-terminal AND no human message for
            >= close_idle_min.
    """
    if not child_states:
        return None
    if any(s in _ACTIVE for s in child_states):
        return "open"
    if any(s in _FAILED for s in child_states):
        return "open"
    if all(s in _MERGED_TERMINAL for s in child_states):
        if minutes_since_human_msg is None or minutes_since_human_msg >= close_idle_min:
            return "done"
        return "open"
    return "open"
