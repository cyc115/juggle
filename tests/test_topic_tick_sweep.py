"""Tick sweep wrapper (2026-06-30 topic-graph-state-unify F5).

The 30s watchdog tick sweeps all conversation topics so a derived close can never
diverge indefinitely, even if an event trigger was missed.
"""
from datetime import datetime, timedelta, timezone

from dbops import db_graph

_NOW = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)


def _merged_idle_topic(db):
    topic = db.create_thread(topic="feature", session_id="s")
    db.add_message(topic, role="user", content="build the thing")
    ts = (_NOW - timedelta(minutes=99)).isoformat()
    with db._connect() as c:
        c.execute(
            "UPDATE messages SET created_at=? WHERE thread_id=? AND role='user'",
            (ts, topic),
        )
        c.commit()
    db_graph.create_task(db, task_id="c1", project_id="INBOX", title="t", prompt="p")
    db_graph.set_task_topic(db, "c1", topic)
    for ev in ("deps_ready", "claim", "dispatch", "integrate_start", "integrate_ok"):
        db_graph.task_transition(db, "c1", ev)
    return topic


def test_topic_sweep_closes_merged_idle(juggle_db):
    import juggle_watchdog_daemon as wd

    topic = _merged_idle_topic(juggle_db)
    wd._topic_sweep(juggle_db)
    assert juggle_db.get_thread(topic)["state"] == "done"


def test_topic_sweep_never_raises_on_bad_db():
    import juggle_watchdog_daemon as wd

    # A bogus object with no usable connection must not propagate an error.
    class _Bad:
        def _connect(self):
            raise RuntimeError("boom")

    wd._topic_sweep(_Bad())  # no exception
