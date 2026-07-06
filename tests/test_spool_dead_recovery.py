"""Spool dead-letter recovery (RCA D1, 2026-07-04).

Dead-lettered spool events used to rot silently: no CLI to see them, no replay
path, and the cockpit counted only pending events (never the dead/ subdir). This
pins the recovery surface:

  * `juggle spool list-dead` surfaces a dead file (uuid/type/created_at/reason),
  * `juggle spool replay` round-trips a dead event back to pending, clearing a
    stale spool_journal row, and a subsequent drain APPLIES it,
  * the 'applying'-state journal refusal is honored unless --force-applying,
  * `juggle spool purge` deletes dead files (all, or older than --before),
  * the cockpit status line shows the dead count with an alert marker,
  * a low-frequency backlog reminder is filed while dead/ is non-empty, but NOT
    re-filed every tick.

All spool/DB access is redirected to per-test temp dirs by conftest's fail-closed
isolation fixtures — this test never touches the real ~/.juggle/spool.
"""
import argparse
import json
import os

import pytest

from dbops.spool import write_event, read_pending, move_to_dead
from juggle_db import JuggleDB
from juggle_spool_apply import drain_spool
from juggle_spool_paths import spool_dir, spool_dead_dir


@pytest.fixture
def db():
    d = JuggleDB(os.environ["JUGGLE_DB_PATH"])
    d.init_db()
    return d


def _make_dead(thread_id="t1", etype="action_notify", args=None, reason="boom"):
    """Write a real spool event then dead-letter it (uuid preserved). Returns uuid."""
    args = args if args is not None else {"message": "hello"}
    uuid = write_event(spool_dir(), etype, "agent-1", thread_id, args)
    ev = read_pending(spool_dir())[-1]
    move_to_dead(spool_dir(), ev.path, reason)
    return uuid


# ── list-dead ──────────────────────────────────────────────────────────────────


def test_list_dead_surfaces_dead_file():
    from juggle_spool_dead import list_dead_entries

    uuid = _make_dead(etype="action_notify", reason="SystemExit code=1")
    entries = list_dead_entries()
    assert len(entries) == 1
    e = entries[0]
    assert e["uuid"] == uuid
    assert e["type"] == "action_notify"
    assert e["dead_reason"] == "SystemExit code=1"
    assert e["created_at"]  # populated from the payload


def test_cmd_list_dead_json(capsys):
    from juggle_cmd_spool import cmd_spool_list_dead

    _make_dead(reason="r1")
    cmd_spool_list_dead(argparse.Namespace(json_out=True))
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1 and out[0]["dead_reason"] == "r1"


# ── D2: applying-interrupted dedicated triage surface ─────────────────────────


def test_applying_interrupt_files_dedicated_high_triage_item(db):
    """2026-07-03 10:49/11:24: real completions interrupted mid-apply dead-lettered
    as anonymous junk. An 'applying' spool_journal row means a prior apply crashed
    mid-flight — the event is a GENUINE completion, not junk. drain must (a) stamp the
    dead file with the machine-readable code=applying-interrupted, and (b) file a
    DEDICATED HIGH action item naming the thread + the exact `--force-applying`
    recovery command, distinct from the capped/grouped generic dead-letter items."""
    tid = db.create_thread("topic", session_id="s")
    uuid = write_event(spool_dir(), "agent_complete", "agent-1", tid,
                       {"thread_id": tid, "result_summary": "did the thing"})
    # Simulate the interrupted-mid-apply crash: journal row stuck at 'applying'.
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO spool_journal(uuid, event_type, applied_at, outcome) "
            "VALUES (?,?,?,'applying')",
            (uuid, "agent_complete", "2026-07-03T10:49:00"),
        )
        conn.commit()

    stats = drain_spool(db)

    assert stats["dead"] == 1
    high = [i for i in db.get_open_action_items()
            if i.get("priority") == "high" and "--force-applying" in (i.get("message") or "")]
    assert len(high) == 1, "exactly one DEDICATED high triage item"
    assert uuid in high[0]["message"]      # the exact replay target
    assert tid in high[0]["message"]       # names the thread
    # The dead file is self-labelling for triage tooling.
    dead_payload = json.loads(next(spool_dead_dir().glob("*.json")).read_text())
    assert dead_payload["dead_reason"].startswith("code=applying-interrupted")


# ── replay round-trip ────────────────────────────────────────────────────────


def test_replay_round_trips_dead_event_and_drain_applies_it(db):
    tid = db.create_thread("topic", session_id="s")
    uuid = _make_dead(thread_id=tid, args={"message": "recovered!"})
    # Simulate the stale 'failed' journal a real dead-letter leaves behind.
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO spool_journal(uuid, event_type, applied_at, outcome) "
            "VALUES (?,?,?,'failed')",
            (uuid, "action_notify", "2026-07-01T00:00:00"),
        )
        conn.commit()

    from juggle_spool_dead import replay_dead

    results = replay_dead(db, uuid=uuid)
    assert results == [(True, uuid)]
    # Back in the pending dir, out of dead/.
    assert any(e.uuid == uuid for e in read_pending(spool_dir()))
    assert list(spool_dead_dir().glob("*.json")) == []

    stats = drain_spool(db)
    assert stats["applied"] == 1
    with db._connect() as conn:
        j = conn.execute("SELECT outcome FROM spool_journal WHERE uuid=?", (uuid,)).fetchone()
        notes = conn.execute(
            "SELECT message FROM notifications_v2 WHERE thread_id=?", (tid,)
        ).fetchall()
    assert j["outcome"] == "applied"  # journal advanced past the cleared 'failed'
    assert any("recovered!" in r["message"] for r in notes)  # the replayed notify landed


def test_replay_all_reinjects_every_dead_file(db):
    _make_dead(thread_id="a", args={"message": "one"})
    _make_dead(thread_id="b", args={"message": "two"})
    from juggle_spool_dead import replay_dead

    results = replay_dead(db, all_=True)
    assert len(results) == 2 and all(ok for ok, _ in results)
    assert list(spool_dead_dir().glob("*.json")) == []
    assert len(read_pending(spool_dir())) == 2


def test_replay_refuses_applying_journal_without_force(db):
    tid = db.create_thread("topic", session_id="s")
    uuid = _make_dead(thread_id=tid, args={"message": "x"})
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO spool_journal(uuid, event_type, applied_at, outcome) "
            "VALUES (?,?,?,'applying')",
            (uuid, "action_notify", "2026-07-01T00:00:00"),
        )
        conn.commit()

    from juggle_spool_dead import replay_dead

    ok, msg = replay_dead(db, uuid=uuid)[0]
    assert ok is False and "applying" in msg.lower()
    # Untouched: still dead, journal still 'applying'.
    assert len(list(spool_dead_dir().glob("*.json"))) == 1

    ok2, _ = replay_dead(db, uuid=uuid, force_applying=True)[0]
    assert ok2 is True
    assert list(spool_dead_dir().glob("*.json")) == []


# ── purge ────────────────────────────────────────────────────────────────────


def test_purge_all_deletes_every_dead_file():
    _make_dead(thread_id="a")
    _make_dead(thread_id="b")
    from juggle_spool_dead import purge_dead

    assert purge_dead() == 2
    assert list(spool_dead_dir().glob("*.json")) == []


def test_purge_before_date_keeps_newer_files():
    # Two dead files with hand-set created_at on the payload.
    dead = spool_dead_dir()
    dead.mkdir(parents=True, exist_ok=True)
    (dead / "old.json").write_text(json.dumps(
        {"uuid": "u-old", "type": "x", "created_at": "2026-07-01T10:00:00", "dead_reason": "r"}))
    (dead / "new.json").write_text(json.dumps(
        {"uuid": "u-new", "type": "x", "created_at": "2026-07-04T10:00:00", "dead_reason": "r"}))
    from juggle_spool_dead import purge_dead

    assert purge_dead(before="2026-07-03") == 1
    remaining = {p.name for p in dead.glob("*.json")}
    assert remaining == {"new.json"}


# ── cockpit dead count ───────────────────────────────────────────────────────


def test_cockpit_status_line_shows_dead_count():
    from juggle_cockpit_spool_status import get_spool_status_line

    _make_dead(thread_id="a")
    _make_dead(thread_id="b")
    line = get_spool_status_line(spool_dir())
    assert "dead: 2" in line
    assert "!" in line  # alert marker when dead > 0


def test_cockpit_status_line_no_dead_suffix_when_empty(tmp_path):
    from juggle_cockpit_spool_status import get_spool_status_line

    line = get_spool_status_line(tmp_path / "spool")
    assert line == "spool: 0"


# ── low-frequency backlog reminder ───────────────────────────────────────────


def test_backlog_reminder_files_once_then_throttles(db):
    _make_dead(thread_id="a")
    from juggle_spool_dead import maybe_file_dead_backlog_reminder

    assert maybe_file_dead_backlog_reminder(db, new_dead=0) is True
    items = [i for i in db.get_open_action_items()
             if "awaiting recovery" in (i.get("message") or "")]
    assert len(items) == 1
    # Second tick within the interval must NOT re-file.
    assert maybe_file_dead_backlog_reminder(db, new_dead=0) is False
    items2 = [i for i in db.get_open_action_items()
              if "awaiting recovery" in (i.get("message") or "")]
    assert len(items2) == 1


def test_backlog_reminder_skips_when_fresh_dead_this_drain(db):
    _make_dead(thread_id="a")
    from juggle_spool_dead import maybe_file_dead_backlog_reminder

    # new_dead>0 → fresh per-event items already filed; reminder is a no-op.
    assert maybe_file_dead_backlog_reminder(db, new_dead=2) is False


def test_backlog_reminder_noop_when_dead_empty(db):
    from juggle_spool_dead import maybe_file_dead_backlog_reminder

    assert maybe_file_dead_backlog_reminder(db, new_dead=0) is False
