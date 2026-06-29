"""dbops.dispatch_edge — the task→dispatch-thread binding as a typed node_edge (P8 M1/Q2).

The relation that used to live on a column of the node row is now an explicit
``kind='dispatch'`` row in ``node_edges``:
``(node_id=<task/topic node>, depends_on_id=<conversation node>, kind='dispatch')``.

Dependency edges are ``kind='dep'``; the two relations never mix in a query — dep
traversal filters ``kind='dep'`` and the agent-thread binding filters
``kind='dispatch'``. These helpers are the single seam for reading/writing the
binding; callers pass a live connection (they own the transaction — no commit here).
"""
from __future__ import annotations

import sqlite3

DEP = "dep"
DISPATCH = "dispatch"


def bind_dispatch_thread(conn: sqlite3.Connection, node_id: str, thread_id) -> None:
    """Bind (or rebind) ``node_id``'s dispatch thread. ``thread_id=None`` clears it.

    A node binds exactly one thread: the prior dispatch edge is replaced.
    """
    conn.execute(
        "DELETE FROM node_edges WHERE node_id=? AND kind=?", (node_id, DISPATCH)
    )
    if thread_id is not None:
        conn.execute(
            "INSERT OR IGNORE INTO node_edges (node_id, depends_on_id, kind) "
            "VALUES (?,?,?)",
            (node_id, thread_id, DISPATCH),
        )


def clear_dispatch_thread(conn: sqlite3.Connection, node_id: str) -> None:
    """Drop ``node_id``'s dispatch binding (the 'reload' resurrection clears it)."""
    conn.execute(
        "DELETE FROM node_edges WHERE node_id=? AND kind=?", (node_id, DISPATCH)
    )


def dispatch_thread_of(conn: sqlite3.Connection, node_id: str) -> "str | None":
    """The conversation id ``node_id`` is dispatched to, or None when unbound."""
    row = conn.execute(
        "SELECT depends_on_id FROM node_edges WHERE node_id=? AND kind=? LIMIT 1",
        (node_id, DISPATCH),
    ).fetchone()
    return row[0] if row else None
