"""Async LLM retroactive-reclassify layer (2026-07-22).

Spec: research/2026-07-22-async-reclassify-spec.md. Covers:
  - the move_messages primitive (MessagesMixin) — the spec's resolved BLOCKER
  - juggle_watchdog_reclassify.run_reclassify_sweep: cadence gate, settle
    window, watermark monotonicity, move/stay/new decisions, guarded new-topic
    creation, and fail-safe watermark handling on LLM failure.

All LLM calls are injected via `llm_fn` — no network in these tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def db(tmp_path):
    from juggle_db import JuggleDB

    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


def _mk_thread(db, topic="feature work"):
    return db.create_thread(topic, session_id="s1")


# ---------------------------------------------------------------------------
# move_messages primitive
# ---------------------------------------------------------------------------


def _stamp(db, thread_id, age_s, now):
    """Backdate every message currently on thread_id to now - age_s."""
    from datetime import datetime, timezone

    ts = datetime.fromtimestamp(now - age_s, tz=timezone.utc).isoformat()
    with db._connect() as conn:
        conn.execute(
            "UPDATE messages SET created_at = ? WHERE thread_id = ?", (ts, thread_id)
        )
        conn.commit()


def test_move_messages_reassigns_thread(db):
    src = _mk_thread(db, "src topic")
    dest = _mk_thread(db, "dest topic")
    db.add_message(src, "user", "message one")
    db.add_message(src, "user", "message two")

    src_msgs = db.get_messages(src, token_budget=10_000)
    ids = [m["id"] for m in src_msgs]

    moved = db.move_messages(ids, dest)

    assert moved == 2
    assert db.get_message_count(src, exclude_junk=False) == 0
    assert db.get_message_count(dest, exclude_junk=False) == 2
    dest_node = db.get_thread(dest)
    assert dest_node["last_active_at"] is not None


# ---------------------------------------------------------------------------
# run_reclassify_sweep
# ---------------------------------------------------------------------------

_NOW = 2_000_000_000.0  # fixed epoch so age math is deterministic


def test_reclassify_cadence_gate(db):
    """Within RECLASSIFY_INTERVAL_S of the last run, the sweep is a no-op."""
    from juggle_watchdog_reclassify import RECLASSIFY_INTERVAL_S, run_reclassify_sweep

    db.set_setting("async_reclassify_at", str(_NOW))
    calls = []

    def llm_fn(prompt):
        calls.append(prompt)
        return None

    run_reclassify_sweep(db, now=_NOW + RECLASSIFY_INTERVAL_S - 1, llm_fn=llm_fn)

    assert calls == []


def test_reclassify_stay_leaves_put_and_advances_watermark(db):
    from juggle_watchdog_reclassify import run_reclassify_sweep

    tid = _mk_thread(db, "topic one")
    db.add_message(tid, "user", "keep talking about the same thing please")
    _stamp(db, tid, age_s=60, now=_NOW)

    def llm_fn(prompt):
        return '{"decision": "stay", "thread_id": null, "confidence": 0.9, "topic_hint": null}'

    run_reclassify_sweep(db, now=_NOW, llm_fn=llm_fn)

    assert db.get_message_count(tid, exclude_junk=False) == 1
    watermark = int(db.get_setting("async_reclassify_watermark") or 0)
    assert watermark > 0


def test_reclassify_move_refiles_and_advances_watermark(db):
    from juggle_watchdog_reclassify import run_reclassify_sweep

    src = _mk_thread(db, "src topic")
    dest = _mk_thread(db, "dest topic")
    db.add_message(src, "user", "this actually belongs on the other thread")
    _stamp(db, src, age_s=60, now=_NOW)

    def llm_fn(prompt):
        return (
            '{"decision": "move", "thread_id": "%s", '
            '"confidence": 0.95, "topic_hint": null}' % dest
        )

    run_reclassify_sweep(db, now=_NOW, llm_fn=llm_fn)

    assert db.get_message_count(src, exclude_junk=False) == 0
    assert db.get_message_count(dest, exclude_junk=False) == 1
    watermark = int(db.get_setting("async_reclassify_watermark") or 0)
    assert watermark > 0


def test_reclassify_watermark_no_reprocess(db):
    """Second pass with no new messages makes zero llm_fn calls."""
    from juggle_watchdog_reclassify import RECLASSIFY_INTERVAL_S, run_reclassify_sweep

    tid = _mk_thread(db, "topic one")
    db.add_message(tid, "user", "the only message in this thread")
    _stamp(db, tid, age_s=60, now=_NOW)

    calls = []

    def llm_fn(prompt):
        calls.append(prompt)
        return '{"decision": "stay", "thread_id": null, "confidence": 0.9, "topic_hint": null}'

    run_reclassify_sweep(db, now=_NOW, llm_fn=llm_fn)
    assert len(calls) == 1

    run_reclassify_sweep(db, now=_NOW + RECLASSIFY_INTERVAL_S, llm_fn=llm_fn)
    assert len(calls) == 1  # no NEW messages -> zero additional calls


def test_reclassify_new_topic_guards(db):
    """A 'new' verdict is rejected (no thread created) when guards fail."""
    from juggle_watchdog_reclassify import run_reclassify_sweep

    before_threads = len(db.get_all_threads())
    tid = _mk_thread(db, "topic one")
    db.add_message(tid, "user", "ok")  # under min-word signal
    _stamp(db, tid, age_s=60, now=_NOW)

    def llm_fn(prompt):
        return '{"decision": "new", "thread_id": null, "confidence": 0.95, "topic_hint": "new thing"}'

    run_reclassify_sweep(db, now=_NOW, llm_fn=llm_fn)

    assert len(db.get_all_threads()) == before_threads + 1  # only `tid` created above
    assert db.get_message_count(tid, exclude_junk=False) == 1  # message left in place


def test_reclassify_new_topic_create_failure_is_contained(db):
    """A create_thread failure (e.g. the MAX_THREADS cap) during a 'new'
    decision must not crash the sweep or wedge the watermark forever — DB
    errors are contained the same way a parse failure is: message stays put,
    watermark still advances so the tick keeps making progress."""
    from juggle_watchdog_reclassify import run_reclassify_sweep

    for i in range(9):  # + the message thread below = MAX_THREADS=10 (test env)
        _mk_thread(db, f"filler topic {i}")

    tid = _mk_thread(db, "topic at cap")
    db.add_message(tid, "user", "this new-topic attempt should hit the thread cap")
    _stamp(db, tid, age_s=60, now=_NOW)

    def llm_fn(prompt):
        return '{"decision": "new", "thread_id": null, "confidence": 0.95, "topic_hint": "overflow topic"}'

    run_reclassify_sweep(db, now=_NOW, llm_fn=llm_fn)  # must not raise

    assert db.get_message_count(tid, exclude_junk=False) == 1  # message left in place
    assert int(db.get_setting("async_reclassify_watermark") or 0) > 0  # tick still progressed


def test_reclassify_llm_failure_holds_watermark(db):
    """llm_fn returns None: watermark NOT advanced; next tick retries."""
    from juggle_watchdog_reclassify import run_reclassify_sweep

    tid = _mk_thread(db, "topic one")
    db.add_message(tid, "user", "a message that cannot be classified right now")
    _stamp(db, tid, age_s=60, now=_NOW)

    calls = []

    def llm_fn(prompt):
        calls.append(prompt)
        return None

    run_reclassify_sweep(db, now=_NOW, llm_fn=llm_fn)

    assert len(calls) == 1
    assert int(db.get_setting("async_reclassify_watermark") or 0) == 0


# ---------------------------------------------------------------------------
# Regression pins (2026-07-22 async-reclassify)
# ---------------------------------------------------------------------------


def test_reclassify_respects_settle_window(db):
    """PIN 2026-07-22: a message younger than SETTLE_WINDOW_S is NEVER moved —
    the sync router just placed the tail and the orchestrator may be answering
    it. RED before the settle guard existed."""
    from juggle_watchdog_reclassify import run_reclassify_sweep

    src = _mk_thread(db, "src topic")
    dest = _mk_thread(db, "dest topic")
    db.add_message(src, "user", "brand new message just typed")
    _stamp(db, src, age_s=5, now=_NOW)  # well inside the 20s settle window

    calls = []

    def llm_fn(prompt):
        calls.append(prompt)
        return (
            '{"decision": "move", "thread_id": "%s", '
            '"confidence": 0.99, "topic_hint": null}' % dest
        )

    run_reclassify_sweep(db, now=_NOW, llm_fn=llm_fn)

    assert calls == []
    assert db.get_message_count(src, exclude_junk=False) == 1
    assert db.get_message_count(dest, exclude_junk=False) == 0


def test_reclassify_never_advances_current_thread_by_default(db):
    """PIN 2026-07-22: async layer must not race the live cursor. A 'move'
    verdict re-files the message but leaves current_thread unchanged (default-
    off CAS). RED if the advance is unconditional."""
    from juggle_watchdog_reclassify import run_reclassify_sweep

    src = _mk_thread(db, "src topic")
    dest = _mk_thread(db, "dest topic")
    other = _mk_thread(db, "unrelated live thread")
    db.add_message(src, "user", "this message will be moved to dest")
    _stamp(db, src, age_s=60, now=_NOW)
    # current_thread points elsewhere so this run is not the live cursor's
    # message (the always-on never-move-live-tail guard is a separate concern
    # from the CAS advance flag under test here).
    db.set_current_thread(other)

    def llm_fn(prompt):
        return (
            '{"decision": "move", "thread_id": "%s", '
            '"confidence": 0.95, "topic_hint": null}' % dest
        )

    run_reclassify_sweep(db, now=_NOW, llm_fn=llm_fn)

    assert db.get_message_count(dest, exclude_junk=False) == 1  # message did move
    assert db.get_current_thread() == other  # but the live cursor did not


def test_reclassify_watermark_monotonic(db):
    """PIN 2026-07-22: no double-process. A message with id <= watermark is
    never re-classified. RED if the pass keys on timestamp instead of id."""
    from juggle_watchdog_reclassify import RECLASSIFY_INTERVAL_S, run_reclassify_sweep

    tid = _mk_thread(db, "topic one")
    db.add_message(tid, "user", "first message ever sent here")
    _stamp(db, tid, age_s=60, now=_NOW)

    calls = []

    def llm_fn(prompt):
        calls.append(prompt)
        return '{"decision": "stay", "thread_id": null, "confidence": 0.9, "topic_hint": null}'

    run_reclassify_sweep(db, now=_NOW, llm_fn=llm_fn)
    assert len(calls) == 1

    # A second message arrives; only IT should be classified next pass.
    db.add_message(tid, "user", "a brand new second message here")
    _stamp(db, tid, age_s=60, now=_NOW + RECLASSIFY_INTERVAL_S)

    run_reclassify_sweep(db, now=_NOW + RECLASSIFY_INTERVAL_S, llm_fn=llm_fn)
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Watchdog tick wiring
# ---------------------------------------------------------------------------


def test_daemon_tick_wires_reclassify_sweep():
    """The watchdog tick body invokes the guarded reclassify sweep (2026-07-22).

    Wired through juggle_watchdog_sweeps.run_agent_health_sweeps (not inline in
    _poll_once) to stay within the juggle_watchdog_daemon.py LOC-gate budget —
    the daemon already calls run_agent_health_sweeps once per tick (spec Q2)."""
    import inspect

    import juggle_watchdog_daemon as wd
    import juggle_watchdog_sweeps as sweeps

    assert "run_agent_health_sweeps" in inspect.getsource(wd._poll_once)
    assert "run_reclassify_sweep" in inspect.getsource(sweeps.run_agent_health_sweeps)
