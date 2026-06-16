"""Test-DB isolation guard (2026-06-16 prod-DB pollution incident).

Root cause: DB_PATH was hardcoded and not env-overridable, so tests doing bare
`JuggleDB()` (and a leaked worktree daemon) wrote junk threads/mirrors straight
into the PRODUCTION DB (~/.claude/juggle/juggle.db).

Fixes pinned here:
  1. The DB path honors `JUGGLE_DB_PATH` (default unchanged when unset).
  2. An autouse conftest fixture points every test at a temp DB AND hard-fails
     (raises) the instant any test tries to open the real production DB.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
import dbops.schema as schema  # noqa: E402


PROD_DB = (Path.home() / ".claude" / "juggle" / "juggle.db").resolve()


# ---------------------------------------------------------------------------
# 1. JUGGLE_DB_PATH override
# ---------------------------------------------------------------------------


def test_resolve_db_path_honors_env(monkeypatch, tmp_path):
    target = tmp_path / "custom.db"
    monkeypatch.setenv("JUGGLE_DB_PATH", str(target))
    assert schema._resolve_db_path() == target


def test_resolve_db_path_default_unchanged(monkeypatch):
    monkeypatch.delenv("JUGGLE_DB_PATH", raising=False)
    assert schema._resolve_db_path() == schema.DEFAULT_DATA_DIR / "juggle.db"


def test_bare_juggledb_honors_env(monkeypatch, tmp_path):
    target = tmp_path / "bare.db"
    monkeypatch.setenv("JUGGLE_DB_PATH", str(target))
    db = JuggleDB()  # no explicit db_path
    assert db.db_path == target


def test_explicit_db_path_wins_over_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JUGGLE_DB_PATH", str(tmp_path / "env.db"))
    explicit = tmp_path / "explicit.db"
    db = JuggleDB(str(explicit))
    assert db.db_path == explicit


# ---------------------------------------------------------------------------
# 2. Fail-closed prod guard (installed by the autouse conftest fixture)
# ---------------------------------------------------------------------------


def test_opening_prod_db_raises():
    """Any attempt to open the real prod DB during a test is a loud error."""
    db = JuggleDB(str(PROD_DB))
    with pytest.raises(RuntimeError, match="(?i)isolation|prod"):
        db._connect()


def test_temp_db_connect_is_allowed(tmp_path):
    """Temp DBs connect normally — the guard only fires on the prod path."""
    db = JuggleDB(str(tmp_path / "ok.db"))
    db.init_db()
    with db._connect() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
