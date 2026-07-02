"""Spool-directory test isolation helpers (2026-07-02 dead-letter incident).

INCIDENT 2026-07-02 ~03:25-04:20 local: 79 production spool events were
consumed and dead-lettered (~/.juggle/spool/dead/, prod spool_journal shows 0
applies) by agent worktree TEST RUNS of tests/test_spool_drain_idempotent.py
and related suites. Root cause: drain_spool / spool_dir() resolved the REAL
~/.juggle/spool while tests journaled into per-test tmp DBs — live
agent_complete / graph_mark_task events for AZ/BB/BF/BI/BH/BJ were silently
destroyed; the orchestrator had to hand-repair each. Example dead event:
dead_reason "SystemExit during replay of graph_mark_task: code=1".

``real_spool_dir()`` is computed PER CALL (not cached at import time) so a
test can simulate "the real spool dir" via a patched HOME without ever
touching the genuine one — mirrors the per-call design of
``_xdist_isolation.assert_not_prod_artifact`` (2026-06-21).
"""
from __future__ import annotations

from pathlib import Path


def real_spool_dir() -> Path:
    return (Path.home() / ".juggle" / "spool").resolve()


def _resolve(path) -> Path:
    try:
        return Path(path).resolve()
    except OSError:
        return Path(path)


def assert_not_prod_spool(path) -> None:
    """Raise RuntimeError if ``path`` is (or is under) the real spool dir.

    PURE — no IO. Callers invoke this BEFORE touching the filesystem so a
    test/drain run targeting the live spool fails loud instead of silently
    consuming/dead-lettering real agent events (see module docstring).
    """
    resolved = _resolve(path)
    prod = real_spool_dir()
    if resolved == prod or prod in resolved.parents:
        raise RuntimeError(
            "TEST ISOLATION VIOLATION (2026-07-02 spool dead-letter incident): "
            f"test/drain code targeted the real spool dir {resolved}. Use "
            "JUGGLE_SPOOL_DIR or monkeypatch spool_dir to a tmp_path — never "
            "the live ~/.juggle/spool (79 production events were destroyed "
            "this way)."
        )
