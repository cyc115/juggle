"""Regression pin: releasing an agent whose bound topic ALREADY landed is routine
cleanup, not a failure — even when the topic's state is not literally 'verified'.

2026-07-07 completed agents leak / watchdog re-dispatches merged topic.
Symptom: releasing an agent whose work WAS merged marked its topic failed-exec
and filed a spurious HIGH "released without completing" action item, because the
reconcile only recognized the exact (state=='verified' AND merged_sha) pair — a
topic carrying a merged_sha in any other landed state (integrated-unlanded / done)
slipped through to the failure arm.
"""

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    import juggle_cli_common as common
    import juggle_cmd_agents_common

    monkeypatch.setattr(common, "get_db", lambda: d)
    monkeypatch.setattr(juggle_cmd_agents_common, "get_db", lambda: d)
    return d


def _bind_topic(db, tid, topic_id, state, merged_sha):
    from dbops import db_topics as tp
    from dbops.state_write import write_state
    from dbops.schema import _now

    tp.create_topic(db, topic_id=topic_id, project_id="INBOX", title="T")
    tp.set_topic_thread(db, topic_id, tid)
    if merged_sha:
        tp.set_topic_merged_sha(db, topic_id, merged_sha)
    with db._connect() as conn:
        write_state(conn, topic_id, state, now=_now(), verified=(state == "verified"))
        conn.commit()


def test_release_on_merged_topic_non_verified_state_is_routine(db):
    """RED on the narrow verified+merged check: a topic carrying merged_sha but
    resting in integrated-unlanded must be treated as landed → no failure item."""
    from juggle_cmd_agents import cmd_release_agent

    tid = db.create_thread("landed-topic", session_id="s")
    db.update_thread(tid, status="background")
    _bind_topic(db, tid, "T-landed", state="integrated-unlanded", merged_sha="cafef00d")

    agent_id = db.create_agent("coder", "pane-1")
    db.update_agent(agent_id, status="busy", assigned_thread=tid)

    cmd_release_agent(argparse.Namespace(agent_id=agent_id, force=True))

    items = db.get_open_action_items()
    assert not any(
        "released without completing" in item["message"] for item in items
    ), f"merged topic should not file a failure action item, got: {items}"
    assert db.get_thread(tid)["state"] != "failed-exec"
