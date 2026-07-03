import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import pytest


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.execute("INSERT INTO t (v) VALUES ('hello')")
    conn.commit()
    conn.close()


# ── snapshot_db ──────────────────────────────────────────────────────────

def test_snapshot_db_creates_consistent_copy(tmp_path):
    from juggle_doctor_snapshot import snapshot_db
    db_path = tmp_path / "j.db"
    _make_db(db_path)

    snap = snapshot_db(str(db_path))

    assert snap.exists()
    assert snap.name.startswith("j.db.pre-migrate.")
    conn = sqlite3.connect(str(snap))
    rows = conn.execute("SELECT v FROM t").fetchall()
    conn.close()
    assert rows == [("hello",)]


def test_snapshot_db_raises_on_missing_source(tmp_path):
    from juggle_doctor_snapshot import snapshot_db
    with pytest.raises(Exception):
        snapshot_db(str(tmp_path / "does_not_exist.db"))


# ── prune_old_snapshots ──────────────────────────────────────────────────

def test_prune_old_snapshots_removes_only_expired(tmp_path):
    from juggle_doctor_snapshot import prune_old_snapshots
    db_path = tmp_path / "j.db"
    _make_db(db_path)

    now = int(time.time())
    old = tmp_path / f"j.db.pre-migrate.{now - 8 * 86400}"
    recent = tmp_path / f"j.db.pre-migrate.{now - 1 * 86400}"
    old.write_text("old")
    recent.write_text("recent")

    removed = prune_old_snapshots(str(db_path))

    assert str(old) in removed
    assert str(recent) not in removed
    assert not old.exists()
    assert recent.exists()


def test_prune_old_snapshots_ignores_malformed_names(tmp_path):
    from juggle_doctor_snapshot import prune_old_snapshots
    db_path = tmp_path / "j.db"
    _make_db(db_path)
    junk = tmp_path / "j.db.pre-migrate.not-a-timestamp"
    junk.write_text("junk")

    removed = prune_old_snapshots(str(db_path))

    assert junk.exists()
    assert str(junk) not in removed


# ── cmd_doctor integration ───────────────────────────────────────────────

def test_doctor_snapshots_before_migrating(tmp_path, monkeypatch, capsys):
    import juggle_cmd_doctor as doc
    import juggle_db

    db_path = tmp_path / "current.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY, session_id TEXT, topic TEXT,"
        " status TEXT DEFAULT 'active', user_label TEXT, assigned_by TEXT,"
        " project_id TEXT, created_at TEXT, last_active TEXT, last_active_at TEXT)"
    )
    conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(juggle_db, "DB_PATH", str(db_path))
    monkeypatch.setattr(doc, "CONFIG_PATH", tmp_path / "nope.json")

    class _Args:
        dry_run = False

    assert doc.cmd_doctor(_Args()) == 0
    snaps = list(tmp_path.glob("current.db.pre-migrate.*"))
    assert len(snaps) == 1, "doctor must snapshot exactly once before migrating"


def test_doctor_dry_run_does_not_snapshot(tmp_path, monkeypatch, capsys):
    import juggle_cmd_doctor as doc
    import juggle_db

    db_path = tmp_path / "current.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(juggle_db, "DB_PATH", str(db_path))
    monkeypatch.setattr(doc, "CONFIG_PATH", tmp_path / "nope.json")

    class _Args:
        dry_run = True

    assert doc.cmd_doctor(_Args()) == 0
    snaps = list(tmp_path.glob("current.db.pre-migrate.*"))
    assert snaps == []


def test_doctor_aborts_migration_when_snapshot_fails(tmp_path, monkeypatch, capsys):
    """Pin: snapshot failure aborts the migration fail-closed — init_db must
    NOT run, and cmd_doctor must return nonzero."""
    import juggle_cmd_doctor as doc
    import juggle_db

    db_path = tmp_path / "current.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(juggle_db, "DB_PATH", str(db_path))
    monkeypatch.setattr(doc, "CONFIG_PATH", tmp_path / "nope.json")

    def _boom(_db_path):
        raise RuntimeError("disk full")

    monkeypatch.setattr(doc, "snapshot_db", _boom)

    init_db_calls = []

    class _FakeDB:
        def __init__(self, path):
            self._path = path

        def init_db(self, *, require_migrate=False):
            init_db_calls.append(self._path)

        def list_projects(self, include_archived=False):
            return []

    monkeypatch.setattr(juggle_db, "JuggleDB", _FakeDB)

    class _Args:
        dry_run = False

    result = doc.cmd_doctor(_Args())

    assert result != 0
    assert init_db_calls == [], "migration must NOT run when the snapshot failed"
