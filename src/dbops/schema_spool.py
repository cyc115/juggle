"""dbops.schema_spool — DDL for the spool-drain journal (single-writer broker)."""
from __future__ import annotations

CREATE_SPOOL_JOURNAL = """
CREATE TABLE IF NOT EXISTS spool_journal (
    uuid        TEXT PRIMARY KEY,
    event_type  TEXT NOT NULL,
    applied_at  TEXT NOT NULL,
    outcome     TEXT NOT NULL DEFAULT 'applied'
)
"""
