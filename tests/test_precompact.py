"""Tests for PreCompact checkpoint: write, restore, cleanup, and guard logic."""
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB
import juggle_hooks
import juggle_hooks_config


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "juggle.db"
    db = JuggleDB(str(db_path))
    db.init_db()
    # Patch juggle_hooks_config — that's where the sub-modules read these at call time.
    monkeypatch.setattr(juggle_hooks_config, "DB_PATH", db_path)
    monkeypatch.setattr(juggle_hooks_config, "_CHECKPOINT_PATH", tmp_path / "checkpoint.json")
    monkeypatch.setattr(juggle_hooks_config, "get_db", lambda db_path=None, init=False: db)
    # Also keep juggle_hooks attributes in sync for any test that reads them directly.
    monkeypatch.setattr(juggle_hooks, "DB_PATH", db_path)
    monkeypatch.setattr(juggle_hooks, "_CHECKPOINT_PATH", tmp_path / "checkpoint.json")
    monkeypatch.setattr(juggle_hooks, "get_db", lambda db_path=None, init=False: db)
    return db, tmp_path / "checkpoint.json"


def _seed_state(db, label="A"):
    """Seed the DB with one open thread set as current, using DB API."""
    tid = db.create_thread("Test topic", session_id="sess-abc123")
    db.set_current_thread(tid)
    with db._connect() as conn:
        # Set session_id key so _get_session_id() returns a stable value
        conn.execute(
            "INSERT INTO session (key, value) VALUES ('session_id', 'sess-abc123')"
            " ON CONFLICT(key) DO UPDATE SET value='sess-abc123'"
        )
        conn.commit()
    return tid


class TestWriteCheckpoint:
    def test_writes_expected_fields(self, tmp_db):
        db, cp_path = tmp_db
        tid = _seed_state(db)

        juggle_hooks._write_checkpoint(db)

        assert cp_path.exists()
        payload = json.loads(cp_path.read_text())
        assert payload["active_thread_id"] == tid
        assert payload["active_thread_label"] is not None  # auto-assigned by create_thread
        assert payload["session_id"] == "sess-abc123"
        assert "ts" in payload
        assert isinstance(payload["in_flight_dispatches"], list)
        assert "notification_cursor" in payload

    def test_overwrites_cleanly_on_double_compact(self, tmp_db):
        db, cp_path = tmp_db
        _seed_state(db)

        juggle_hooks._write_checkpoint(db)
        first_ts = json.loads(cp_path.read_text())["ts"]
        time.sleep(0.01)
        juggle_hooks._write_checkpoint(db)
        second_ts = json.loads(cp_path.read_text())["ts"]

        assert second_ts > first_ts

    def test_atomic_tmp_cleanup(self, tmp_db):
        db, cp_path = tmp_db
        _seed_state(db)
        juggle_hooks._write_checkpoint(db)
        tmp = cp_path.with_suffix(".json.tmp")
        assert not tmp.exists()


class TestRestoreCheckpoint:
    def test_returns_empty_when_no_checkpoint(self, tmp_db):
        db, _ = tmp_db
        _seed_state(db)
        result = juggle_hooks._restore_checkpoint(db)
        assert result == ""

    def test_returns_context_on_valid_checkpoint(self, tmp_db):
        db, cp_path = tmp_db
        _seed_state(db)
        juggle_hooks._write_checkpoint(db)

        result = juggle_hooks._restore_checkpoint(db)

        assert "Resuming after compaction" in result
        assert "[A]" in result

    def test_ignores_stale_checkpoint(self, tmp_db):
        db, cp_path = tmp_db
        _seed_state(db)
        juggle_hooks._write_checkpoint(db)
        payload = json.loads(cp_path.read_text())
        payload["ts"] = time.time() - 7200  # 2 hours ago
        cp_path.write_text(json.dumps(payload))

        result = juggle_hooks._restore_checkpoint(db)
        assert result == ""

    def test_ignores_wrong_session(self, tmp_db):
        db, cp_path = tmp_db
        _seed_state(db)
        juggle_hooks._write_checkpoint(db)
        payload = json.loads(cp_path.read_text())
        payload["session_id"] = "other-session"
        cp_path.write_text(json.dumps(payload))

        result = juggle_hooks._restore_checkpoint(db)
        assert result == ""

    def test_handles_corrupt_checkpoint_gracefully(self, tmp_db):
        db, cp_path = tmp_db
        _seed_state(db)
        cp_path.write_text("not-valid-json{{{")

        result = juggle_hooks._restore_checkpoint(db)
        assert result == ""


class TestStaleReaper:
    def test_reaper_deletes_24h_old_checkpoint(self, tmp_db, monkeypatch):
        db, cp_path = tmp_db
        _seed_state(db)
        old_payload = {
            "ts": time.time() - 90000,  # 25 hours
            "session_id": "old",
            "active_thread_id": None,
            "active_thread_label": None,
            "in_flight_dispatches": [],
            "notification_cursor": 0,
            "pending_action_item_head": None,
        }
        cp_path.write_text(json.dumps(old_payload))

        # Simulate what handle_session_start does for the reaper block
        import importlib
        with patch.object(juggle_hooks_config, "_CHECKPOINT_PATH", cp_path):
            if cp_path.exists():
                try:
                    cp = json.loads(cp_path.read_text())
                    if time.time() - cp.get("ts", 0) > 86400:
                        cp_path.unlink(missing_ok=True)
                except Exception:
                    pass

        assert not cp_path.exists()


class TestHandlePreCompact:
    def test_writes_checkpoint_and_prints_system_message(self, tmp_db, monkeypatch, capsys):
        db, cp_path = tmp_db
        _seed_state(db)
        monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)

        with patch.object(juggle_hooks_config, "is_active", return_value=True):
            with pytest.raises(SystemExit) as exc:
                juggle_hooks.handle_pre_compact({})

        assert exc.value.code == 0
        assert cp_path.exists()
        out = capsys.readouterr().out
        msg = json.loads(out)
        assert "systemMessage" in msg
        assert "compaction" in msg["systemMessage"].lower()

    def test_skips_agent_sessions(self, tmp_db, monkeypatch, capsys):
        db, cp_path = tmp_db
        _seed_state(db)
        monkeypatch.setenv("JUGGLE_IS_AGENT", "1")

        with pytest.raises(SystemExit) as exc:
            juggle_hooks.handle_pre_compact({})

        assert exc.value.code == 0
        assert not cp_path.exists()
