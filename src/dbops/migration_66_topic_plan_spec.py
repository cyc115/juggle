"""Migration 66 (T-fix-dispatch-plan-spec-provision) — additive
``nodes.plan_path``/``nodes.spec_path`` columns.

GAP (2026-07-03, user-flagged): coders were dispatched without the plan/spec
files their tasks implement. ``juggle_graph_load``'s spec format discarded the
file-level preamble ("Implements PLAN <path>") at load time, and the topic
dispatch-prompt builder rebuilt prompts from stored per-task bodies only — so
plan/spec references survived only if repeated in every task body. These two
columns make plan/spec first-class, topic-node fields so the dispatch-prompt
builder (juggle_graph_hydration.build_source_of_truth_section) can surface them.

ADDITIVE only, idempotent, presence-guarded, fail-soft (matches Migration 61).
Never rebuilds the table.
"""
from __future__ import annotations

import logging
import sqlite3

_log = logging.getLogger(__name__)


def migrate_66_topic_plan_spec(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "nodes" not in tables:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    try:
        if "plan_path" not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN plan_path TEXT")
        if "spec_path" not in cols:
            conn.execute("ALTER TABLE nodes ADD COLUMN spec_path TEXT")
        conn.commit()
        _log.info("Migration 66: nodes.plan_path/spec_path columns ensured")
    except sqlite3.OperationalError as e:  # fail-soft (additive convention)
        _log.warning("Migration 66 (topic_plan_spec) skipped: %s", e)
