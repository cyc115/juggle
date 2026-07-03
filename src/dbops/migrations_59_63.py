"""dbops.migrations_59_63 — thin dispatcher for migrations 59-63.

Wired directly from run_migrations (Migration 46/58 precedent) — additive
nodes columns + notifications_v2.kind/handled_by (irl-backbone R1) +
nodes.fail_envelope (irl-envelope T2). Collapsed to one call site here to
keep dbops.migrations under the LOC gate.
"""
from __future__ import annotations

import sqlite3


def apply_migrations_59_63(conn: sqlite3.Connection) -> None:
    from dbops.migration_59_node_priority import migrate_59_node_priority
    from dbops.migration_60_submitted_rev import migrate_60_submitted_rev
    from dbops.migration_61_pending_merged_sha import migrate_61_pending_merged_sha
    from dbops.migration_62_event_kind_routing import migrate_62_event_kind_routing
    from dbops.migration_63_fail_envelope import migrate_63_fail_envelope

    migrate_59_node_priority(conn)
    migrate_60_submitted_rev(conn)
    migrate_61_pending_merged_sha(conn)
    migrate_62_event_kind_routing(conn)
    migrate_63_fail_envelope(conn)
