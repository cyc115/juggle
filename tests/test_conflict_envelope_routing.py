"""Regression pins for fix-conflict-envelope-routing (2026-07-04 cyc_GP incident).

INCIDENT: integrate conflict failures DID stamp a fail_envelope, but
store_fail_envelope resolved the topic BY THREAD (get_topic_by_thread → the
thread's first topic). A dual-dispatch thread owning several topics
(T-gp-spine verified + T-gp-retry/T-gp-edit integrating) got the envelope
stamped on the already-verified T-gp-spine while the actually-integrating
topics stayed envelope-empty. juggle_graph_reintegrate step 4 (envelope →
route failed-integration) never fired for the real topics, so the sweep
re-drove the doomed gate every backoff round and filed a byte-identical HIGH
action item each time (~25 items, 5312-5340).

Pin 1: a thread with one verified + two integrating topics, conflict failure →
BOTH integrating topics get the envelope, the verified topic untouched, and the
next sweep pass routes BOTH to failed-integration with no re-spawn.
Pin 2: two consecutive conflict failures → exactly one open action item.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest


@pytest.fixture
def db(tmp_path):
    from juggle_db import JuggleDB

    d = JuggleDB(db_path=str(tmp_path / "juggle-fixture.db"))
    d.init_db()
    return d


def _mk_topic(db, topic_id, state, thread_id, project_id="INBOX"):
    """Create a topic in ``state`` bound (kind='dispatch') to ``thread_id``."""
    from datetime import datetime, timezone

    from dbops import db_topics

    db_topics.create_topic(db, topic_id=topic_id, project_id=project_id,
                           title=f"Topic {topic_id}")
    now = datetime.now(timezone.utc).isoformat()
    with db._connect() as c:
        c.execute("UPDATE nodes SET state=?, updated_at=? WHERE id=? AND kind='topic'",
                  (state, now, topic_id))
        c.commit()
    db_topics.set_topic_thread(db, topic_id, thread_id)


# ── Pin 1: envelope lands on every integrating topic, verified untouched ──────

def test_conflict_envelope_stamps_all_integrating_topics_on_thread(db):
    """cyc_GP dual-dispatch: stamp the envelope on BOTH integrating topics,
    never the thread's first/verified topic (the bug stamped it on the
    already-verified T-gp-spine)."""
    from dbops.db_topics import get_topic
    from dbops.db_topics_marking import store_fail_envelope

    thread = db.create_thread(topic="gp", session_id="s")
    _mk_topic(db, "T-gp-spine", "verified", thread)
    _mk_topic(db, "T-gp-retry", "integrating", thread)
    _mk_topic(db, "T-gp-edit", "integrating", thread)

    store_fail_envelope(db, thread, None, '{"class":"conflict"}')

    assert get_topic(db, "T-gp-retry")["fail_envelope"] == '{"class":"conflict"}'
    assert get_topic(db, "T-gp-edit")["fail_envelope"] == '{"class":"conflict"}'
    assert not get_topic(db, "T-gp-spine")["fail_envelope"]


def test_sweep_routes_both_integrating_topics_without_respawn(db, monkeypatch):
    """After the envelope lands on both integrating topics, the next
    reintegrate sweep routes BOTH to failed-integration and NEVER re-spawns a
    detached integrate (the wedge that filed duplicate items every round)."""
    import juggle_graph_reintegrate as ri
    from dbops.db_topics import get_topic
    from dbops.db_topics_marking import store_fail_envelope

    thread = db.create_thread(topic="gp", session_id="s")
    _mk_topic(db, "T-gp-spine", "verified", thread)
    _mk_topic(db, "T-gp-retry", "integrating", thread)
    _mk_topic(db, "T-gp-edit", "integrating", thread)
    store_fail_envelope(db, thread, None, '{"class":"conflict"}')

    spawned = []
    monkeypatch.setattr(ri, "_spawn_detached_integrate",
                        lambda *a, **k: spawned.append(1))
    ri.reset_backoff()

    ri.sweep_reintegrate(db, ["INBOX"], session_id="s")

    assert get_topic(db, "T-gp-retry")["state"] == "failed-integration"
    assert get_topic(db, "T-gp-edit")["state"] == "failed-integration"
    assert get_topic(db, "T-gp-spine")["state"] == "verified"  # untouched
    assert spawned == [], "a topic with a fail_envelope must route, never re-spawn"


# ── Pin 2: dedup — no byte-identical HIGH item every backoff round ────────────

def test_two_consecutive_conflict_failures_file_one_action_item(db):
    """items 5312-5340: a re-driven doomed gate must not file byte-identical
    HIGH action items every backoff round — one open item, then refresh/skip."""
    from juggle_integrate_envelope import (
        STEP_REBASE_CONFLICT,
        FailContext,
        record_refusal,
    )

    ctx = FailContext(db=db, thread_id="t-gp", worktree_branch="cyc_GP")
    detail = "Rebase conflict on cyc_GP onto main."
    record_refusal(STEP_REBASE_CONFLICT, detail, ctx)
    record_refusal(STEP_REBASE_CONFLICT, detail, ctx)

    items = [i for i in db.get_open_action_items()
             if "integrate failed" in i["message"]]
    assert len(items) == 1
