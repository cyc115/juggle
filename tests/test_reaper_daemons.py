"""Phase B — watchdog-daemon reaper + global daemon cap.

Pins the 2026-06-20 watchdog-daemon leak incident: ~109 detached
juggle_watchdog_daemon.py processes accumulated over ~8h. The core gap (RCA §6)
was that NOTHING reaps a watchdog daemon whose worktree/DB has vanished — a
daemon launched against /private/tmp/juggle-repo-XXX keeps ticking forever after
that worktree is deleted — and the per-DB flock is not a global cap, so N
distinct tmp DBs could run N daemons simultaneously.

These tests drive the pure reaper seams (injected readers/killers/log) so they
never touch a real process.
"""

import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


# ---------------------------------------------------------------------------
# daemon_is_orphan — the liveness predicate
# ---------------------------------------------------------------------------


def test_daemon_is_orphan_when_db_file_missing(tmp_path):
    from juggle_reaper import daemon_is_orphan

    missing_db = str(tmp_path / "gone.db")
    live_cwd = str(tmp_path)
    assert daemon_is_orphan(missing_db, live_cwd) is True


def test_daemon_is_orphan_when_cwd_missing(tmp_path):
    from juggle_reaper import daemon_is_orphan

    db = tmp_path / "live.db"
    db.write_text("x")
    missing_cwd = str(tmp_path / "deleted-worktree")
    assert daemon_is_orphan(str(db), missing_cwd) is True


def test_daemon_not_orphan_when_both_present(tmp_path):
    from juggle_reaper import daemon_is_orphan

    db = tmp_path / "live.db"
    db.write_text("x")
    assert daemon_is_orphan(str(db), str(tmp_path)) is False


def test_daemon_not_orphan_when_db_path_unknown(tmp_path):
    """If we can't read the daemon's DB path, be conservative — never reap."""
    from juggle_reaper import daemon_is_orphan

    assert daemon_is_orphan(None, str(tmp_path)) is False


# ---------------------------------------------------------------------------
# reap_orphan_watchdog_daemons — SIGTERM the daemons whose worktree/DB is gone
# ---------------------------------------------------------------------------


def test_reaper_kills_orphan_daemon_only(tmp_path):
    """A daemon whose DB is gone is killed; a live-DB daemon is spared.

    2026-06-20 leak: orphaned tmp/worktree daemons were never reaped and
    accumulated unbounded.
    """
    from juggle_reaper import reap_orphan_watchdog_daemons

    live_db = tmp_path / "prod.db"
    live_db.write_text("x")
    gone_db = str(tmp_path / "tmp-worktree.db")  # never created → orphan

    db_paths = {101: str(live_db), 202: gone_db}
    cwds = {101: str(tmp_path), 202: str(tmp_path)}
    killed = []
    logs = []

    result = reap_orphan_watchdog_daemons(
        pids=[101, 202],
        db_path_reader=lambda pid: db_paths.get(pid),
        cwd_reader=lambda pid: cwds.get(pid),
        killer=lambda pid: killed.append(pid),
        log=logs.append,
    )

    assert result == [202]
    assert killed == [202]
    # live daemon untouched
    assert 101 not in killed
    # the kill is logged (no silent reap)
    assert any("202" in m for m in logs)


def test_reaper_spares_daemon_with_unreadable_db_path(tmp_path):
    """Unreadable DB path ⇒ conservative skip (don't kill an unknown)."""
    from juggle_reaper import reap_orphan_watchdog_daemons

    killed = []
    result = reap_orphan_watchdog_daemons(
        pids=[303],
        db_path_reader=lambda pid: None,
        cwd_reader=lambda pid: str(tmp_path),
        killer=lambda pid: killed.append(pid),
        log=lambda m: None,
    )
    assert result == []
    assert killed == []


# ---------------------------------------------------------------------------
# enforce_daemon_cap — global backstop that LOGS when it fires (RCA P1)
# ---------------------------------------------------------------------------


def test_daemon_cap_kills_oldest_over_cap_and_logs():
    """Over the cap, the oldest daemons are killed and the cap fire is logged.

    2026-06-20 leak: the per-DB flock is not a global cap; N distinct tmp DBs
    could run N daemons. A global cap is the backstop — and it must never be
    silent.
    """
    from juggle_reaper import enforce_daemon_cap

    # start times: lower = older. 5 daemons, cap 3 → kill the 2 oldest.
    starts = {1: 100.0, 2: 200.0, 3: 300.0, 4: 400.0, 5: 500.0}
    killed = []
    logs = []

    result = enforce_daemon_cap(
        3,
        pids=list(starts),
        start_time_reader=lambda pid: starts.get(pid),
        killer=lambda pid: killed.append(pid),
        log=logs.append,
    )

    # oldest two (1, 2) killed; newest three (3,4,5) survive
    assert sorted(result) == [1, 2]
    assert sorted(killed) == [1, 2]
    assert logs, "cap firing must be logged (no silent cap)"


def test_daemon_cap_noop_under_cap():
    from juggle_reaper import enforce_daemon_cap

    killed = []
    logs = []
    result = enforce_daemon_cap(
        5,
        pids=[1, 2],
        start_time_reader=lambda pid: float(pid),
        killer=lambda pid: killed.append(pid),
        log=logs.append,
    )
    assert result == []
    assert killed == []
    assert logs == []  # no fire ⇒ no log


def test_daemon_cap_zero_or_negative_is_disabled():
    """A cap of 0 / negative disables the backstop (never kill)."""
    from juggle_reaper import enforce_daemon_cap

    killed = []
    result = enforce_daemon_cap(
        0,
        pids=[1, 2, 3],
        start_time_reader=lambda pid: float(pid),
        killer=lambda pid: killed.append(pid),
        log=lambda m: None,
    )
    assert result == []
    assert killed == []


# ---------------------------------------------------------------------------
# Wiring pin: the watchdog tick must actually invoke the daemon reaper.
# ---------------------------------------------------------------------------


def test_poll_once_invokes_daemon_reaper(tmp_path):
    """_poll_once must call reap_watchdog_daemons_tick.

    2026-06-20 leak: the daemon loop had no daemon reaper at all. This pin
    fails if a refactor drops the wiring (the leak would silently return).
    """
    import importlib.machinery
    import importlib.util
    import logging
    import tempfile
    from unittest import mock

    src_dir = Path(__file__).parent.parent / "src"
    loader = importlib.machinery.SourceFileLoader(
        "juggle_watchdog_daemon_pin", str(src_dir / "juggle_watchdog_daemon.py")
    )
    spec = importlib.util.spec_from_loader("juggle_watchdog_daemon_pin", loader)
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
    }
    with mock.patch.dict("sys.modules", _mocks):
        spec.loader.exec_module(mod)

    reaper_mock = mock.MagicMock()
    reaper_module_mock = mock.MagicMock(reap_watchdog_daemons_tick=reaper_mock)

    db = mock.MagicMock()
    db.get_all_agents.return_value = []
    mgr = mock.MagicMock()

    with mock.patch.dict("sys.modules", {"juggle_reaper": reaper_module_mock}):
        with mock.patch.object(mod, "get_settings", return_value=settings):
            with mock.patch.object(mod, "check_orphaned_threads"):
                with mock.patch.object(mod, "get_session_id", return_value="s1"):
                    with mock.patch.object(mod, "write_heartbeat", mock.MagicMock()):
                        mod._poll_once(db, mgr)

    reaper_mock.assert_called_once_with(5)
    logging.disable(logging.NOTSET)
