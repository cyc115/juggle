"""dbops.migrations_selfheal_p2 — selfheal-triage-v2 P2 migration chain (47-49).

Extracted from dbops.migrations_recent (≤300-line architecture gate). Bundles the
three additive, idempotent P2 migrations so apply_recent_migrations wires them in
with a single call. Migration 46 is reserved for thread DI (topic_summary_cache);
migrations are idempotent guarded functions (no version ledger), so a gap is
harmless.
"""
from __future__ import annotations

import sqlite3

from dbops.migration_selfheal_audit import migrate_selfheal_audit
from dbops.migration_selfheal_group_key import migrate_group_key
from dbops.migration_selfheal_lease import migrate_selfheal_lease


def apply_selfheal_p2_migrations(conn: sqlite3.Connection) -> None:
    """Apply migrations 47 (group_key) / 48 (selfheal_audit) / 49 (benign_until)."""
    migrate_group_key(conn)       # 47: error_events.group_key + backfill
    migrate_selfheal_audit(conn)  # 48: durable selfheal_audit table
    migrate_selfheal_lease(conn)  # 49: error_events.benign_until lease
