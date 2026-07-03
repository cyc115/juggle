"""juggle_doctor_snapshot — pre-migration DB snapshot (P3, 2026-07-03).

Spec DA: "Watchdog-run doctor = unattended prod-DB migration" — a bad
migration would execute with no human eyes. ``snapshot_db`` takes a
consistent sqlite backup BEFORE any migration touches the live file, so an
auto-migration becomes auto-revertible; ``prune_old_snapshots`` caps
retention. Fail-closed: the caller (``cmd_doctor``) must abort the migration
entirely when the snapshot itself fails — never proceed unsnapshotted.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SNAPSHOT_SUFFIX = ".pre-migrate."
RETENTION_DAYS = 7


def snapshot_db(db_path: str) -> Path:
    """Consistent sqlite backup of ``db_path`` to ``<db_path>.pre-migrate.<ts>``.

    Uses sqlite3's own backup API (not a raw file copy) so a concurrently
    open WAL-mode connection can't yield a torn snapshot. Raises on any
    failure or an empty/missing result — callers must fail-closed.
    """
    src = Path(db_path)
    if not src.exists():
        raise FileNotFoundError(f"cannot snapshot — {src} does not exist")
    dest = src.with_name(f"{src.name}{SNAPSHOT_SUFFIX}{int(time.time())}")
    src_conn = sqlite3.connect(str(src))
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        src_conn.close()
    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError(f"snapshot verification failed: {dest} missing or empty")
    return dest


def prune_old_snapshots(db_path: str, retention_days: int = RETENTION_DAYS) -> list[str]:
    """Delete ``<db>.pre-migrate.<ts>`` snapshots older than ``retention_days``.

    Age is read from the timestamp embedded in the filename (snapshot-creation
    time), not filesystem mtime. Best-effort — a delete failure is skipped, not
    raised (pruning must never block a migration that already succeeded).
    """
    src = Path(db_path)
    cutoff = time.time() - retention_days * 86400
    removed = []
    for candidate in src.parent.glob(f"{src.name}{SNAPSHOT_SUFFIX}*"):
        suffix = candidate.name.rsplit(SNAPSHOT_SUFFIX, 1)[-1]
        try:
            ts = int(suffix)
        except ValueError:
            continue
        if ts < cutoff:
            try:
                candidate.unlink()
                removed.append(str(candidate))
            except OSError:
                pass
    return removed
