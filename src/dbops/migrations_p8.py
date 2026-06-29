"""dbops.migrations_p8 — the P8 unified-nodes migration block (50..N).

Extracted from dbops.migrations_recent (LOC gate) so the P8 collapse's migration
wiring lives in one cohesive place that can grow as the collapse adds steps,
without pushing the general migration registry past its budget.

All steps run via JuggleDB.init_db() → run_migrations() → apply_recent_migrations(),
which calls apply_p8_migrations() in the orchestrator (non-agent) context behind
assert_migration_allowed. Each step is idempotent / presence-guarded.
"""
from __future__ import annotations

import sqlite3


def apply_p8_migrations(conn: sqlite3.Connection) -> None:
    """Run the P8 unified-nodes migrations 50.. in order (idempotent)."""
    # Migration 50 (unified-topic-graph P8 prep): additive nodes parity columns
    # (user_label/assigned_by/last_active_at) + kind-scoped slug index, then
    # backfill them from threads. ADDITIVE; applied via juggle doctor. Blocks the
    # P8 read-collapse; also fixes the Migration-44 last_active backfill-staleness.
    from dbops.migration_nodes_parity import migrate_50_nodes_parity, backfill_nodes_parity
    migrate_50_nodes_parity(conn)
    backfill_nodes_parity(conn)  # also runs backfill_graph_parity (P8 Q2/Q3)
    from dbops.migration_51_state_vocab import migrate_51_state_vocab  # P8 C3+R2-4
    migrate_51_state_vocab(conn)  # unify task vocab pending->open (FAIL-LOUD, before renamed engine)
    from dbops.migration_52_dispatch_edge import migrate_52_dispatch_edge  # P8 M1/Q2
    migrate_52_dispatch_edge(conn)  # type node_edges.kind; legacy binding -> kind='dispatch' edge
    from dbops.migration_53_kind_topic import migrate_53_kind_topic  # P8 M2
    migrate_53_kind_topic(conn)  # graph topics -> kind='topic' (FAIL-LOUD; runs after 44 backfill)
