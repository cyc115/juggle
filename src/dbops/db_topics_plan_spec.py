"""dbops.db_topics_plan_spec — plan_path/spec_path setter for topic nodes.

Extracted from dbops.db_topics (2026-07-03, architecture-gate LOC budget:
db_topics was at its grandfathered ceiling) as part of
T-fix-dispatch-plan-spec-provision.

Owns: set_topic_plan_spec only. Must not own: topic CRUD/state (dbops.db_topics).
"""
from __future__ import annotations

from dbops.db_graph import _cx
from dbops.schema import _now


def set_topic_plan_spec(
    db, topic_id: str, *, plan_path: str | None = None, spec_path: str | None = None,
    force: bool = False, conn=None,
) -> None:
    """Set ``plan_path``/``spec_path`` on an existing topic.

    Default (``force=False``, the ``graph add-task --plan/--spec`` path):
    first-set-wins — only fills a column that is currently empty, so an
    add-task call never clobbers the plan/spec a `graph load` already
    recorded for the topic. ``force=True`` (the `graph load` path) always
    overwrites — the spec file is the source of truth on reload."""
    from dbops.db_topics import get_topic  # lazy: db_topics re-exports us

    topic = get_topic(db, topic_id, conn=conn)
    if topic is None:
        return
    sets, params = [], []
    if plan_path and (force or not topic.get("plan_path")):
        sets.append("plan_path=?")
        params.append(plan_path)
    if spec_path and (force or not topic.get("spec_path")):
        sets.append("spec_path=?")
        params.append(spec_path)
    if not sets:
        return
    sets.append("updated_at=?")
    params.append(_now())
    params.append(topic_id)
    with _cx(db, conn) as c:
        c.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE id=? AND kind='topic'", params)
