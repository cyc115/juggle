"""dbops.conv_node_mirror — P8 dual-write of a conversation thread onto its
kind='conversation' node, in the caller's connection/transaction.

Extracted from dbops.threads (loc-gate budget) during the P8 Wave-3 collapse.
Best-effort: a missing nodes table (pre-Migration-44) never breaks the thread
write. Once the legacy `threads` table is dropped these become the sole writers
of the conversation node (the threads UPDATE/INSERT alongside them goes away).
"""
from __future__ import annotations

import sqlite3

from dbops.node_translation import STATUS_TO_STATE

# threads.* -> nodes.* column renames for the mirror.
_CONV_COL_RENAME = {"topic": "title", "last_active": "last_active_at"}


def mirror_conv_insert(
    conn, thread_id: str, *, topic: str, session_id: str, user_label, now: str
) -> None:
    """Mirror a freshly-created thread as a kind='conversation' node.

    P8 H4 (2026-06-27): CREATE_NODES now carries every column written here, so a
    missing COLUMN can no longer occur on a migrated DB — and if one ever does it
    FAILS LOUD (real schema gap), never swallowed. A missing nodes TABLE
    (pre-Migration-44) stays tolerated, pinned by
    test_p8_conv_read_collapse.test_conv_mirror_fails_loud_on_missing_column."""
    try:
        conn.execute(
            "INSERT OR IGNORE INTO nodes "
            "(id, kind, title, objective, state, project_id, session_id, user_label, "
            " show_in_list, summarized_msg_count, open_questions, key_decisions, "
            " last_active_at, created_at, updated_at) "
            # project_id mirrors the threads column DEFAULT 'INBOX' (create_thread
            # never sets it explicitly) so project-keyed node reads (resweep_inbox)
            # resolve the conversation — without this the node's project_id was NULL.
            "VALUES (?, 'conversation', ?, '', 'open', 'INBOX', ?, ?, 1, 0, '[]', '[]', ?, ?, ?)",
            (thread_id, topic, session_id, user_label, now, now, now),
        )
    except sqlite3.OperationalError as e:
        # A missing nodes TABLE (pre-Migration-44) is tolerated; a missing COLUMN
        # is a real schema gap and must FAIL LOUD (H4 — the DDL is now complete).
        if "no such table" in str(e).lower():
            return
        raise


def mirror_conv_update(conn, thread_id: str, **cols) -> None:
    """Mirror a thread column change onto its conversation node.

    ``status`` is value-mapped to ``state`` (unknown/legacy statuses leave the
    node state untouched — fail-soft, not fail-loud); ``topic``/``last_active``
    are renamed; columns the nodes table lacks (reviewed, assigned_confidence,
    …) are dropped. No-op on an empty change set."""
    try:
        ncols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    except sqlite3.OperationalError:
        return
    if not ncols:
        return
    sets, params = [], []
    if "status" in cols:
        state = STATUS_TO_STATE.get(cols.pop("status"))
        if state is not None:
            sets.append("state=?")
            params.append(state)
    for key, val in cols.items():
        ncol = _CONV_COL_RENAME.get(key, key)
        if ncol not in ncols:
            continue
        sets.append(f"{ncol}=?")
        params.append(val)
    if not sets:
        return
    params.append(thread_id)
    conn.execute(
        f"UPDATE nodes SET {', '.join(sets)} WHERE id=? AND kind='conversation'",
        params,
    )
