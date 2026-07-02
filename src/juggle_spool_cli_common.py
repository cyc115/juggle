"""juggle_spool_cli_common — spool-related CLI helpers (T-spool-02).

Factored out of juggle_cli_common.py to keep that module under the repo's
300-line module gate (scripts/loc_gate.py). Re-exported from
juggle_cli_common so existing `juggle_cli_common.should_spool` /
`juggle_cli_common.resolve_thread_id_for_spool` call sites and test
monkeypatches keep working unchanged.
"""
from __future__ import annotations


def should_spool() -> bool:
    """True when this CLI process is running inside a dispatched agent/worktree
    context and MUST spool a write event instead of opening the DB read-write.

    Reuses the single existing agent-context detector (JUGGLE_ORCHESTRATOR wins
    over JUGGLE_IS_AGENT/worktree-cwd) — do not re-implement the heuristic here.
    """
    from dbops.graph_guards import is_agent_context

    return is_agent_context()


def resolve_thread_id_for_spool(thread_id_input: str) -> str:
    """Best-effort read-only resolve of a user-label/hex-prefix/UUID to the full
    thread UUID, for spool writers (Tasks 3-4) to call BEFORE writing an event.

    Why: spooled events replay at drain time, possibly minutes later. Thread
    labels are recycled off a finite wheel (dbops/slug_alloc.py) — resolving at
    REPLAY time instead of WRITE time risks a freed-then-reassigned label
    misapplying the event to the wrong thread. Resolving here and writing the
    UUID into the event closes that window; the replayed cmd_* handler's own
    _resolve_thread call becomes a no-op passthrough on an already-full UUID.

    Never raises: on any failure (missing DB file, locked, malformed input)
    returns the input unchanged — the replayed handler's own _resolve_thread
    is the fallback path, so a resolve failure here is a no-op regression to
    today's single-resolve-at-replay-time behavior, not a new hazard.
    """
    import juggle_cli_common as _common

    s = (thread_id_input or "").strip()
    if len(s) == 36 and s.count("-") == 4:
        return s  # already a full UUID — nothing to resolve
    try:
        from juggle_db_connect import open_connection_readonly

        conn = open_connection_readonly(_common._db_path())
        try:
            if 1 <= len(s) <= 2 and s.isalpha():
                row = conn.execute(
                    "SELECT id FROM nodes WHERE kind='conversation' AND user_label=? LIMIT 1",
                    (s.upper(),),
                ).fetchone()
                return row["id"] if row else s
            row = conn.execute(
                "SELECT id FROM nodes WHERE kind='conversation' AND id LIKE ? LIMIT 1",
                (f"{s}%",),
            ).fetchone()
            return row["id"] if row else s
        finally:
            conn.close()
    except Exception:
        return s
