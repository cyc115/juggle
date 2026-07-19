"""Completed-agent reaper — releases a pool agent left busy while idle at its
prompt on a topic whose work already landed (verified / merged / done).

2026-07-07 completed agents leak / watchdog re-dispatches merged topic.
Symptom: five coder agents whose work had COMPLETED sat status=busy for 15-35h,
idle at their tmux prompt, never released — saturating the pool (5/5) so every
new `agent get` failed "pool full". The stalled/crashed reaper only targets
STALLED/CRASHED agents; there was NO reaper for a COMPLETED-but-unreleased agent
(bound topic terminal, agent idle).
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from juggle_db import JuggleDB

# claude-harness markers (SSOT mirror; used as injected fixtures)
READY = ("shift+tab to cycle",)
SUBMIT = ("esc to interrupt", "✻")
IDLE_PANE = "recap: all done.\n\n > \n  shift+tab to cycle"
WORKING_PANE = "Running tests…\n✻ Cooking (26s · esc to interrupt)\n  shift+tab to cycle"


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
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


def _busy_agent_on(db, tid, pane="%1", busy_since="2020-01-01T00:00:00+00:00"):
    agent_id = db.create_agent("coder", pane)
    db.update_agent(
        agent_id, status="busy", assigned_thread=tid, last_task="do it",
        busy_since=busy_since,
    )
    return agent_id


def _dead_letter_agent_complete(thread_id: str) -> str:
    """Write + dead-letter an agent_complete spool event for ``thread_id``,
    mirroring production (spool_event_if_agent writes agent_id/thread_id as
    "" and puts the real thread id inside args)."""
    from dbops.spool import move_to_dead, write_event
    from juggle_spool_paths import spool_dir

    sd = spool_dir()
    ev_uuid = write_event(sd, "agent_complete", "", "", {"thread_id": thread_id})
    pending = [p for p in sd.glob("*.json") if ev_uuid[:8] in p.name]
    move_to_dead(sd, pending[0], "test: simulated apply failure")
    return ev_uuid


def _dead_letter_agent_fail(thread_id: str) -> str:
    """Same as ``_dead_letter_agent_complete`` but for an agent_fail event —
    mirrors the 2026-07-18 dead-letter 672c4a14 (a failed agent's status
    write-back dead-lettered on a UNIQUE constraint, leaving the agent stuck
    'busy' since the Fault-1 backstop only recognized agent_complete)."""
    from dbops.spool import move_to_dead, write_event
    from juggle_spool_paths import spool_dir

    sd = spool_dir()
    ev_uuid = write_event(sd, "agent_fail", "", "", {"thread_id": thread_id})
    pending = [p for p in sd.glob("*.json") if ev_uuid[:8] in p.name]
    move_to_dead(sd, pending[0], "test: simulated apply failure")
    return ev_uuid


def _reap(db, capture, **kw):
    from juggle_watchdog_reap_done import reap_completed_agents

    reap_completed_agents(
        db,
        MagicMock(),
        session_id="s",
        capture=lambda pid: capture,
        markers_for=lambda a: (READY, SUBMIT),
        active_pattern_for=lambda a: "",
        **kw,
    )


def test_completed_topic_idle_agent_is_released(db):
    """Idle-at-prompt agent bound to a landed (verified+merged) topic → reaper
    returns it to the pool (status idle, slot freed)."""
    tid = db.create_thread("done-topic", session_id="s")
    db.update_thread(tid, status="background")
    _bind_topic(db, tid, "T-done", state="verified", merged_sha="deadbeef")
    agent_id = _busy_agent_on(db, tid)

    _reap(db, IDLE_PANE)

    agent = db.get_agent(agent_id)
    assert agent["status"] == "idle"
    assert agent["assigned_thread"] is None
    # Thread reconciled to done so the orphan detector (scans state='background')
    # can never re-dispatch the already-landed work.
    assert db.get_thread(tid)["state"] == "done"


def test_reaper_ignores_agent_mid_turn(db):
    """Guard: an agent actively working (submission marker visible) on a landed
    topic must NOT be reaped even though the topic is terminal."""
    tid = db.create_thread("done-topic", session_id="s")
    db.update_thread(tid, status="background")
    _bind_topic(db, tid, "T-done", state="verified", merged_sha="deadbeef")
    agent_id = _busy_agent_on(db, tid)

    _reap(db, WORKING_PANE)

    assert db.get_agent(agent_id)["status"] == "busy"


def test_reaped_landed_thread_is_not_reorphaned(db):
    """After the reaper releases a landed agent, the orphan detector must NOT
    re-dispatch the topic: reconciling the thread to 'done' removes it from the
    orphan scan (state='background'). Pins the reaper↔orphan-path interaction so
    the completed-agents-leak fix isn't defeated by a second re-dispatch path."""
    from juggle_watchdog import check_orphaned_threads

    tid = db.create_thread("done-topic", session_id="s")
    db.update_thread(tid, status="background", last_dispatched_task="impl",
                     last_dispatched_role="coder")
    _bind_topic(db, tid, "T-done", state="verified", merged_sha="deadbeef")
    _busy_agent_on(db, tid)

    _reap(db, IDLE_PANE)

    mgr = MagicMock()
    mgr.spawn_agent = MagicMock(return_value={"id": "x", "pane_id": "%9", "status": "busy"})
    # Threshold 0 → every eligible background thread would be re-dispatched.
    check_orphaned_threads(db, orphan_threshold=0.0, mgr=mgr)
    mgr.spawn_agent.assert_not_called()


def test_reaper_ignores_agent_on_active_topic(db):
    """Guard: an idle agent whose bound topic is still ACTIVE is legitimately
    awaiting its next task — the reaper must leave it alone."""
    tid = db.create_thread("active-topic", session_id="s")
    db.update_thread(tid, status="background")
    _bind_topic(db, tid, "T-active", state="running", merged_sha=None)
    agent_id = _busy_agent_on(db, tid)

    _reap(db, IDLE_PANE)

    assert db.get_agent(agent_id)["status"] == "busy"


# ── Fault-1 backstop: completion ATTEMPTED but topic never landed ───────────
# REGRESSION (2026-07-13 completed-agents-never-decommission): the reaper's
# sole gate was topic_work_landed(topic) — a topic stuck non-terminal (e.g.
# 'background') because its `agent complete` call dead-lettered had NO
# backstop, so the agent leaked forever. The backstop must fire ONLY when a
# completion was actually ATTEMPTED (a dead-lettered agent_complete spool
# event for the thread exists) — never on a topic that's merely mid-flight,
# which is indistinguishable from "idle, legitimately awaiting next task"
# without that evidence (see test_reaper_ignores_agent_on_active_topic above).


def test_backstop_reaps_agent_whose_completion_dead_lettered(db):
    """Idle agent + non-landed topic + a dead-lettered agent_complete for its
    thread, past the bounded min-age → reconciled: agent freed, topic state
    left untouched (NOT silently marked done), action item filed for the
    orchestrator."""
    tid = db.create_thread("stuck-topic", session_id="s")
    db.update_thread(tid, status="background")
    _bind_topic(db, tid, "T-stuck", state="background", merged_sha=None)
    agent_id = _busy_agent_on(db, tid)
    _dead_letter_agent_complete(tid)

    _reap(db, IDLE_PANE, backstop_min_age_secs=0.0)

    agent = db.get_agent(agent_id)
    assert agent["status"] == "idle"
    assert agent["assigned_thread"] is None
    # Work may not have landed — topic state must NOT be silently marked done.
    assert db.get_thread(tid)["state"] == "background"
    items = db.get_open_action_items()
    assert any(it.get("thread_id") == tid for it in items), (
        "backstop must push a needs-judgment action item, not silently reconcile"
    )


def test_backstop_ignores_dead_letter_within_min_age(db):
    """Guard: a just-written dead-letter must NOT trigger the backstop before
    the configured min-age elapses — gives self-heal/retry paths a window."""
    tid = db.create_thread("stuck-topic", session_id="s")
    db.update_thread(tid, status="background")
    _bind_topic(db, tid, "T-stuck", state="background", merged_sha=None)
    agent_id = _busy_agent_on(db, tid)
    _dead_letter_agent_complete(tid)

    _reap(db, IDLE_PANE, backstop_min_age_secs=9999.0)

    assert db.get_agent(agent_id)["status"] == "busy"


def test_backstop_ignores_non_landed_topic_without_dead_letter(db):
    """Guard: no dead-letter evidence at all → never fires, even past the
    min-age window (0.0) — this is the ordinary "awaiting next task" case."""
    tid = db.create_thread("active-topic-2", session_id="s")
    db.update_thread(tid, status="background")
    _bind_topic(db, tid, "T-active-2", state="background", merged_sha=None)
    agent_id = _busy_agent_on(db, tid)

    _reap(db, IDLE_PANE, backstop_min_age_secs=0.0)

    assert db.get_agent(agent_id)["status"] == "busy"


def test_backstop_ignores_stale_dead_letter_after_fresh_redispatch(db):
    """DA finding: a dead-letter is never purged when the backstop reconciles
    — if the topic is later manually retried with a FRESH agent, that new
    agent must NOT be reaped just because an OLD dead-letter for the same
    thread still exists in spool/dead/. Discriminator: the busy agent's
    busy_since must predate the dead-letter's created_at (same attempt);
    a later busy_since means a different (retry) assignment."""
    tid = db.create_thread("stuck-topic", session_id="s")
    db.update_thread(tid, status="background")
    _bind_topic(db, tid, "T-stuck", state="background", merged_sha=None)

    # The dead-letter is created FIRST (the original agent's failed attempt)...
    _dead_letter_agent_complete(tid)
    # ...then a FRESH agent is dispatched afterward (real busy_since, i.e. now).
    from dbops.schema import _now
    agent_id = _busy_agent_on(db, tid, busy_since=_now())

    _reap(db, IDLE_PANE, backstop_min_age_secs=0.0)

    assert db.get_agent(agent_id)["status"] == "busy"


# ── Fault-1 backstop must also recognize a dead-lettered agent_fail ─────────
# REGRESSION (2026-07-18 dead-letter 672c4a14): a failed agent's status
# write-back (agent_fail) dead-lettered (spool replay hit a UNIQUE constraint
# on an already-archived thread). find_attempted_completion only matched
# type=="agent_complete", so the backstop never fired for it — the agent sat
# status='busy' forever with no path back to the pool, unreconcilable by the
# watchdog. The backstop must treat a dead-lettered agent_fail exactly like a
# dead-lettered agent_complete: attempted-but-failed-to-land evidence.


def test_backstop_reaps_agent_whose_agent_fail_dead_lettered(db):
    """Same shape as test_backstop_reaps_agent_whose_completion_dead_lettered
    but the dead-lettered event is agent_fail, not agent_complete."""
    tid = db.create_thread("stuck-topic", session_id="s")
    db.update_thread(tid, status="background")
    _bind_topic(db, tid, "T-stuck", state="background", merged_sha=None)
    agent_id = _busy_agent_on(db, tid)
    _dead_letter_agent_fail(tid)

    _reap(db, IDLE_PANE, backstop_min_age_secs=0.0)

    agent = db.get_agent(agent_id)
    assert agent["status"] == "idle", "stuck-busy agent must be released by the backstop"
    assert agent["assigned_thread"] is None
    assert db.get_thread(tid)["state"] == "background"
    items = db.get_open_action_items()
    assert any(it.get("thread_id") == tid for it in items), (
        "backstop must push a needs-judgment action item, not silently reconcile"
    )
