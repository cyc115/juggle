"""Shared seeders so tests build nodes/threads without copy-pasting INSERTs."""
from __future__ import annotations

_T = "2026-01-01 00:00"

# Legacy tables are DROPPED on init_db (Migration 55, P8 terminal). Migration/
# backfill tests that must exercise the one-shot legacy→nodes upgrade path
# re-create the legacy table(s) as a local fixture using the SAME CREATE strings
# the historical schema shipped (still kept in src for the upgrade migration).
_LEGACY_CREATE = {
    "threads": "dbops.schema.CREATE_THREADS",
    "graph_tasks": "dbops.schema_graph.CREATE_GRAPH_TASKS",
    "graph_topics": "dbops.schema_graph.CREATE_GRAPH_TOPICS",
    "graph_edges": "dbops.schema_graph.CREATE_GRAPH_EDGES",
}


# Columns added to the legacy tables by LATER ALTER migrations (not in the base
# CREATE strings). A real upgrade DB carries them, so the fixture must too for the
# backfills (Migration 16/37/42/50) to find their source columns.
_LEGACY_ALTERS = {
    "threads": [
        "ALTER TABLE threads ADD COLUMN user_label TEXT",
        "ALTER TABLE threads ADD COLUMN assigned_by TEXT DEFAULT 'auto'",
        "ALTER TABLE threads ADD COLUMN project_id TEXT",
        "ALTER TABLE threads ADD COLUMN last_active_at TEXT",
    ],
    "graph_tasks": ["ALTER TABLE graph_tasks ADD COLUMN topic_id TEXT"],
    "graph_topics": [
        "ALTER TABLE graph_topics ADD COLUMN is_mirror INTEGER NOT NULL DEFAULT 0",
    ],
}


def make_legacy_tables(conn, *which):
    """(Re)create dropped legacy tables for a migration/backfill fixture.

    ``which`` names the tables to create (default: all four). Uses the shipped
    CREATE strings plus the migration-era ALTER columns so the fixture schema
    matches what the one-shot upgrade migration reads.
    """
    import importlib

    for name in (which or tuple(_LEGACY_CREATE)):
        mod_name, const = _LEGACY_CREATE[name].rsplit(".", 1)
        ddl = getattr(importlib.import_module(mod_name), const)
        conn.execute(ddl)
        for alter in _LEGACY_ALTERS.get(name, ()):
            conn.execute(alter)
    conn.commit()


def seed_thread(conn, id="t1", topic="x", status="active", **extra):
    cols = {"id": id, "session_id": "", "topic": topic, "status": status,
            "created_at": _T, "last_active": _T, **extra}
    keys = ",".join(cols)
    conn.execute(f"INSERT INTO threads ({keys}) VALUES ({','.join('?' * len(cols))})",
                 tuple(cols.values()))
    return id


def seed_node(conn, id="n1", kind="conversation", title="x", state="open",
              parent_id=None, **extra):
    cols = {"id": id, "kind": kind, "title": title, "state": state,
            "parent_id": parent_id, "created_at": _T, "updated_at": _T, **extra}
    keys = ",".join(cols)
    conn.execute(f"INSERT INTO nodes ({keys}) VALUES ({','.join('?' * len(cols))})",
                 tuple(cols.values()))
    return id
