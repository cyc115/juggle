"""dbops.schema_selfheal — self-heal DDL + status vocabulary.

Extracted from dbops.schema (≤300-line architecture gate) when selfheal-triage-v2
P2 added the group_key/benign_until columns and the selfheal_audit table. Re-
exported from dbops.schema so existing ``from dbops.schema import ...`` imports
keep working (mirrors schema_graph / schema_runs).
"""
from __future__ import annotations

CREATE_ERROR_EVENTS = """
CREATE TABLE IF NOT EXISTS error_events (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  signature_hash   TEXT    NOT NULL,
  error_class      TEXT    NOT NULL CHECK(error_class IN ('A', 'B')),
  exc_type         TEXT,
  traceback        TEXT,
  entrypoint       TEXT,
  surface          TEXT,
  command_args     TEXT,
  juggle_ref       TEXT,
  count            INTEGER NOT NULL DEFAULT 1,
  first_seen       TEXT    NOT NULL,
  last_seen        TEXT    NOT NULL,
  status           TEXT    NOT NULL DEFAULT 'open',
  action_item_id   INTEGER,
  group_key        TEXT,
  benign_until     TEXT
);
"""

# Self-heal status vocabulary — the single source of truth now that the DB-level
# CHECK on error_events.status is dropped (selfheal-triage-v2 P1, 2026-06-21).
# Validation is enforced in app code (set_error_event_status). Deliberately the
# LAST status migration ever needed: new statuses no longer require a rebuild.
VALID_ERROR_STATUSES: frozenset[str] = frozenset(
    {"open", "diagnosing", "awaiting_approval", "non_issue_proposed", "non_issue", "resolved"}
)

# Self-heal hide-on-arrival durable audit log (selfheal-triage-v2 P2, 2026-06-21).
# Promotes the previously log-line-only sweep/resurface/silent-hide records to a
# durable, queryable table — the HARD precondition for any audited silent
# auto-hide (Task 5). action ∈ {allowlist_hide, resurface, silent_autohide,
# lease_set, new_variant}.
CREATE_SELFHEAL_AUDIT = """
CREATE TABLE IF NOT EXISTS selfheal_audit (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ts              TEXT    NOT NULL,
  event_id        INTEGER,
  signature_hash  TEXT,
  group_key       TEXT,
  action          TEXT    NOT NULL,
  reason          TEXT,
  detail          TEXT
);
"""
