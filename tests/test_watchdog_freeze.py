"""Phase D — working freeze sentinel.

Pins the 2026-06-20 incident: the defect-protocol "freeze" (stop-watchdog)
could not actually hold because the cockpit re-spawned the daemon every 15s.
A freeze sentinel makes the freeze authoritative — while set, ensure_watchdog
is a hard no-op even when no daemon is alive — so `stop-watchdog --freeze`
genuinely stops the respawn churn until an explicit start/unfreeze.
"""

import sys
from pathlib import Path

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_ensure_watchdog_is_noop_when_frozen(tmp_path, monkeypatch):
    """With the freeze sentinel set, ensure_watchdog must not spawn even when
    NO daemon is alive (2026-06-20: the 15s respawn defeated stop-watchdog)."""
    import juggle_watchdog_singleton as ws

    db = tmp_path / "x.db"
    db.write_text("")
    monkeypatch.setattr(ws, "is_watchdog_alive", lambda p: False)

    ws.freeze_watchdog(str(db))
    assert ws.is_watchdog_frozen(str(db)) is True

    spawns = []
    res = ws.ensure_watchdog(
        str(db),
        spawn=lambda *a, **k: spawns.append(1),
        survive_timeout=0.0,
        min_respawn_interval=0.0,
    )
    assert res is False
    assert spawns == [], "frozen watchdog must never (re)spawn"


def test_force_does_not_override_freeze(tmp_path, monkeypatch):
    """Even force=True must respect the freeze — only an explicit unfreeze lifts
    it (the debounce-bypass must not become a freeze-bypass)."""
    import juggle_watchdog_singleton as ws

    db = tmp_path / "x.db"
    db.write_text("")
    monkeypatch.setattr(ws, "is_watchdog_alive", lambda p: False)
    ws.freeze_watchdog(str(db))

    spawns = []
    res = ws.ensure_watchdog(
        str(db), spawn=lambda *a, **k: spawns.append(1),
        survive_timeout=0.0, force=True,
    )
    assert res is False
    assert spawns == []


def test_unfreeze_restores_spawning(tmp_path, monkeypatch):
    """After unfreeze, ensure_watchdog spawns again."""
    import juggle_watchdog_singleton as ws

    db = tmp_path / "x.db"
    db.write_text("")
    monkeypatch.setattr(ws, "is_watchdog_alive", lambda p: False)

    ws.freeze_watchdog(str(db))
    ws.unfreeze_watchdog(str(db))
    assert ws.is_watchdog_frozen(str(db)) is False

    spawns = []
    ws.ensure_watchdog(
        str(db), spawn=lambda *a, **k: spawns.append(1),
        survive_timeout=0.0, min_respawn_interval=0.0,
    )
    assert spawns == [1]


def test_explicit_toggle_start_clears_freeze(tmp_path, monkeypatch):
    """W-hotkey 'start' (toggle_watchdog) is an explicit user start → unfreeze."""
    import juggle_watchdog_singleton as ws

    db = tmp_path / "x.db"
    db.write_text("")
    monkeypatch.setattr(ws, "is_watchdog_alive", lambda p: False)
    ws.freeze_watchdog(str(db))

    spawns = []
    action = ws.toggle_watchdog(
        str(db), spawn=lambda *a, **k: spawns.append(1)
    )
    assert action == "started"
    assert ws.is_watchdog_frozen(str(db)) is False
    assert spawns == [1], "explicit start must unfreeze and spawn"


def test_stop_watchdog_freeze_flag_sets_sentinel(tmp_path, monkeypatch):
    """`juggle stop-watchdog --freeze` sets the sentinel so the freeze holds."""
    import juggle_cmd_agents as cmd
    import juggle_watchdog_singleton as ws

    db = tmp_path / "x.db"
    db.write_text("")

    # Point cmd_stop_watchdog at the tmp DB + stub the kill + pidfile cleanup.
    monkeypatch.setattr(ws, "terminate_all_watchdogs", lambda *a, **k: [])

    class _Args:
        freeze = True

    monkeypatch.setenv("JUGGLE_DB_PATH", str(db))
    cmd.cmd_stop_watchdog(_Args())

    assert ws.is_watchdog_frozen(str(db)) is True


def test_stop_watchdog_without_freeze_does_not_set_sentinel(tmp_path, monkeypatch):
    """Plain stop-watchdog (no --freeze) must NOT leave a sticky freeze."""
    import juggle_cmd_agents as cmd
    import juggle_watchdog_singleton as ws

    db = tmp_path / "x.db"
    db.write_text("")
    monkeypatch.setattr(ws, "terminate_all_watchdogs", lambda *a, **k: [])

    class _Args:
        freeze = False

    monkeypatch.setenv("JUGGLE_DB_PATH", str(db))
    cmd.cmd_stop_watchdog(_Args())

    assert ws.is_watchdog_frozen(str(db)) is False
