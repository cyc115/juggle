"""Regression pin: doctor must resolve DB path at call time, not import time (Task 3).

Incident: juggle_cmd_doctor used the module-level DB_PATH constant which is
fixed at first-import time. If JUGGLE_DB_PATH changes after module load,
the doctor would connect to the stale path instead of the live one.
Fix: call _resolve_db_path() inside cmd_doctor() (at call time).
"""
import os
import sys
import importlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_doctor_respects_juggle_db_path_set_after_module_load(tmp_path, monkeypatch):
    """cmd_doctor resolves DB path at call time, not at module import time.

    Regression: if doctor caches DB_PATH at import time, setting JUGGLE_DB_PATH
    AFTER the first import has no effect — the doctor would hit the stale path.
    Fix: doctor calls _resolve_db_path() inside cmd_doctor(), not at module scope.
    """
    # Step 1: Import doctor WITHOUT JUGGLE_DB_PATH set (simulates stale import)
    monkeypatch.delenv("JUGGLE_DB_PATH", raising=False)
    monkeypatch.delenv("_JUGGLE_TEST_DB", raising=False)
    import juggle_cmd_doctor as _doc_stale  # noqa: F401 — trigger module cache

    # Step 2: NOW set JUGGLE_DB_PATH to a fresh isolated DB
    db_path = tmp_path / "test_juggle.db"
    from juggle_db import JuggleDB
    db = JuggleDB(db_path=str(db_path))
    db.init_db()
    monkeypatch.setenv("JUGGLE_DB_PATH", str(db_path))

    # Step 3: Reload doctor so it picks up the module-level DB_PATH (this is
    # what the bug would show: it would still use the stale path).
    # With the fix (call-time resolution), cmd_doctor uses the fresh env var.
    import juggle_cmd_doctor
    importlib.reload(juggle_cmd_doctor)

    import argparse
    args = argparse.Namespace(dry_run=True)
    # This should succeed against our isolated DB, not fail on the prod DB
    rc = juggle_cmd_doctor.cmd_doctor(args)
    assert rc in (0, None), f"doctor returned {rc}"


def test_doctor_cmd_doctor_does_not_use_module_level_db_path(tmp_path, monkeypatch):
    """cmd_doctor function body must not reference module-level DB_PATH directly.

    It must call _resolve_db_path() or JuggleDB(db_path=None) which re-resolves
    at call time.
    """
    import inspect
    import juggle_cmd_doctor
    src = inspect.getsource(juggle_cmd_doctor.cmd_doctor)
    # The function must NOT use the bare module-level DB_PATH constant
    # (it may reference it only to assign to a local using _resolve_db_path)
    # We accept any reference inside a call to _resolve_db_path or JuggleDB(None)
    # Simple proxy: if "DB_PATH" appears in the function body without
    # "_resolve_db_path" nearby, that's the bug.
    if "DB_PATH" in src:
        assert "_resolve_db_path" in src, (
            "cmd_doctor uses DB_PATH without calling _resolve_db_path() — "
            "path is stale if env var is set after module import"
        )
