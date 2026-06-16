"""Global test isolation (2026-06-16 prod-DB pollution incident).

Two fail-closed guards applied to EVERY test:

  1. `JUGGLE_DB_PATH` is pointed at a per-test temp DB, so any bare `JuggleDB()`
     (or product code that opens the default DB) lands in a throwaway file —
     never the production DB at ~/.claude/juggle/juggle.db.

  2. `JuggleDB._connect` is wrapped to RAISE the instant any test opens the real
     production DB. Prod access in a test is then an immediate, loud error
     rather than silent pollution.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

_PROD_DB = (Path.home() / ".claude" / "juggle" / "juggle.db").resolve()


@pytest.fixture(autouse=True)
def _isolate_db_from_prod(tmp_path, monkeypatch):
    # 1. Redirect the default DB path at this test's temp dir, and initialize it
    #    so any `get_db()` / bare `JuggleDB()` with no explicit path resolves to
    #    a WORKING isolated DB (never the production DB).
    temp_db = tmp_path / "juggle-test.db"
    monkeypatch.setenv("JUGGLE_DB_PATH", str(temp_db))

    # 2. Fail-closed guard: opening the real prod DB is an immediate error.
    from juggle_db import JuggleDB

    _orig_connect = JuggleDB._connect

    def _guarded_connect(self):
        try:
            resolved = Path(self.db_path).resolve()
        except OSError:
            resolved = Path(self.db_path)
        if resolved == _PROD_DB:
            raise RuntimeError(
                "TEST ISOLATION VIOLATION: a test tried to open the production "
                f"DB {self.db_path}. Use a temp DB (tmp_path / JUGGLE_DB_PATH)."
            )
        return _orig_connect(self)

    monkeypatch.setattr(JuggleDB, "_connect", _guarded_connect)

    # Pre-initialize the isolated DB so default get_db() handles are usable.
    JuggleDB(str(temp_db)).init_db()
    yield
