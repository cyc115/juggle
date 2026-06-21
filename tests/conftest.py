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


# ── Worktree-leak guard (2026-06-20 incident) ─────────────────────────────────
# A test that called ``_create_worktree`` WITHOUT pointing ``worktree_root`` at
# its own ``tmp_path`` wrote the checkout to /private/tmp/juggle-* — outside
# pytest's managed temp dir — so it was never cleaned up. 100+ orphaned dangling
# worktrees accumulated (pruned manually 2026-06-20).
#
# This guard is HERMETIC: it wraps ``_create_worktree`` at its definition module
# and asserts, per call, that the requested ``worktree_root`` resolves under the
# test's ``tmp_path``. It only inspects calls the test under test actually makes,
# so it is immune to concurrent juggle processes (watchdog, other agents) that
# also touch the shared /tmp — a global-filesystem snapshot guard misattributes
# those to innocent tests and flakes. Tests that monkeypatch _create_worktree
# (to mock it) transparently override the wrapper for that symbol — they create
# no real worktree, so they are unaffected.


@pytest.fixture(autouse=True)
def _guard_worktree_under_tmp(tmp_path, monkeypatch):
    import juggle_cmd_agents_worktree as _wt

    _orig_create = _wt._create_worktree
    _tmp_resolved = tmp_path.resolve()

    def _guarded_create(repo_path, thread_label, worktree_root):
        try:
            root_resolved = Path(worktree_root).resolve()
        except OSError:
            root_resolved = Path(worktree_root)
        # The worktree root must live under THIS test's tmp_path. A bare /tmp
        # (or any path outside tmp_path) is the leak this guard exists to stop.
        if _tmp_resolved not in (root_resolved, *root_resolved.parents):
            raise AssertionError(
                "TEST HYGIENE VIOLATION (2026-06-20 worktree-leak incident): "
                f"_create_worktree called with worktree_root={worktree_root!r} "
                f"which is NOT under this test's tmp_path ({tmp_path}). Pass "
                "worktree_root=str(tmp_path) so the checkout cannot leak into /tmp."
            )
        return _orig_create(repo_path, thread_label, worktree_root)

    # Patch the definition module and every module that re-exports the symbol,
    # so both ``from juggle_cmd_agents_worktree import _create_worktree`` and the
    # ``juggle_cmd_agents_common._create_worktree`` production seam are guarded.
    monkeypatch.setattr(_wt, "_create_worktree", _guarded_create)
    for _modname in ("juggle_cmd_agents_common", "juggle_cmd_agents"):
        _mod = sys.modules.get(_modname)
        if _mod is not None and hasattr(_mod, "_create_worktree"):
            monkeypatch.setattr(_mod, "_create_worktree", _guarded_create)
    yield
