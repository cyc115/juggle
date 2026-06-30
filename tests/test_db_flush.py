"""Tests for juggle_cmd_db_flush (Task 6)."""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _make_db(path: Path):
    from juggle_db import JuggleDB
    db = JuggleDB(db_path=str(path))
    db.init_db()
    return db


def test_flush_once_copies_live_to_durable(tmp_path):
    """flush_once copies live DB content to durable path atomically."""
    from juggle_cmd_db_flush import flush_once
    live = tmp_path / "live.db"
    durable = tmp_path / "durable.db"
    _make_db(live)

    # Write a marker row to live
    conn = sqlite3.connect(str(live))
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('test-uuid-1', 'test')"
    )
    conn.commit()
    conn.close()

    flush_once(live, durable)

    assert durable.exists(), "durable should exist after flush"
    conn2 = sqlite3.connect(str(durable))
    row = conn2.execute(
        "SELECT key FROM settings WHERE key='test-uuid-1'"
    ).fetchone()
    conn2.close()
    assert row is not None, "flushed data should be in durable"


def test_flush_once_atomic_on_interrupt(tmp_path):
    """flush_once leaves durable intact if interrupted (uses tmp+rename)."""
    from juggle_cmd_db_flush import flush_once
    live = tmp_path / "live.db"
    durable = tmp_path / "durable.db"
    _make_db(live)
    _make_db(durable)  # pre-existing durable

    # Write original content to durable
    conn_d = sqlite3.connect(str(durable))
    conn_d.execute(
        "INSERT INTO settings (key, value) VALUES ('original-row', 'orig')"
    )
    conn_d.commit()
    conn_d.close()

    # Normal flush
    flush_once(live, durable)
    # durable still readable
    conn2 = sqlite3.connect(str(durable))
    tables = {r[0] for r in conn2.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn2.close()
    assert "nodes" in tables


def test_flush_status_returns_dict(tmp_path):
    """flush_status returns a dict with last_flush_at and age_s fields."""
    from juggle_cmd_db_flush import flush_once, flush_status
    live = tmp_path / "live.db"
    durable = tmp_path / "durable.db"
    _make_db(live)

    flush_once(live, durable)
    status = flush_status(durable)

    assert isinstance(status, dict)
    assert "last_flush_at" in status
    assert "age_s" in status
    assert isinstance(status["age_s"], (int, float))


def test_flush_status_no_flush_yet(tmp_path):
    """flush_status returns None/null last_flush_at when no flush has occurred."""
    from juggle_cmd_db_flush import flush_status
    durable = tmp_path / "durable.db"
    # durable doesn't exist yet
    status = flush_status(durable)
    assert status["last_flush_at"] is None


def test_flush_status_age_increases_after_flush(tmp_path):
    """age_s in flush_status is >= 0 after a flush."""
    from juggle_cmd_db_flush import flush_once, flush_status
    live = tmp_path / "live.db"
    durable = tmp_path / "durable.db"
    _make_db(live)

    flush_once(live, durable)
    status = flush_status(durable)
    assert status["age_s"] >= 0


# ── _install_supervisor — units use the new `db flush` token (X1) ─────────────

def test_install_supervisor_launchd_uses_db_flush_verb(tmp_path, monkeypatch):
    """macOS launchd plist invokes `db flush` (two argv tokens), never `db-flush`."""
    import platform
    from juggle_cmd_db_flush import _install_supervisor

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setenv("HOME", str(tmp_path))

    _install_supervisor(tmp_path / "live.db", tmp_path / "durable.db", 30.0)

    agents = tmp_path / "Library" / "LaunchAgents"
    plists = list(agents.glob("*.plist"))
    assert len(plists) == 1, f"expected one plist, got {plists}"
    content = plists[0].read_text()
    assert "db-flush" not in content, "plist must not reference legacy db-flush token"
    assert "<string>db</string>\n    <string>flush</string>" in content


def test_install_supervisor_systemd_uses_db_flush_verb(tmp_path, monkeypatch):
    """Linux systemd unit ExecStart calls `db flush`, never `db-flush`."""
    import platform
    from juggle_cmd_db_flush import _install_supervisor

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setenv("HOME", str(tmp_path))

    _install_supervisor(tmp_path / "live.db", tmp_path / "durable.db", 30.0)

    unit = tmp_path / ".config" / "systemd" / "user" / "juggle-db-flush.service"
    content = unit.read_text()
    assert "db-flush" not in content, "unit must not reference legacy db-flush token"
    assert "db flush --live" in content


def test_install_supervisor_removes_legacy_launchd_unit(tmp_path, monkeypatch):
    """Re-install removes a stale legacy-labelled plist so the dir is db-flush-free."""
    import platform
    from juggle_cmd_db_flush import _install_supervisor

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setenv("HOME", str(tmp_path))

    agents = tmp_path / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    legacy = agents / "com.juggle.db-flush.plist"
    legacy.write_text("<plist><string>db-flush</string></plist>")

    _install_supervisor(tmp_path / "live.db", tmp_path / "durable.db", 30.0)

    assert not legacy.exists(), "legacy db-flush plist should be removed on reinstall"
    leftover = [
        p for p in agents.rglob("*") if p.is_file() and "db-flush" in p.read_text()
    ]
    assert not leftover, f"no installed unit may reference db-flush: {leftover}"
