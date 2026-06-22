"""Shared seeders so tests build nodes/threads without copy-pasting INSERTs."""
from __future__ import annotations

_T = "2026-01-01 00:00"


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
