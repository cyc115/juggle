"""dbops.migrations_tail — migrations 58..N, wired after the P8 collapse.

Extracted from dbops.migrations (LOC gate) — same rationale as migrations_p8:
keeps the general migration registry from growing past its budget as the tail
picks up new steps. Each step is idempotent / presence-guarded.
"""
from __future__ import annotations

import sqlite3


def apply_tail_migrations(conn: sqlite3.Connection) -> None:
    """Run migrations 58.. in order (idempotent)."""
    import logging
    log = logging.getLogger(__name__)

    # Migration 58 (single-writer broker, T-spool): additive spool_journal table —
    # crash-safe idempotency ledger for the watchdog's spool drain (Task 8).
    from dbops.schema_spool import CREATE_SPOOL_JOURNAL
    try:
        conn.execute(CREATE_SPOOL_JOURNAL)
        conn.commit()
        log.info("Migration 58 (spool journal): spool_journal table installed")
    except sqlite3.OperationalError as e:
        log.warning("Migration 58 (spool journal) skipped: %s", e)

    # Migrations 59-63: additive nodes columns + notifications_v2.kind/handled_by
    # (irl-backbone R1) + nodes.fail_envelope (irl-envelope T2).
    from dbops.migrations_59_63 import apply_migrations_59_63
    apply_migrations_59_63(conn)

    # Migration 64 (P2, irl-prevention): migration_seq allocator table.
    from dbops.migration_64_migration_seq import migrate_64_migration_seq
    migrate_64_migration_seq(conn)

    # Migration 65 (P3, irl-prevention): nodes.rebase_nudge_sent column.
    from dbops.migration_65_rebase_nudge import migrate_65_rebase_nudge
    migrate_65_rebase_nudge(conn)

    # Migration 66 (plan/spec provision): nodes.plan_path/spec_path columns.
    from dbops.migration_66_topic_plan_spec import migrate_66_topic_plan_spec
    migrate_66_topic_plan_spec(conn)

    # Migration 67 (graph-node-primitives Phase 1): nodes.cancel_reason column.
    from dbops.migration_67_cancel_reason import migrate_67_cancel_reason
    migrate_67_cancel_reason(conn)

    # Migration 70 (graph-node learnings primitive): nodes.learnings column.
    from dbops.migration_70_node_learnings import migrate_70_node_learnings
    migrate_70_node_learnings(conn)

    # Migration 71 (fix-reintegrate-persist-backoff): durable re-integrate backoff
    # columns — nodes.reintegrate_attempts/last_attempt/pid.
    from dbops.migration_71_reintegrate_backoff import migrate_71_reintegrate_backoff
    migrate_71_reintegrate_backoff(conn)

    # Migration 72 (loop-entity V1 Phase 1): nodes.role/delivery + projects.kind.
    from dbops.migration_72_node_role_delivery import migrate_72_node_role_delivery
    migrate_72_node_role_delivery(conn)

    # Migration 73 (loop-entity V1 Phase 1): loops table + run-seq/breaker columns.
    from dbops.migration_73_loops import migrate_73_loops
    migrate_73_loops(conn)

    # Migration 74 (loop-entity V2 Release 1 / P2): additive nodes.model column
    # (requested model, TOPIC grain — distinct from nodes.last_dispatched_model).
    from dbops.migration_74_node_model import migrate_74_node_model
    migrate_74_node_model(conn)

    # Migration 75 (2026-07-09 incident D1): widen idx_nodes_live_label to
    # state != 'archived', matching the 5e0c8f8 held-slug invariant with a
    # DB-level backstop (was Python-check-only for the done/failed-exec subset).
    from dbops.migration_75_conv_label_archived_only import (
        migrate_75_conv_label_archived_only,
    )
    migrate_75_conv_label_archived_only(conn)
