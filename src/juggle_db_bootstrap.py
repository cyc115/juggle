"""juggle_db_bootstrap — copy durable DB → tmpfs live path on first connect.

Called by JuggleDB.__init__ when mode=tmpfs and live DB is absent.
Uses sqlite3 backup API for a consistent, online copy.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def bootstrap_tmpfs(live_path: Path, durable_path: Path) -> None:
    """Copy durable→live if live is absent; noop if live already exists.

    After copy, runs PRAGMA integrity_check. Raises RuntimeError if the
    check fails (indicating a corrupt source or bad copy).
    """
    live_path = Path(live_path)
    durable_path = Path(durable_path)

    if live_path.exists():
        return  # already bootstrapped

    live_path.parent.mkdir(parents=True, exist_ok=True)

    # Copy via sqlite3 backup API for a consistent snapshot
    src = sqlite3.connect(str(durable_path))
    dst = sqlite3.connect(str(live_path))
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()

    # Integrity check on the live copy
    conn = sqlite3.connect(str(live_path))
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()

    if rows != [("ok",)]:
        messages = "; ".join(r[0] for r in rows)
        raise RuntimeError(
            f"integrity_check failed on bootstrapped tmpfs DB: {messages}"
        )
