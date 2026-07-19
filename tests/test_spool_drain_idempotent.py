"""Watchdog spool drain: apply-once idempotency, dead-letter on malformed events,
SystemExit-safety, crash-mid-apply recovery, and reuse of the EXISTING cmd_*
write bodies (agent_complete/agent_fail/action_create/action_ack/action_notify/
mark_task/record_error).

Regression scope (T-spool, DA findings 1+2): a crash mid-drain must never
double-apply an event, a sys.exit(1) inside a replayed handler must never
crash-loop the watchdog process, and a malformed spool event must never
silently vanish (dead-letter + HIGH action item)."""
import os

import pytest

from dbops.spool import write_event, read_pending
from juggle_db import JuggleDB
from juggle_spool_apply import apply_event, drain_spool


@pytest.fixture
def db():
    # Bind to the SAME DB the conftest autouse fixture points get_db() at
    # (JUGGLE_DB_PATH) — the replayed cmd_* handlers resolve their own handle
    # via get_db(), so the test's handle and the handler's handle must be one
    # file, or the writes land where the test can't see them.
    d = JuggleDB(os.environ["JUGGLE_DB_PATH"])
    d.init_db()
    return d


@pytest.fixture
def spool(tmp_path, monkeypatch):
    s = tmp_path / "spool"
    s.mkdir()
    monkeypatch.setattr("juggle_spool_apply.spool_dir", lambda: s)
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")
    return s


def _make_thread(db, label="AB"):
    return db.create_thread("test topic", session_id="s")


def test_drain_applies_action_notify_event(db, spool):
    tid = _make_thread(db)
    # Real writer shape (cmd_notify): thread_id rides the event's top-level
    # field, args carries ONLY {"message": ...} — apply_event must seed the
    # replayed Namespace's thread_id from the top level, not from args.
    write_event(spool, "action_notify", "agent-1", tid, {"message": "hi"})
    stats = drain_spool(db)
    assert stats["applied"] == 1
    with db._connect() as conn:
        rows = conn.execute("SELECT message FROM notifications_v2 WHERE thread_id=?", (tid,)).fetchall()
    assert any("hi" in r["message"] for r in rows)


def test_drain_is_idempotent_across_two_calls_same_event(db, spool):
    tid = _make_thread(db)
    write_event(spool, "action_notify", "agent-1", tid, {"message": "once"})
    stats1 = drain_spool(db)
    assert stats1["applied"] == 1
    assert read_pending(spool) == []
    stats2 = drain_spool(db)
    assert stats2["applied"] == 0


def test_drain_skips_already_applied_uuid_without_reapplying(db, spool, monkeypatch):
    """If a file somehow survives past a successful apply (e.g. rename raced), the
    journal is the idempotency backstop — re-applying the same uuid is a no-op."""
    tid = _make_thread(db)
    write_event(spool, "action_notify", "agent-1", tid, {"message": "dup"})
    from dbops.spool import read_pending as _rp
    event = _rp(spool)[0]
    ok, _ = apply_event(db, event)
    assert ok
    ok2, msg2 = apply_event(db, event)
    assert ok2 is True
    assert "already applied" in msg2.lower()


def test_drain_dead_letters_malformed_event_and_files_action_item(db, spool):
    tid = _make_thread(db)
    write_event(spool, "action_notify", "agent-1", tid, {})  # missing required 'message'
    stats = drain_spool(db)
    assert stats["dead"] == 1
    assert stats["applied"] == 0
    dead_files = list((spool / "dead").glob("*.json"))
    assert len(dead_files) == 1
    open_items = db.get_open_action_items()
    assert any("spool" in (i.get("message") or "").lower() for i in open_items)


def test_drain_sweeps_unparseable_pending_to_dead(db, spool):
    """RCA D5 2026-07-04: corrupt pending file skipped every tick forever. A
    top-level *.json that read_pending can't parse (corrupt / half-written) inflated
    `spool: N` on every drain with no path out. After surviving _UNPARSEABLE_SWEEP_TICKS
    drains it must move to dead/ with dead_reason 'unparseable pending file' and stop
    counting as pending."""
    import json

    from dbops.spool import _UNPARSEABLE_SWEEP_TICKS

    corrupt = spool / "20260101T000000.000000-agent-abc.json"
    corrupt.write_text("{not valid json")

    # Early ticks give a half-written file time to settle — not swept yet.
    drain_spool(db)
    assert corrupt.exists(), "must not sweep a fresh unparseable file on the first tick"

    for _ in range(_UNPARSEABLE_SWEEP_TICKS):
        drain_spool(db)

    assert not corrupt.exists()
    dead_files = list((spool / "dead").glob("*.json"))
    assert any(
        json.loads(p.read_text()).get("dead_reason") == "unparseable pending file"
        for p in dead_files
    )
    # No longer inflating the pending count.
    assert read_pending(spool) == []


def test_drain_applies_agent_complete_via_existing_cmd_complete_agent(db, spool):
    tid = _make_thread(db)
    write_event(spool, "agent_complete", "agent-1", tid, {
        "thread_id": tid, "result_summary": "shipped it", "retain_text": None,
        "open_questions": None, "handoff": None, "role": None,
    })
    stats = drain_spool(db)
    assert stats["applied"] == 1
    thread = db.get_thread(tid)
    # get_thread returns NODE vocab (P8 read-collapse) — 'state', not 'status'.
    assert thread["state"] in ("closed", "done", "active")  # cmd_complete_agent's own transition rules apply


def test_apply_event_dead_letters_on_systemexit_without_propagating(db, spool, monkeypatch):
    """DA Resolution #1 pin (RED before fix): a replayed handler's sys.exit(1)
    (e.g. 'thread not found' validation) must be caught, journaled 'failed', and
    dead-lettered — NOT propagate as SystemExit and kill the watchdog process."""
    uuid = write_event(spool, "agent_complete", "agent-1", "bogus-thread-id", {
        "thread_id": "bogus-thread-id", "result_summary": "x", "retain_text": None,
        "open_questions": None, "handoff": None, "role": None,
    })
    event = read_pending(spool)[0]
    # cmd_complete_agent sys.exit(1)s when the thread isn't found — this must not raise here.
    ok, msg = apply_event(db, event)
    assert ok is False
    assert "systemexit" in msg.lower() or "exit" in msg.lower()
    with db._connect() as conn:
        row = conn.execute("SELECT outcome FROM spool_journal WHERE uuid=?", (uuid,)).fetchone()
    assert row["outcome"] == "failed"


def test_apply_event_journals_failure_in_the_db_it_was_called_with_not_get_db(db, spool, monkeypatch, tmp_path):
    """Regression pin (2026-07-02 spool isolation incident, point 4): a replay
    failure must journal 'failed' in the SAME db apply_event/drain_spool were
    called with — never split-brain into whatever DB the dispatched cmd_*
    handler's own get_db() happens to resolve (e.g. an unrelated tmp DB in this
    test) while the caller's journal silently sees nothing."""
    other_db = JuggleDB(str(tmp_path / "other-not-the-caller-db.db"))
    other_db.init_db()
    import juggle_cli_common as cli_common
    monkeypatch.setattr(cli_common, "get_db", lambda *a, **kw: other_db)

    uuid = write_event(spool, "agent_complete", "agent-1", "bogus-thread-id", {
        "thread_id": "bogus-thread-id", "result_summary": "x", "retain_text": None,
        "open_questions": None, "handoff": None, "role": None,
    })
    event = read_pending(spool)[0]
    ok, _msg = apply_event(db, event)
    assert ok is False

    with db._connect() as conn:
        row = conn.execute("SELECT outcome FROM spool_journal WHERE uuid=?", (uuid,)).fetchone()
    assert row is not None and row["outcome"] == "failed"

    with other_db._connect() as conn:
        other_row = conn.execute("SELECT outcome FROM spool_journal WHERE uuid=?", (uuid,)).fetchone()
    assert other_row is None


def test_apply_event_refuses_to_reapply_stuck_applying_state(db, spool):
    """DA Resolution #2 pin: simulate a crash between the 'applying' journal
    write and the handler completing (process killed mid-flight — the only way
    a row is left in 'applying' state). The NEXT apply_event call for the same
    uuid must refuse to blind-retry (side effects like git-push/integrate may be
    partially complete) and dead-letter for manual triage instead."""
    tid = _make_thread(db)
    uuid = write_event(spool, "action_notify", "agent-1", tid, {"message": "x"})
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO spool_journal(uuid, event_type, applied_at, outcome) VALUES (?,?,?,?)",
            (uuid, "action_notify", "2026-07-01T00:00:00", "applying"),
        )
        conn.commit()
    event = read_pending(spool)[0]
    ok, msg = apply_event(db, event)
    assert ok is False
    assert "applying" in msg.lower() or "interrupted" in msg.lower()


def test_drain_applies_action_create_via_existing_cmd_request_action(db, spool):
    """Real writer shape (cmd_request_action): thread_id at the top level, args
    carries {message, type, priority} — the replayed handler reads args.thread_id,
    so apply_event must seed it from the top level or the event dead-letters."""
    tid = _make_thread(db)
    write_event(spool, "action_create", "agent-1", tid,
                {"message": "do the thing", "type": "manual_step", "priority": "high"})
    stats = drain_spool(db)
    assert stats["applied"] == 1
    assert stats["dead"] == 0
    assert any("do the thing" in (i.get("message") or "") for i in db.get_open_action_items())


def test_drain_agent_complete_on_archived_reallocated_thread_does_not_raise(db, spool):
    """Regression pin (2026-07-12/13 incident, dead-lettered spool events
    191c7e17/aabd9574): an ad-hoc thread archived (e.g. by the watchdog's
    stall/orphan reclaim) while its dispatched coder was still working, whose
    freed slug the wheel then reallocates to a brand-new conversation, must
    not crash on a late agent_complete. cmd_complete_agent's decide_thread_close
    walks a no-human-message thread to 'closed' -> node state 'done'
    (STATUS_TO_STATE) unconditionally; 'done' re-enters the partial
    idx_nodes_live_label index (WHERE state != 'archived'), colliding with the
    reallocated-slug holder -> IntegrityError: UNIQUE constraint failed:
    nodes.user_label. Dead reason on unfixed code:
    'IntegrityError during replay of agent_complete: UNIQUE constraint failed:
    nodes.user_label' (byte-identical to the production dead-letter)."""
    from dbops.schema import _wheel_index

    tid = _make_thread(db, label="AA")
    label = db.get_thread(tid)["user_label"]
    db.archive_thread(tid)
    assert db.get_thread(tid)["state"] == "archived"

    # Force the wheel to reallocate the just-freed slug to a new conversation,
    # simulating the wheel cycling back around in a long-running instance.
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO juggle_meta(key, value) VALUES ('label_seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(_wheel_index(label)),),
        )
        conn.commit()
    other = db.create_thread("unrelated new topic", session_id="s2")
    assert db.get_thread(other)["user_label"] == label

    write_event(spool, "agent_complete", "agent-1", tid, {
        "thread_id": tid, "result_summary": "late completion of archived thread",
        "retain_text": None, "open_questions": None, "handoff": None, "role": None,
    })
    stats = drain_spool(db)

    assert stats["dead"] == 0, "must not dead-letter — the archived thread stays untouched"
    assert stats["applied"] == 1
    assert db.get_thread(tid)["state"] == "archived"


def test_drain_agent_fail_on_archived_reallocated_thread_does_not_raise(db, spool):
    """Regression pin (2026-07-18 dead-letter 672c4a14): same shape as
    test_drain_agent_complete_on_archived_reallocated_thread_does_not_raise,
    but for agent_fail. cmd_fail_agent's unrecoverable-failure branch called
    db.set_thread_status(thread_uuid, "closed") unconditionally — unlike
    cmd_complete_agent's decide_thread_close, it had no already-archived guard
    — so a late agent_fail for a thread the watchdog had since archived (its
    freed slug reallocated to a new conversation) raised the same 'IntegrityError:
    UNIQUE constraint failed: nodes.user_label' (byte-identical dead reason to
    the production dead-letter) instead of a no-op."""
    from dbops.schema import _wheel_index

    tid = _make_thread(db, label="AA")
    label = db.get_thread(tid)["user_label"]
    db.archive_thread(tid)
    assert db.get_thread(tid)["state"] == "archived"

    with db._connect() as conn:
        conn.execute(
            "INSERT INTO juggle_meta(key, value) VALUES ('label_seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(_wheel_index(label)),),
        )
        conn.commit()
    other = db.create_thread("unrelated new topic", session_id="s2")
    assert db.get_thread(other)["user_label"] == label

    write_event(spool, "agent_fail", "agent-1", tid, {
        "thread_id": tid, "error": "late failure of archived thread",
        "failure_type": "persistent", "max_retries": 0, "recovery_dispatched": False,
    })
    stats = drain_spool(db)

    assert stats["dead"] == 0, "must not dead-letter — the archived thread stays untouched"
    assert stats["applied"] == 1
    assert db.get_thread(tid)["state"] == "archived"


def test_drain_routes_graph_mark_task_event_type(db, spool):
    """Regression pin: the committed writer emits event type 'graph_mark_task'
    (juggle_cmd_graph.py), NOT 'mark_task'. A stale type string would route to
    the else-arm and dead-letter EVERY real mark-task with 'unknown spool event
    type'. The task itself is absent here, so the replayed handler fails its own
    validation — but the failure must NOT be an unknown-type routing miss."""
    write_event(spool, "graph_mark_task", "agent-1", "",
                {"task_id": "no-such-task", "fail": False, "handoff": None})
    event = read_pending(spool)[0]
    ok, msg = apply_event(db, event)
    assert "unknown spool event type" not in msg.lower()


def test_drain_applies_record_error_with_command_args_key(db, spool):
    """Real writer shape (_spool_error_event): the captured context rides under
    'command_args' (a JSON string), NOT 'context'. Reading the wrong key would
    silently drop it — pin that the replayed dedup_or_insert_error persists it."""
    write_event(spool, "record_error", "", "", {
        "signature_hash": "sig-xyz", "error_class": "A", "exc_type": "ValueError",
        "traceback": "Traceback ...", "entrypoint": "juggle_cli",
        "command_args": '{"cmd": "graph mark-task"}',
    })
    stats = drain_spool(db)
    assert stats["applied"] == 1
    with db._connect() as conn:
        row = conn.execute(
            "SELECT command_args FROM error_events WHERE signature_hash=?", ("sig-xyz",)
        ).fetchone()
    assert row is not None
    assert "graph mark-task" in row["command_args"]
