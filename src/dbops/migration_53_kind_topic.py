"""Migration 53 (P8 M2): introduce kind='topic' as the node discriminator — FAIL-LOUD.

Until now a graph topic and a bare task were both kind='task' nodes, separable
only by graph_topics membership (db_graph._TASK_ONLY / db_topics._TOPIC_ONLY).
The terminal-drop migration retires graph_topics, so that membership
discriminator must be replaced by a real kind BEFORE the drop. This migration
flips every graph_topics member's node from kind='task' to kind='topic'.

Bare root tasks (parent_id NULL, NOT in graph_topics), child tasks, and
conversation mirrors are untouched: a conversation-mirror graph_topics row maps
to a kind='conversation' node, which the ``kind='task'`` guard already excludes.

FAIL-LOUD: the SAME-RELEASE topic engine (db_topics.topic_transition and the
flipped _TOPIC_ONLY = kind='topic' read) hard-depends on this having applied. A
fail-soft swallow would leave topics as kind='task' nodes the flipped topic
predicates can no longer see (get_topic returns None -> topic_transition raises,
the topic ready-set is empty, autopilot stalls with no error). BEGIN IMMEDIATE
takes the write lock up front (cf. Migration 51); contention PROPAGATES and the
init_db caller aborts the upgrade. Idempotent (WHERE kind='task' -> second run
no-ops). Apply via juggle doctor (behind assert_migration_allowed); never run
directly against the shared production DB.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_53_kind_topic(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    # Need both the discriminator source (graph_topics) and the target (nodes).
    # A post-drop DB (graph_topics gone) or a pre-Migration-44 DB no-ops BEFORE
    # the lock so the upgrade stays cheap (not a spurious write-lock grab).
    if "nodes" not in tables or "graph_topics" not in tables:
        return
    prev_isolation = conn.isolation_level
    conn.isolation_level = None              # explicit transaction control
    conn.execute("BEGIN IMMEDIATE")          # write lock up front; raises on contention (fail-LOUD)
    try:
        conn.execute(
            "UPDATE nodes SET kind='topic' "
            "WHERE kind='task' AND id IN (SELECT id FROM graph_topics)"
        )
        conn.execute("COMMIT")
        _log.info("Migration 53: graph topics promoted to kind='topic'")
    except Exception:
        conn.execute("ROLLBACK")             # fail-LOUD: abort the upgrade, do NOT swallow
        raise
    finally:
        conn.isolation_level = prev_isolation
