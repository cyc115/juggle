"""Phase C — ensure_watchdog debounce + boot-window grace.

Pins the 2026-06-20 watchdog respawn-storm incident: the cockpit's
set_interval(15.0, self._ensure_watchdog) respawned a daemon every 15s whenever
the per-DB flock was momentarily free during a slow `uv run` cold-start (32 prod
boots, exact 15.0s cadence in the spawn log; 24 "killed previous instance"
events). Each tick saw is_watchdog_alive() == False (the new daemon had not yet
acquired the lock) and spawned ANOTHER daemon.

Fix: a min-respawn-interval. After a spawn, ensure_watchdog records a timestamp
sidecar and suppresses the next respawn within the window even if the lock is
not yet held — giving the booting daemon time to take the lock.
"""

import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_two_ensures_within_grace_spawn_only_once(tmp_path, monkeypatch):
    """Two ensures inside the grace window cause exactly ONE spawn.

    2026-06-20 respawn storm: a slow-booting daemon hasn't taken the lock yet,
    so back-to-back ensures each saw 'no live watchdog' and respawned.
    """
    import juggle_watchdog_singleton as ws

    db = tmp_path / "x.db"
    db.write_text("")

    # Daemon never appears alive (simulates a slow cold-start that hasn't
    # acquired the lock yet) — the ONLY thing that should stop the 2nd spawn is
    # the debounce.
    monkeypatch.setattr(ws, "is_watchdog_alive", lambda p: False)

    clock = {"t": 1000.0}
    monkeypatch.setattr(ws.time, "monotonic", lambda: clock["t"])

    spawns = []
    def fake_spawn(p, *, repo_path=None):
        spawns.append(clock["t"])
        return 4242

    # First ensure → spawns.
    ws.ensure_watchdog(
        str(db), spawn=fake_spawn, survive_timeout=0.0, min_respawn_interval=60.0
    )
    # Second ensure 15s later (the cockpit cadence) → suppressed by debounce.
    clock["t"] += 15.0
    ws.ensure_watchdog(
        str(db), spawn=fake_spawn, survive_timeout=0.0, min_respawn_interval=60.0
    )

    assert len(spawns) == 1, f"expected ONE spawn within grace, got {len(spawns)}"


def test_ensure_respawns_after_grace_window(tmp_path, monkeypatch):
    """Once the grace window elapses, a dead daemon is respawned (self-heal)."""
    import juggle_watchdog_singleton as ws

    db = tmp_path / "x.db"
    db.write_text("")
    monkeypatch.setattr(ws, "is_watchdog_alive", lambda p: False)

    clock = {"t": 1000.0}
    monkeypatch.setattr(ws.time, "monotonic", lambda: clock["t"])

    spawns = []
    def fake_spawn(p, *, repo_path=None):
        spawns.append(clock["t"])
        return 1

    ws.ensure_watchdog(str(db), spawn=fake_spawn, survive_timeout=0.0,
                       min_respawn_interval=60.0)
    clock["t"] += 61.0  # past the window
    ws.ensure_watchdog(str(db), spawn=fake_spawn, survive_timeout=0.0,
                       min_respawn_interval=60.0)

    assert len(spawns) == 2, "should respawn once the debounce window passes"


def test_live_watchdog_is_still_a_noop(tmp_path, monkeypatch):
    """A live watchdog short-circuits before any spawn/debounce (unchanged)."""
    import juggle_watchdog_singleton as ws

    db = tmp_path / "x.db"
    db.write_text("")
    monkeypatch.setattr(ws, "is_watchdog_alive", lambda p: True)

    spawns = []
    res = ws.ensure_watchdog(
        str(db), spawn=lambda *a, **k: spawns.append(1),
        min_respawn_interval=60.0,
    )
    assert res is False
    assert spawns == []


def test_explicit_restart_bypasses_debounce(tmp_path, monkeypatch):
    """The R-hotkey restart must relaunch even inside the debounce window.

    2026-06-20: the debounce must NOT defeat an explicit user restart — only
    the automatic 15s cockpit ensure is throttled.
    """
    import juggle_watchdog_singleton as ws

    db = tmp_path / "x.db"
    db.write_text("")
    monkeypatch.setattr(ws, "is_watchdog_alive", lambda p: False)
    monkeypatch.setattr(ws, "stop_watchdog", lambda p, **k: False)

    clock = {"t": 2000.0}
    monkeypatch.setattr(ws.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(ws.time, "sleep", lambda s: None)

    spawns = []
    def fake_spawn(p, *, repo_path=None):
        spawns.append(clock["t"])

    # Prime a recent spawn stamp (automatic ensure just fired).
    ws.ensure_watchdog(str(db), spawn=fake_spawn, survive_timeout=0.0,
                       min_respawn_interval=60.0)
    assert len(spawns) == 1
    # Explicit restart 5s later — must spawn despite the debounce window.
    clock["t"] += 5.0
    ws.restart_watchdog(str(db), spawn=fake_spawn, survive_timeout=0.0)
    assert len(spawns) == 2, "explicit restart must bypass the debounce"


def test_force_flag_bypasses_debounce(tmp_path, monkeypatch):
    """ensure_watchdog(force=True) ignores the debounce window."""
    import juggle_watchdog_singleton as ws

    db = tmp_path / "x.db"
    db.write_text("")
    monkeypatch.setattr(ws, "is_watchdog_alive", lambda p: False)

    clock = {"t": 100.0}
    monkeypatch.setattr(ws.time, "monotonic", lambda: clock["t"])

    spawns = []
    def fake_spawn(p, *, repo_path=None):
        spawns.append(clock["t"])

    ws.ensure_watchdog(str(db), spawn=fake_spawn, survive_timeout=0.0,
                       min_respawn_interval=60.0)
    clock["t"] += 1.0
    ws.ensure_watchdog(str(db), spawn=fake_spawn, survive_timeout=0.0,
                       min_respawn_interval=60.0, force=True)
    assert len(spawns) == 2


def test_default_min_respawn_interval_from_settings(tmp_path, monkeypatch):
    """When min_respawn_interval is not passed, the window comes from settings."""
    import juggle_watchdog_singleton as ws

    db = tmp_path / "x.db"
    db.write_text("")
    monkeypatch.setattr(ws, "is_watchdog_alive", lambda p: False)

    clock = {"t": 5000.0}
    monkeypatch.setattr(ws.time, "monotonic", lambda: clock["t"])

    spawns = []
    def fake_spawn(p, *, repo_path=None):
        spawns.append(clock["t"])

    # No min_respawn_interval kwarg → must read the settings default (60s) and
    # still debounce a 15s-later second ensure.
    ws.ensure_watchdog(str(db), spawn=fake_spawn, survive_timeout=0.0)
    clock["t"] += 15.0
    ws.ensure_watchdog(str(db), spawn=fake_spawn, survive_timeout=0.0)

    assert len(spawns) == 1
