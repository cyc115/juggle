"""Phase 1 pin — `juggle start` is the CLI start/unfreeze path for the watchdog.

Incident: 2026-06-20 watchdog-start-unfreeze.
Symptom: a CLI user who ran `stop-watchdog --freeze` was stranded — no CLI command
cleared the freeze sentinel (only the cockpit W/R hotkeys did), so the watchdog
stayed frozen forever. `juggle start` activated the session but never touched the
watchdog or the freeze sentinel.

Fix: cmd_start must clear the freeze sentinel (unfreeze_watchdog) AND ensure the
watchdog is up (ensure_watchdog force=True), reusing the singleton primitives.
"""

import sys
from pathlib import Path
from unittest.mock import Mock, patch

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def _mock_db(db_path: Path) -> Mock:
    db = Mock()
    db.db_path = db_path
    db.init_db = Mock()
    db.set_active = Mock()
    db.set_orchestrator_session_id = Mock()
    db.get_all_threads = Mock(return_value=[])
    db.create_thread = Mock(return_value="thread-uuid-1")
    db.set_current_thread = Mock()
    db.get_thread = Mock(return_value={"id": "thread-uuid-1", "label": "General"})
    return db


def test_cmd_start_clears_freeze_and_brings_watchdog_alive(tmp_path, monkeypatch):
    """With the freeze sentinel set, cmd_start clears it AND the watchdog ends up
    alive — driven through the injected spawn seam (no real prod daemon)."""
    import juggle_cmd_threads as cmd
    import juggle_watchdog_singleton as ws

    db_path = tmp_path / "x.db"
    db_path.write_text("")
    db = _mock_db(db_path)

    # Freeze it first (simulating a prior `stop-watchdog --freeze`).
    ws.freeze_watchdog(str(db_path))
    assert ws.is_watchdog_frozen(str(db_path)) is True

    # Simulate the daemon coming alive once spawn is invoked: the spawn callback
    # flips a flag, and is_watchdog_alive reads it (the singleton lock seam).
    alive = {"v": False}
    spawns: list = []

    def fake_spawn(p, *, repo_path=None):
        spawns.append(p)
        alive["v"] = True

    monkeypatch.setattr(ws, "is_watchdog_alive", lambda p: alive["v"])

    captured = {}

    real_ensure = ws.ensure_watchdog

    def ensure_spy(p, **kw):
        kw.setdefault("spawn", fake_spawn)
        kw.setdefault("survive_timeout", 0.2)
        kw.setdefault("min_respawn_interval", 0.0)
        captured["ensure_alive"] = real_ensure(p, **kw)
        return captured["ensure_alive"]

    with patch("juggle_cmd_threads.get_db", return_value=db), \
         patch("juggle_cmd_threads._DATA_DIR", tmp_path), \
         patch("juggle_cmd_threads._maybe_start_talkback"), \
         patch("juggle_cmd_threads._get_version", return_value="1.0"), \
         patch.object(ws, "ensure_watchdog", ensure_spy), \
         patch("builtins.print"):
        cmd.cmd_start(None)

    # 1) freeze sentinel cleared by the explicit start
    assert ws.is_watchdog_frozen(str(db_path)) is False, "cmd_start must unfreeze"
    # 2) the watchdog ended up alive via the spawn seam
    assert spawns == [str(db_path)], "cmd_start must ensure the watchdog (spawn once)"
    assert captured.get("ensure_alive") is True, "watchdog must end up alive"
    assert ws.is_watchdog_alive(str(db_path)) is True
