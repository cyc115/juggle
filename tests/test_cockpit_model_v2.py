"""Tests for Task 6 cockpit model v2."""
import pytest
from juggle_db import JuggleDB
from juggle_cockpit_model import snapshot


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    d._set_session_key_external("session_id", "sessA")
    return d


def test_topics_ordered_active_running_closed_archived(db):
    a = db.create_thread("active", session_id="sessA")         # A
    r = db.create_thread("running", session_id="sessA")        # B
    c = db.create_thread("closed", session_id="sessA")         # C
    x = db.create_thread("archived", session_id="sessA")       # D
    db.set_thread_status(r, "running")
    db.set_thread_status(c, "closed")
    db.archive_thread(x)
    state = snapshot(db)
    statuses = [t.status for t in state.topics]
    # active first, then running, then closed (within TTL), then archived
    assert statuses[0] == "active"
    assert statuses[1] == "running"
    assert statuses[2] == "closed"
    assert statuses[3] == "archived"


def test_archived_limit_most_recent_n(db):
    # Create and archive sequentially (MAX_THREADS=10 enforced for non-archived)
    ids = []
    for i in range(15):
        tid = db.create_thread(f"t{i}", session_id="sessA")
        ids.append(tid)
        db.archive_thread(tid)
    state = snapshot(db)
    archived = [t for t in state.topics if t.status == "archived"]
    # default limit N = 10 per spec
    assert len(archived) == 10


def test_actions_populated_from_action_items(db):
    tid = db.create_thread("t", session_id="sessA")
    db.add_action_item(thread_id=tid, message="push to prod", type_="manual_step", priority="high")
    db.add_action_item(thread_id=None, message="consider rebase", type_="decision", priority="low")
    state = snapshot(db)
    msgs = [a.text for a in state.actions]
    assert "push to prod" in msgs
    assert "consider rebase" in msgs
    # High priority first
    assert state.actions[0].text == "push to prod"


def test_notifications_filtered_to_current_session(db):
    db.add_notification_v2(thread_id=None, message="this session", session_id="sessA")
    db.add_notification_v2(thread_id=None, message="other session", session_id="sessOLD")
    state = snapshot(db)
    msgs = [n.text for n in state.notifications]
    assert "this session" in msgs
    assert "other session" not in msgs
