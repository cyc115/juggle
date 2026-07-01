"""Regression: watchdog daemon sqlite connection leak (defect B).

Symptom: the live watchdog daemon accrued ~60 open FDs to juggle.db (normal is
1-3). RCA: every DB op runs `with self._connect() as conn:`, but a stock sqlite3
connection's context-manager only commits/rolls back on `__exit__` — it does NOT
close the connection. So each op leaked one open connection/FD; over many ticks
they piled up until GC happened to reclaim them.

Fix: `_connect()` connections close deterministically at `with`-block exit, so the
open-connection count stays bounded no matter how many ticks/ops run.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from juggle_db import JuggleDB  # noqa: E402


def _is_open(conn: sqlite3.Connection) -> bool:
    """True iff the connection is still usable (not yet closed)."""
    try:
        conn.execute("SELECT 1")
        return True
    except sqlite3.ProgrammingError:
        return False


def test_with_block_closes_connection(tmp_path):
    """A `with db._connect() as conn:` block must close conn on exit."""
    db = JuggleDB(str(tmp_path / "leak.db"))
    db.init_db()

    with db._connect() as conn:
        conn.execute("SELECT 1")
        assert _is_open(conn)

    # After the block the connection (and its FD) must be released.
    assert not _is_open(conn), "connection left open after `with` block — FD leak"


def test_many_ticks_do_not_leak_connections(tmp_path, monkeypatch):
    """Running many DB ops (as ticks do) must not accumulate open connections.

    Strong references are held to every connection created so GC cannot mask the
    leak — this makes the assertion deterministic: pre-fix every op leaves an
    open connection, post-fix each is closed at block exit.
    """
    db = JuggleDB(str(tmp_path / "ticks.db"))
    db.init_db()

    created: list[sqlite3.Connection] = []
    current = JuggleDB._connect  # already-patched (conftest isolation guard)

    def tracking_connect(self):
        conn = current(self)
        created.append(conn)
        return conn

    monkeypatch.setattr(JuggleDB, "_connect", tracking_connect)

    for _ in range(50):
        db.get_all_agents()  # representative per-op `with self._connect()` call

    open_count = sum(1 for c in created if _is_open(c))
    assert open_count <= 3, (
        f"leaked {open_count} open connections across {len(created)} ops "
        "(expected <=3) — watchdog FD leak regressed"
    )
