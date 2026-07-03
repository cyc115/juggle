"""juggle_stack_base — stack-relative base ref resolution (SPEC §4).

Owns: ``stack_base(db, topic_id, repo, backend) -> Rev``, the single function
that resolves what a topic's workspace/rebase should fork from, given its
DERIVED topic-level deps (``db_topics.derived_topic_deps``):
  - 0 unlanded deps  -> ``backend.trunk(repo)`` (the H2 fix: the workspace now
    forks trunk, never the caller's implicit local HEAD).
  - 1 unlanded dep   -> that dep topic's ``submitted_rev`` (stack on its tip).
  - >=2 unlanded deps -> fail loud (v1 limitation): file a ``manual_step``
    action item and raise ``TooManyUnlandedDeps``.

Threaded through BOTH insertion points — dispatch's
``create_workspace(base=...)`` and integrate's rebase/update_to target — so
dispatch/integrate stay backend-agnostic (SPEC §7.2): this module is the only
place with dep-topic base-ref policy.

Must not own: dep-edge storage (dbops.db_topics), the merged/verified gate
(dbops.graph_guards), or VCS mechanics (vcs.py backends).
"""
from __future__ import annotations

from dbops import db_topics
from dbops.graph_guards import topic_is_merged


class TooManyUnlandedDeps(RuntimeError):
    """>=2 unlanded dependency topics — v1 does not support multi-dep
    stacking; a human must sequence or land the deps manually (SPEC §4)."""


def topic_id_for_thread(db, thread_id: str) -> str | None:
    """The topic bound to ``thread_id`` via the dispatch edge, or None
    (pre-migration DB / Mock db in tests, or a thread outside the topic/
    task-DAG flow) — fail-soft so callers can treat it as "no topic"."""
    try:
        topic = db_topics.get_topic_by_thread(db, thread_id)
    except Exception:
        return None
    return topic["id"] if topic else None


def stack_base(db, topic_id: str, repo: str, backend):
    """Resolve the Rev ``topic_id``'s workspace/rebase should be based on."""
    dep_ids = db_topics.derived_topic_deps(db, topic_id)
    unlanded = [d for d in dep_ids if not topic_is_merged(db, d)]

    if not unlanded:
        return backend.trunk(repo)

    if len(unlanded) == 1:
        dep = db_topics.get_topic(db, unlanded[0]) or {}
        rev = (dep.get("submitted_rev") or "").strip()
        if rev:
            return rev
        return backend.trunk(repo)

    db.add_action_item(
        thread_id=None,
        message=(
            f"⚠️ topic {topic_id} has {len(unlanded)} unlanded dependency "
            f"topics ({', '.join(unlanded)}) — stacking on 2+ unlanded deps "
            f"is not supported (v1 limitation, SPEC §4). Sequence or land the "
            f"deps manually, then retry."
        ),
        type_="manual_step", priority="high",
    )
    raise TooManyUnlandedDeps(
        f"topic {topic_id} has {len(unlanded)} unlanded deps: {unlanded}"
    )
