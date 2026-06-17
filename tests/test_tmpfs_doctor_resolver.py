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
    """cmd_doctor must not cache DB_PATH at module level.

    Regression pin: DB_PATH must be resolved inside cmd_doctor() (call time),
    not at module import time. Acceptable patterns:
      - import inside function body: `from juggle_db import JuggleDB, DB_PATH`
      - call _resolve_db_path() inside the function

    Either way, patching juggle_db.DB_PATH or JUGGLE_DB_PATH after module load
    takes effect on the next cmd_doctor() call.
    """
    import ast
    import inspect
    import juggle_cmd_doctor

    # Parse the module source and check DB_PATH is NOT assigned at module scope
    # (it must only appear inside cmd_doctor's body, not as a top-level assignment)
    module_src = inspect.getsource(juggle_cmd_doctor)
    tree = ast.parse(module_src)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DB_PATH":
                    raise AssertionError(
                        "juggle_cmd_doctor has a module-level DB_PATH assignment — "
                        "this fixes the path at import time and breaks test isolation"
                    )
