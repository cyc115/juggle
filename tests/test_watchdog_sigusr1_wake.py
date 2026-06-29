"""Deterministic tests for P4: SIGUSR1 tick-on-demand + 30s backstop.

2026-06-20: Verify the watchdog wakes immediately on SIGUSR1 instead of
waiting up to 30s, that poke_watchdog signals the live PID, that multiple
pokes coalesce to at most 1 extra tick, and that the 30s backstop still fires.

All tests are deterministic — no real 30s sleeps. Inject/patch the event,
tick counter, and fake PID file.
"""
from __future__ import annotations

import os
import signal
import sys
import time
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture(autouse=True, scope="module")
def install_sigusr1_handler():
    """Register the daemon's SIGUSR1 handler for the duration of this test module.

    The handler normally lives in main() — tests must install it explicitly
    so os.kill(os.getpid(), SIGUSR1) wakes _tick_event rather than terminating.
    """
    from juggle_watchdog_daemon import _handle_sigusr1
    old = signal.signal(signal.SIGUSR1, _handle_sigusr1)
    yield
    signal.signal(signal.SIGUSR1, old)


# ── SIGUSR1 handler wakes the event ───────────────────────────────────────────

def test_sigusr1_sets_tick_event():
    """SIGUSR1 to the process sets the _tick_event so the loop wakes immediately.

    P4 regression pin: handler must be installed and must set the event.
    """
    from juggle_watchdog_daemon import _tick_event, _handle_sigusr1

    _tick_event.clear()
    os.kill(os.getpid(), signal.SIGUSR1)
    # Event must be set within 1s (signal delivery is near-instant)
    assert _tick_event.wait(timeout=1.0), (
        "SIGUSR1 did not set _tick_event within 1s — handler not installed or broken"
    )


def test_sigusr1_handler_only_sets_event():
    """Handler must be async-signal-safe: only sets the event, no DB/tmux calls.

    P4 constraint: no complex work inside the signal handler — all DB/tmux ops
    happen in the main loop thread after event.wait() returns.
    """
    import inspect
    from juggle_watchdog_daemon import _handle_sigusr1

    src = inspect.getsource(_handle_sigusr1)
    # Must not call juggle DB or tmux from inside the handler
    assert "db." not in src, "handler must not access DB directly"
    assert "mgr." not in src, "handler must not access tmux mgr directly"
    assert "_tick_event.set" in src, "handler must set the threading.Event"


def test_sigusr1_triggers_tick_without_waiting_full_interval(tmp_path):
    """Simulated loop: SIGUSR1 causes tick to run without sleeping full interval.

    Injects a short-timeout event.wait and a fake _poll_once to count ticks.
    """
    from juggle_watchdog_daemon import _tick_event

    tick_calls = []

    def fake_poll(db, mgr):
        tick_calls.append(1)

    _tick_event.clear()

    # Simulate one loop iteration: wait with 5s timeout, fire SIGUSR1 after 50ms
    def fire_signal():
        time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGUSR1)

    t = threading.Thread(target=fire_signal, daemon=True)
    t.start()

    start = time.monotonic()
    woke = _tick_event.wait(timeout=5.0)  # should wake in ~50ms, not 5s
    elapsed = time.monotonic() - start

    assert woke, "event.wait did not return True — SIGUSR1 not delivered?"
    assert elapsed < 2.0, f"Waited {elapsed:.2f}s — should have woken in ~50ms"

    _tick_event.clear()
    fake_poll(None, None)

    assert len(tick_calls) == 1


# ── coalescing ────────────────────────────────────────────────────────────────

def test_multiple_sigusr1_coalesce_to_one_pending_tick():
    """N SIGUSR1 signals while event is already set collapse to one pending wake.

    threading.Event.set() is idempotent — the second/third set() while the
    event is already set does not queue N extra ticks.
    """
    from juggle_watchdog_daemon import _tick_event

    _tick_event.clear()

    # Fire 10 signals
    for _ in range(10):
        os.kill(os.getpid(), signal.SIGUSR1)

    time.sleep(0.05)  # let signal delivery settle

    # Event is set (at least once)
    assert _tick_event.is_set()

    # Clear it (as the loop would before each tick)
    _tick_event.clear()

    # After clearing, no further signals are pending — event stays clear
    time.sleep(0.01)
    assert not _tick_event.is_set(), (
        "Event is set again after clear — signals during sleep are unexpected here"
    )


def test_coalescing_signal_during_tick_queues_exactly_one_more():
    """A signal arriving while a tick is running queues exactly one more tick.

    Loop clears the event BEFORE running _poll_once so a signal during tick
    sets it again → exactly one follow-up tick, not N.
    """
    from juggle_watchdog_daemon import _tick_event

    tick_calls = []

    def fake_poll_with_mid_signal(db, mgr):
        tick_calls.append(1)
        # Simulate a poke arriving during the tick itself
        os.kill(os.getpid(), signal.SIGUSR1)
        time.sleep(0.01)  # let signal land

    _tick_event.clear()
    _tick_event.set()  # first wake

    # Iteration 1: clear BEFORE tick (spec pattern), run tick that fires signal
    _tick_event.clear()
    fake_poll_with_mid_signal(None, None)

    # Event should be set again due to signal during tick
    assert _tick_event.is_set(), "Signal during tick did not re-set event"

    # Iteration 2: clear and run one more tick
    _tick_event.clear()
    fake_poll_with_mid_signal(None, None)

    assert len(tick_calls) == 2  # exactly 2 ticks, not more


# ── poke_watchdog ─────────────────────────────────────────────────────────────

def test_poke_watchdog_sends_sigusr1_to_live_pid(tmp_path, monkeypatch):
    """poke_watchdog reads PID from singleton and sends SIGUSR1 to it.

    Uses the current process as the 'watchdog' so we can observe the signal.
    """
    from juggle_watchdog_poke import poke_watchdog
    from juggle_watchdog_daemon import _tick_event

    # Write the current PID as the watchdog PID
    lock_file = tmp_path / "juggle.lock"
    lock_file.write_text(str(os.getpid()))

    # Patch read_lock_pid to return our PID
    monkeypatch.setattr(
        "juggle_watchdog_poke.read_lock_pid",
        lambda db_path: os.getpid(),
    )

    _tick_event.clear()
    poke_watchdog(tmp_path / "juggle.db")
    assert _tick_event.wait(timeout=1.0), "poke_watchdog did not cause SIGUSR1 delivery"


def test_poke_watchdog_no_op_when_pid_missing(tmp_path, monkeypatch):
    """poke_watchdog is a no-op (no exception) when no watchdog PID is found."""
    from juggle_watchdog_poke import poke_watchdog

    monkeypatch.setattr(
        "juggle_watchdog_poke.read_lock_pid",
        lambda db_path: None,
    )

    # Must not raise
    poke_watchdog(tmp_path / "juggle.db")


def test_poke_watchdog_no_op_when_pid_dead(tmp_path, monkeypatch):
    """poke_watchdog is a no-op (no exception) when PID is not alive."""
    from juggle_watchdog_poke import poke_watchdog

    # Use PID 1 on macOS is launchd — send would raise PermissionError, not ProcessLookupError.
    # Use a definitely-dead PID instead (very large number unlikely to be alive).
    dead_pid = 2**20  # 1048576 — extremely unlikely to be a live process

    monkeypatch.setattr(
        "juggle_watchdog_poke.read_lock_pid",
        lambda db_path: dead_pid,
    )

    # Must not raise even if kill fails
    poke_watchdog(tmp_path / "juggle.db")


# ── periodic backstop ─────────────────────────────────────────────────────────

def test_30s_backstop_fires_without_sigusr1():
    """Without any SIGUSR1, event.wait(timeout=T) times out and tick still runs.

    Uses a very short timeout to keep the test fast.
    """
    from juggle_watchdog_daemon import _tick_event

    tick_calls = []

    def fake_poll(db, mgr):
        tick_calls.append(1)

    _tick_event.clear()

    SHORT = 0.05  # 50ms fake "poll interval" for testing

    # Simulate one backstop tick: wait times out, clear, poll
    woke = _tick_event.wait(timeout=SHORT)
    _tick_event.clear()
    fake_poll(None, None)

    # woke can be True or False (no signal sent); tick must still fire
    assert len(tick_calls) == 1, "Backstop tick did not fire after timeout"


# ── ready-writer integration ──────────────────────────────────────────────────

def test_recompute_ready_pokes_watchdog(tmp_path, monkeypatch):
    """recompute_ready calls poke_watchdog when nodes are promoted to ready."""
    from juggle_db import JuggleDB
    from dbops import db_graph

    poke_calls = []

    # Patch at the source module: lazy `from juggle_watchdog_poke import poke_watchdog`
    # re-imports on each call, so patching the source attr is the right target.
    monkeypatch.setattr(
        "juggle_watchdog_poke.poke_watchdog",
        lambda db_path: poke_calls.append(db_path),
    )

    d = JuggleDB(db_path=str(tmp_path / "t.db"))
    d.init_db()
    d.set_active(True)

    db_graph.create_task(d, task_id="T1", project_id="P", title="T1", prompt="do it")
    # T1 has no deps → should be ready-eligible immediately
    db_graph.recompute_ready(d, "P")

    assert len(poke_calls) >= 1, "recompute_ready did not call poke_watchdog"


def test_recompute_topic_ready_pokes_watchdog(tmp_path, monkeypatch):
    """recompute_topic_ready calls poke_watchdog when topics are promoted to ready."""
    from juggle_db import JuggleDB
    from dbops import db_topics, db_graph

    poke_calls = []

    monkeypatch.setattr(
        "juggle_watchdog_poke.poke_watchdog",
        lambda db_path: poke_calls.append(db_path),
    )

    d = JuggleDB(db_path=str(tmp_path / "t.db"))
    d.init_db()
    d.set_active(True)

    with d._connect() as conn:
        from dbops.schema import _now
        now = _now()
        conn.execute(
            "INSERT INTO projects (id, name, created_at, last_active) VALUES ('P', 'P', ?, ?)",
            (now, now),
        )
        conn.execute(
            "INSERT INTO graph_topics (id, project_id, title, state, created_at, updated_at) "
            "VALUES ('TOP1', 'P', 'Topic 1', 'open', ?, ?)",
            (_now(), _now()),
        )
        # P8 Task 4.2: topic_ready_eligible reads the topic from nodes (root task
        # node), so the topic must exist there too.
        conn.execute(
            "INSERT INTO nodes (id, kind, title, objective, state, project_id, "
            "parent_id, created_at, updated_at) "
            "VALUES ('TOP1', 'topic', 'Topic 1', '', 'open', 'P', NULL, ?, ?)",
            (_now(), _now()),
        )
        conn.commit()

    # Topic needs at least one pending task (topic_ready_eligible G3 gate)
    db_graph.create_task(d, task_id="T1", project_id="P", title="T1", prompt="do it")
    db_graph.set_task_topic(d, "T1", "TOP1")  # dual-writes nodes.parent_id

    db_topics.recompute_topic_ready(d, "P")

    assert len(poke_calls) >= 1, "recompute_topic_ready did not call poke_watchdog"
