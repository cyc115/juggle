"""dbops.orphan_autoclear — auto-ack stale completed-but-UNMERGED escalations.

Extracted from ``dbops.orphan_guard`` (2026-07-04, LOC gate): once a topic's
work lands on main — via the out-of-band reconcile path
(``dbops.orphan_reconcile``) OR the integrate happy path
(``juggle_integrate_mergedsha._record_merged_sha``) — the HIGH action item the
orphan guard filed for it must be auto-acked. Items are matched EXACTLY by the
``action_item_id`` recorded on each ``_ORPHAN_EVENT`` marker at filing time
(``orphan_guard.flag_unmerged_completed_topics``) — never by grepping the
message. Emits one INFO notification (WATCHDOG_RECOVERY, DB-row-only) per clear.

Incident (2026-07-03/04 integrate-wedge #2 follow-up): the watchdog filed the
UNMERGED escalation but nothing closed it when the merge subsequently landed, so
item 5308 stayed open for the operator to hand-ack.

Helpers from ``orphan_guard`` (``_ORPHAN_EVENT``, ``_dispatch_thread``) are
imported lazily inside the function bodies to break the import cycle (orphan_guard
re-exports ``clear_stale_unmerged_escalations`` from here).
"""

from __future__ import annotations


def clear_stale_unmerged_escalations(db, node_id: str) -> list[int]:
    """Auto-ack every still-open completed-but-UNMERGED action item filed for
    ``node_id`` once its work has merged. Returns the dismissed item ids.

    Idempotent: only open items are dismissed, so a second call is a no-op and
    never emits a duplicate INFO notification.
    """
    from dbops.orphan_guard import _ORPHAN_EVENT

    with db._connect() as conn:
        marker_rows = conn.execute(
            "SELECT snapshot_path FROM watchdog_events "
            "WHERE thread_id=? AND event_type=? AND snapshot_path IS NOT NULL",
            (node_id, _ORPHAN_EVENT),
        ).fetchall()
    item_ids: list[int] = []
    for row in marker_rows:
        try:
            item_ids.append(int(row["snapshot_path"]))
        except (TypeError, ValueError):
            continue

    dismissed: list[int] = []
    for item_id in item_ids:
        with db._connect() as conn:
            open_row = conn.execute(
                "SELECT id FROM action_items WHERE id=? AND dismissed_at IS NULL",
                (item_id,),
            ).fetchone()
        if not open_row:
            continue
        db.dismiss_action_item(item_id)
        dismissed.append(item_id)

    if dismissed:
        label = node_id
        try:
            with db._connect() as conn:
                trow = conn.execute(
                    "SELECT title FROM nodes WHERE id=?", (node_id,)
                ).fetchone()
            if trow and (trow["title"] or "").strip():
                label = trow["title"].strip()
        except Exception:
            pass
        _emit_autoclear_info(db, node_id, label)
    return dismissed


def _emit_autoclear_info(db, node_id: str, label: str) -> None:
    """Best-effort INFO notification that a stale escalation was auto-cleared.

    Routed as WATCHDOG_RECOVERY (DB-row-only, FYI) — never pushed. A missing
    session row (headless reconcile) must not break the auto-ack, so this is
    fully guarded."""
    try:
        from dbops.event_kinds import WATCHDOG_RECOVERY
        from dbops.orphan_guard import _dispatch_thread

        with db._connect() as conn:
            srow = conn.execute(
                "SELECT value FROM session WHERE key='session_id'"
            ).fetchone()
        session_id = srow["value"] if srow else ""
        thread_id = _dispatch_thread(db, node_id)
        db.emit_event(
            thread_id=thread_id,
            message=f"auto-cleared stale unmerged escalation for {label}",
            session_id=session_id,
            kind=WATCHDOG_RECOVERY,
        )
    except Exception:
        pass
