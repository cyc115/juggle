"""Helpers for xdist-parallel test isolation (speedup-tier, 2026-06-21).

Kept out of conftest.py to respect the ≤300-line module gate. Provides the
per-worker id, the documented set of prod artifacts the guard protects, and the
pure decision the autouse `_guard_no_prod_artifacts` fixture makes per call.
"""
from __future__ import annotations

import os
from pathlib import Path

_PROD_DB = (Path.home() / ".claude" / "juggle" / "juggle.db").resolve()
_PROD_LOCK = (_PROD_DB.parent / f".{_PROD_DB.name}.watchdog.lock").resolve()
# Prod pidfile/config dir: the watchdog writes ~/.juggle/watchdog.pid; no test
# may write a pidfile (or anything) into the live prod config dir.
_PROD_DOT_JUGGLE = (Path.home() / ".juggle").resolve()


def worker_id() -> str:
    """xdist worker token ('gw0'...), or 'main' when running single-process."""
    return os.environ.get("PYTEST_XDIST_WORKER", "main")


def watchdog_session_name() -> str:
    """Per-xdist-worker real-tmux session name for the watchdog suite.

    The watchdog conftest's session was a FIXED 'juggle-watchdog-test', so two
    xdist workers created/killed the same session and stole each other's panes.
    Keying it to the worker id lets the watchdog suite run PARALLEL. 'main'
    suffix when single-process (speedup-tier, 2026-06-21).
    """
    return f"juggle-watchdog-test-{worker_id()}"


def prod_artifact_paths() -> list[Path]:
    """Shared prod filesystem artifacts a test must NEVER create/modify.

    The prod DB, its watchdog lock (lock_path_for derives '.juggle.db.watchdog.lock'
    next to the DB), and any ~/.juggle/*.pid pidfiles.
    """
    pidfiles = (
        sorted(_PROD_DOT_JUGGLE.glob("*.pid")) if _PROD_DOT_JUGGLE.is_dir() else []
    )
    return [_PROD_DB, _PROD_LOCK, *pidfiles]


def _resolve(path) -> Path:
    try:
        return Path(path).resolve()
    except OSError:
        return Path(path)


def assert_not_prod_artifact(path) -> None:
    """Raise AssertionError if ``path`` is a prod watchdog artifact.

    Prod artifacts: the prod DB, the prod watchdog lock, or anything under the
    live prod ``~/.juggle`` config/pidfile dir. PURE — no IO. The autouse guard
    in conftest calls this on the lock/pidfile path each writer seam is handed,
    so a test that targets a prod artifact fails loud BEFORE the write happens.
    This per-call check is immune to a concurrent live watchdog daemon (B1).
    """
    resolved = _resolve(path)
    is_prod = (
        resolved in (_PROD_DB, _PROD_LOCK)
        or resolved == _PROD_DOT_JUGGLE
        or _PROD_DOT_JUGGLE in resolved.parents
    )
    if is_prod:
        raise AssertionError(
            "TEST ISOLATION VIOLATION (speedup-tier 2026-06-21): test targeted "
            f"prod watchdog artifact {resolved}. Isolate to tmp_path — never the "
            "prod DB/lock/pidfile; under xdist this corrupts sibling workers."
        )
