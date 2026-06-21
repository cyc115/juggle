"""Regression pins — global daemon-survivor guard + spawn-neutralizer.

Incident (2026-06-21 daemon-teardown leak; 8 full-suite ERRORS): a test that
spawns a REAL ``uv run python src/juggle_watchdog_daemon.py`` (the cockpit
on_mount self-heal path, or the daemon-entrypoint pin) can leave the DETACHED
python daemon CHILD alive when teardown reaps only the ``uv run`` parent. The
orphan keeps ticking against the test's tmp DB and contaminates the rest of the
suite — the symptom TODO.md tracked as "Full-suite daemon teardown for
cockpit/graph-mode tests" (observed live 2026-06-20: a doctor agent's
full-suite run spawned a daemon on a pytest tmp DB).

Two durable fixes, both pinned here:
  1. ``scoped_daemon_survivors`` — detect a leaked daemon SCOPED to one test's
     own tmp_path (never a concurrent xdist worker's daemon or the prod
     watchdog).  The global autouse guard SIGKILLs + fails on a survivor.
  2. The autouse spawn-neutralizer — no test ever launches a real watchdog
     daemon subprocess via the cockpit / cmd_start ensure path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# 1. Scoped detection helper
# ---------------------------------------------------------------------------


def test_scoped_daemon_survivors_flags_live_own_daemon_only(tmp_path):
    """The guard flags a lock under THIS test's tmp_path whose recorded PID is a
    LIVE daemon, and excludes a dead PID, an alive non-daemon PID, and a
    malformed lock. Pure/injected readers → host-independent (no real ``ps``)."""
    from _daemon_guard import scoped_daemon_survivors

    (tmp_path / ".alive.db.watchdog.lock").write_text("101")   # live daemon -> flagged
    (tmp_path / ".dead.db.watchdog.lock").write_text("202")    # dead -> excluded
    (tmp_path / ".other.db.watchdog.lock").write_text("303")   # alive, non-daemon -> excluded
    (tmp_path / ".junk.db.watchdog.lock").write_text("not-a-pid")  # malformed -> excluded
    (tmp_path / "unrelated.txt").write_text("404")             # not a lock -> ignored

    alive = {101, 303}
    daemon = {101}
    survivors = scoped_daemon_survivors(
        tmp_path,
        is_alive=lambda p: p in alive,
        is_daemon=lambda p: p in daemon,
    )
    assert survivors == [101]


def test_scoped_daemon_survivors_empty_when_no_locks(tmp_path):
    """The common case — no watchdog lock sidecar under tmp_path — is a fast,
    process-scan-free no-op (the 99% of tests that never spawn a daemon)."""
    from _daemon_guard import scoped_daemon_survivors

    (tmp_path / "juggle.db").write_text("")  # a DB but no .watchdog.lock
    assert scoped_daemon_survivors(
        tmp_path, is_alive=lambda p: True, is_daemon=lambda p: True
    ) == []


def test_reap_survivors_signals_each_pid(tmp_path):  # noqa: ARG001
    """``reap_survivors`` SIGKILLs every survivor so a leak never contaminates
    the rest of the suite (injected killer → no real signals)."""
    from _daemon_guard import reap_survivors

    killed: list[int] = []
    reap_survivors([7, 9], killer=killed.append)
    assert killed == [7, 9]


# ---------------------------------------------------------------------------
# 2. Cockpit must not launch a real watchdog daemon during a test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cockpit_run_does_not_launch_real_watchdog_daemon(tmp_path, monkeypatch):
    """``CockpitApp.on_mount`` self-heals a watchdog via ``ensure_watchdog`` → a
    REAL ``uv run python src/juggle_watchdog_daemon.py`` against the test's tmp
    DB (2026-06-20 leak; TODO line 10). No test may launch a real daemon.

    Spies on ``subprocess.Popen`` (returning a fake proc so nothing actually
    spawns) and asserts the cockpit never tried to launch the daemon. RED before
    the autouse spawn-neutralizer; green after.
    """
    import subprocess as _sp

    from juggle_db import JuggleDB

    launched: list = []
    real_popen = _sp.Popen

    def spy_popen(cmd, *a, **k):
        argv = cmd if isinstance(cmd, (list, tuple)) else [cmd]
        if any("juggle_watchdog_daemon.py" in str(x) for x in argv):
            launched.append(list(argv))

            class _Fake:
                pid = -1

                def poll(self):
                    return 0

                def wait(self, timeout=None):  # noqa: ARG002
                    return 0

                def terminate(self):
                    pass

                def kill(self):
                    pass

            return _Fake()
        return real_popen(cmd, *a, **k)

    monkeypatch.setattr(_sp, "Popen", spy_popen)

    import juggle_cockpit

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)

    app = juggle_cockpit.CockpitApp(db_path=db_path)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(0.1)

    assert launched == [], (
        f"cockpit launched a REAL watchdog daemon subprocess during a test: {launched}"
    )


# ---------------------------------------------------------------------------
# 3. The conftest must keep both fixtures wired (anti-silent-removal)
# ---------------------------------------------------------------------------


def test_conftest_wires_daemon_guard_and_spawn_neutralizer():
    """The global conftest must keep BOTH autouse fixtures — the survivor guard
    (fail loud if a daemon leaks) and the spawn-neutralizer (no test launches a
    real daemon). Guards against silent removal in a refactor."""
    src = (Path(__file__).parent / "conftest.py").read_text()
    assert "def _guard_no_leaked_watchdog_daemons" in src, "survivor-guard fixture missing"
    assert "def _no_real_watchdog_daemon_spawn" in src, "spawn-neutralizer fixture missing"
    assert "start_watchdog_detached" in src, "neutralizer must patch start_watchdog_detached"
