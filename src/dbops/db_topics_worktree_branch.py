"""dbops.db_topics_worktree_branch — record a topic's OWN dispatch branch.

Extracted from dbops.db_topics (2026-07-03, LOC gate: db_topics.py was
already at its grandfathered budget) for T-fix-backfill-sha-misattribution.

A dispatch thread is not exclusively owned by one topic — dispatch_edge only
enforces "a node binds exactly one thread", not the reverse, so a
conversation thread can be (and in production was) reused as the dispatch
home for a second, unrelated topic. Reading a branch to prove a topic's own
commits landed off the bound thread's live ``worktree_branch`` therefore
proves nothing once that thread is reused: it reflects whichever topic
dispatched through it MOST RECENTLY. ``nodes.worktree_branch`` is instead
each topic's own, write-once record, stamped at worktree creation
(``juggle_cmd_agents_worktree._create_worktree``) — the only source the
merged-sha backfill/reconcile self-heal (``dbops.db_topics_reconcile``) and
the orphan guard (``dbops.orphan_guard``) may trust as proof.
"""

from __future__ import annotations

from dbops.schema import _now
from dbops.db_graph import _cx


def set_topic_worktree_branch(db, topic_id, branch, conn=None) -> None:
    """No-op if ``branch`` is falsy or the topic already has one recorded —
    write-once: a topic's own branch identity, once set, is never repointed."""
    if not branch:
        return
    now = _now()
    with _cx(db, conn) as c:
        c.execute(
            "UPDATE nodes SET worktree_branch=?, updated_at=? "
            "WHERE id=? AND kind='topic' AND (worktree_branch IS NULL OR worktree_branch='')",
            (branch, now, topic_id),
        )
