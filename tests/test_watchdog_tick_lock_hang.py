"""Regression pin — INCIDENT 2026-07-02: watchdog tick hung 10+ min in summary
cache warming (T-fix-watchdog-tick-lock-hang).

`sample` on the hung prod watchdog (PID 63539) showed the main thread parked
forever in PyThread_acquire_lock during its FIRST tick, blocking dispatch for
~40 minutes. Prime suspect: the eager summary-cache warmer
(`warm_stale_summaries`, T-summary-eager-gen) ran synchronously inside
`_poll_once`, so any internal stall (LLM call, lock, join) stalled the whole
watchdog loop — no dispatch/advance/drain until it returned.

Fix: summary warming runs fire-and-forget on a background thread, guarded by
a non-blocking busy-skip lock, so it can never stall `_poll_once` regardless
of how long it takes internally.
"""
from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _load_daemon_module(monkeypatch_modules: dict):
    """Load juggle_watchdog_daemon fresh, with its heavy imports mocked out.

    Mirrors the pattern used in tests/test_reaper_daemons.py so this module
    can be imported without a real DB/tmux/settings stack.
    """
    import importlib.machinery
    import importlib.util
    import tempfile

    src_dir = Path(__file__).parent.parent / "src"
    loader = importlib.machinery.SourceFileLoader(
        "juggle_watchdog_daemon_hangpin", str(src_dir / "juggle_watchdog_daemon.py")
    )
    spec = importlib.util.spec_from_loader("juggle_watchdog_daemon_hangpin", loader)
    mod = importlib.util.module_from_spec(spec)

    settings = {
        "paths": {"config_dir": tempfile.mkdtemp()},
        "agent_boot_grace_secs": 120,
        "agent_idle_ttl_secs": 43200,
        "watchdog": {"max_daemons": 5},
    }
    js_mock = mock.MagicMock()
    js_mock.get_settings = mock.Mock(return_value=settings)

    _mocks = {
        "juggle_db": mock.MagicMock(),
        "juggle_settings": js_mock,
        "juggle_tmux": mock.MagicMock(),
        "juggle_watchdog": mock.MagicMock(),
        "juggle_watchdog_health": mock.MagicMock(),
        **monkeypatch_modules,
    }
    with mock.patch.dict("sys.modules", _mocks):
        spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def daemon_mod():
    return _load_daemon_module({})


def _fake_db():
    db = mock.MagicMock()
    db.get_all_agents.return_value = []
    return db


def test_poll_once_never_blocks_on_hung_summary_warmer(daemon_mod):
    """REGRESSION PIN 2026-07-02: a summary-warming worker that never returns
    must not stall _poll_once — it must run fire-and-forget in the background.

    Pre-fix: _poll_once called warm_stale_summaries synchronously, so a hang
    inside it (LLM call / lock / join) blocked the whole tick — and therefore
    every subsequent tick's dispatch/advance/drain — forever.
    """
    hang_started = threading.Event()
    release = threading.Event()

    def _hanging_warm_stale_summaries(db, *a, **kw):
        hang_started.set()
        # Bounded "forever" — long enough to prove _poll_once didn't wait,
        # short enough not to leak past this test.
        release.wait(timeout=10)
        return 0

    warmer_mock = mock.MagicMock(warm_stale_summaries=_hanging_warm_stale_summaries)

    db = _fake_db()
    mgr = mock.MagicMock()

    settings = {"paths": {"config_dir": tempfile.mkdtemp()}, "watchdog": {}}
    with mock.patch.dict("sys.modules", {"juggle_summary_warmer": warmer_mock}):
        with mock.patch.object(daemon_mod, "get_settings", return_value=settings):
            with mock.patch.object(daemon_mod, "check_orphaned_threads"):
                with mock.patch.object(daemon_mod, "get_session_id", return_value="s1"):
                    with mock.patch.object(daemon_mod, "write_heartbeat", mock.MagicMock()):
                        t0 = time.monotonic()
                        daemon_mod._poll_once(db, mgr)
                        elapsed = time.monotonic() - t0

    try:
        assert elapsed < 2.0, (
            f"_poll_once took {elapsed:.2f}s — hung summary warmer stalled the tick"
        )
        assert hang_started.wait(timeout=2.0), "warming worker never started"
    finally:
        release.set()  # let the background thread finish so it doesn't leak


def test_summary_warming_skips_when_previous_sweep_still_busy(daemon_mod):
    """A second tick arriving while warming is still in flight must skip
    (not queue/block) rather than piling up background threads."""
    release = threading.Event()
    call_count = {"n": 0}

    def _slow(db, *a, **kw):
        call_count["n"] += 1
        release.wait(timeout=10)
        return 0

    warmer_mock = mock.MagicMock(warm_stale_summaries=_slow)

    db = _fake_db()
    mgr = mock.MagicMock()

    settings = {"paths": {"config_dir": tempfile.mkdtemp()}, "watchdog": {}}
    try:
        with mock.patch.dict("sys.modules", {"juggle_summary_warmer": warmer_mock}):
            with mock.patch.object(daemon_mod, "get_settings", return_value=settings):
                with mock.patch.object(daemon_mod, "check_orphaned_threads"):
                    with mock.patch.object(daemon_mod, "get_session_id", return_value="s1"):
                        with mock.patch.object(daemon_mod, "write_heartbeat", mock.MagicMock()):
                            daemon_mod._poll_once(db, mgr)
                            # Give the background thread a moment to start.
                            time.sleep(0.2)
                            daemon_mod._poll_once(db, mgr)
                            time.sleep(0.2)
        assert call_count["n"] == 1, "second tick should have skipped, not started a new warmer"
    finally:
        release.set()


# ── Tick-of-tick watchdog (point 2) ───────────────────────────────────────────


def test_tick_watchdog_fires_when_not_cancelled_in_time():
    """A tick budget that elapses without cancel() must log + dump stacks."""
    import juggle_watchdog_tickguard as tg

    with mock.patch.object(tg, "_log") as log_mock:
        timer = tg.arm_tick_watchdog(interval=0, multiplier=0.05)
        try:
            timer.join(timeout=1.0)
            assert log_mock.error.called, "budget timer did not fire"
        finally:
            timer.cancel()


def test_tick_watchdog_silent_when_cancelled_before_budget():
    """A normal, fast tick that cancels the timer must never log the hang error."""
    import juggle_watchdog_tickguard as tg

    with mock.patch.object(tg, "_log") as log_mock:
        timer = tg.arm_tick_watchdog(interval=10, multiplier=3)
        timer.cancel()
        time.sleep(0.1)
        assert not log_mock.error.called
