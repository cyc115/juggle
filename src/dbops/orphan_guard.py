"""dbops.orphan_guard — detect & surface completed-but-unmerged topics (G5).

P8 (Task 4.2): orphan detection reads exclusively from the unified nodes table —
topic nodes (kind='topic', P8 M2), their child states, and the
bound dispatch thread (the typed kind='dispatch' node_edge, P8 M1/Q2).
reconcile_out_of_band_merges stamps nodes.merged_sha (the lockstep
set_topic_merged_sha keeps graph_topics in sync where it is still dual-written).

Incident (2026-06-17): a false-negative in ``JuggleTmuxManager.send_task`` made
the watchdog treat a successful dispatch as failed, so the topic was never
tracked for integrate. When the coder's ``complete-agent`` closed the topic, its
work sat committed-in-worktree but UNMERGED, and ``juggle integrate`` reported
"Missing worktree fields — nothing to integrate". G1 (graph_guards.topic_is_merged)
already keeps such a topic out of ``verified``; this guard *detects* the stranded
topics and files a HIGH action item so a completed topic is NEVER silently closed
without merge — it always surfaces a blocker.

Pure detection + flagging. Owns no state transition (db_topics stays the writer)
and no integrate logic (juggle_cmd_integrate).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dbops.graph_guards import (
    branch_merged_to_main,
    resolve_branch_sha,
    sha_is_ancestor,
)

# watchdog_events.event_type used to dedup repeated flags for the same topic.
_ORPHAN_EVENT = "topic_unmerged_orphan"
# Sentinel agent_id (watchdog_events.agent_id is NOT NULL) — this guard is not
# bound to a single agent.
_GUARD_AGENT_ID = "orphan-guard"

# Grace period (minutes) after the last child task turns verified before a
# topic becomes eligible for orphan detection — consistent with the
# watchdog's stall_threshold_minutes convention (juggle_watchdog_stall).
# Incident (2026-07-02): mark-task verified happens BEFORE the owning agent's
# own `agent complete` -> `integrate` finalize call in the normal happy path,
# so detecting the instant all children verify races that finalize and fires
# a false HIGH action item almost every time a topic completes normally
# (confirmed on T-spool-02, T-bump-version-1-94).
_DEFAULT_ORPHAN_GRACE_MINUTES = 5.0


def _dispatch_thread(db, node_id: str) -> "str | None":
    """The conversation id this node is dispatched to — the typed kind='dispatch'
    node_edge (P8 M1/Q2), which replaced the legacy node binding column."""
    from dbops.dispatch_edge import dispatch_thread_of

    with db._connect() as conn:
        return dispatch_thread_of(conn, node_id)


def _node_repo(db, node: dict) -> str:
    """Resolve main-repo path for a nodes row (P8).

    Primary: node.main_repo_path (written by integrate/dispatch).
    Compat: the bound dispatch thread → thread.main_repo_path (the bound agent).
    Fallback: juggle's own repo (self-repo topics).
    """
    repo = (node.get("main_repo_path") or "").strip()
    if repo:
        return repo
    # Bound-thread binding lives in the typed kind='dispatch' node_edge.
    thread_id = _dispatch_thread(db, node["id"])
    if thread_id:
        thread = db.get_thread(thread_id) or {}
        repo = (thread.get("main_repo_path") or "").strip()
        if repo:
            return repo
    try:
        from pathlib import Path
        from juggle_cli_common import SRC_DIR
        return str(Path(SRC_DIR).parent.resolve())
    except Exception:
        return ""


def _node_is_merged(db, node: dict) -> bool:
    """G1 gate over a nodes row: merged iff merged_sha is set and on main."""
    sha = (node.get("merged_sha") or "").strip()
    if not sha:
        return False
    repo = _node_repo(db, node)
    if not repo:
        return False
    return sha_is_ancestor(repo, sha)


def _orphan_grace_minutes() -> float:
    """``watchdog.orphan_grace_minutes`` setting, defaulting to
    ``_DEFAULT_ORPHAN_GRACE_MINUTES``."""
    try:
        from juggle_settings import get_settings
        wd = get_settings().get("watchdog", {}) or {}
    except Exception:
        wd = {}
    return float(wd.get("orphan_grace_minutes", _DEFAULT_ORPHAN_GRACE_MINUTES))


def _owning_agent_busy(db, node: dict) -> bool:
    """True iff the topic's dispatch thread currently has a live busy agent —
    it may still be mid-finalize (``agent complete`` -> ``integrate``) on its
    own thread, which must never be mistaken for an orphan."""
    thread_id = _dispatch_thread(db, node["id"])
    if not thread_id:
        return False
    return db.get_agent_by_thread(thread_id) is not None


def _grace_period_elapsed(child_rows: list[dict], *, grace_minutes: float) -> bool:
    """True iff >= ``grace_minutes`` have elapsed since the LAST child task
    turned verified. A child with no ``verified_at`` stamp (e.g. legacy rows
    with state written directly) is treated as already elapsed — it never
    blocks detection indefinitely."""
    stamps = [r["verified_at"] for r in child_rows if r.get("verified_at")]
    if not stamps:
        return True
    latest = max(stamps)
    try:
        verified_dt = datetime.fromisoformat(latest)
    except ValueError:
        return True
    if verified_dt.tzinfo is None:
        verified_dt = verified_dt.replace(tzinfo=timezone.utc)
    elapsed = datetime.now(timezone.utc) - verified_dt
    return elapsed >= timedelta(minutes=grace_minutes)


def find_unmerged_completed_topics(db) -> list[dict]:
    """Return non-mirror root task nodes whose children are ALL verified but whose
    work is NOT merged to main.

    P8 M2: reads from nodes WHERE kind='topic'.
    A node with zero children, any unfinished child, or proven-merged work is
    excluded. So is a topic whose owning agent is still busy on its dispatch
    thread, or whose last child verified less than the grace period ago — both
    guard against racing the owning agent's own finalize sequence (2026-07-02).
    """
    with db._connect() as conn:
        parent_rows = conn.execute(
            "SELECT * FROM nodes "
            "WHERE kind='topic'"
        ).fetchall()
        parents = [dict(r) for r in parent_rows]

    grace_minutes = _orphan_grace_minutes()
    orphans: list[dict] = []
    for node in parents:
        # Skip nodes already stamped as verified
        if node.get("state") == "verified":
            continue
        with db._connect() as conn:
            child_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT state, verified_at FROM nodes WHERE parent_id=?",
                    (node["id"],),
                ).fetchall()
            ]
        if not child_rows:
            continue
        if not all(r["state"] == "verified" for r in child_rows):
            continue
        if _node_is_merged(db, node):
            continue
        if _owning_agent_busy(db, node):
            continue
        if not _grace_period_elapsed(child_rows, grace_minutes=grace_minutes):
            continue
        orphans.append(node)
    return orphans


def _topic_branch(db, node: dict) -> str:
    """The agent branch bound to this node (nodes.worktree_branch), or '' if none."""
    branch = (node.get("worktree_branch") or "").strip()
    if branch:
        return branch
    # Fallback: the bound thread's branch (kind='dispatch' node_edge → thread).
    thread_id = _dispatch_thread(db, node["id"])
    if thread_id:
        thread = db.get_thread(thread_id) or {}
        return (thread.get("worktree_branch") or "").strip()
    return ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def reconcile_out_of_band_merges(db, *, main: str = "main") -> list[str]:
    """Stamp ``merged_sha`` for completed topics whose work is already on main.

    P8: reads nodes as primary source; stamps nodes.merged_sha AND (compat)
    graph_topics.merged_sha. Calls db_topics.reconcile_topic_state for compat
    state update on graph_topics.

    G1-pure verification: only advances topics whose branch IS an ancestor of
    main — genuinely-unmerged topics (branch ahead of main, or branch ref gone)
    are left untouched. Returns reconciled node ids.

    Self-heals graph drift FIRST (DEFECT #4907): a task node with a NULL
    parent_id makes its topic look childless to ``find_unmerged_completed_topics``
    so it is never reconciled and the watchdog re-dispatches it forever. Heal a
    still-NULL parent_id from the frozen graph_tasks.topic_id before detecting, so
    a stranded-but-completed topic is found and stamped. (P8 c4-write-cut:
    nodes.state is authoritative and is NOT resynced from the frozen legacy table.)
    """
    from dbops import db_topics
    from dbops.migration_parent_relink import reconcile_node_parentage

    reconcile_node_parentage(db)

    reconciled: list[str] = []
    for node in find_unmerged_completed_topics(db):
        repo = _node_repo(db, node)
        branch = _topic_branch(db, node)
        if not repo or not branch:
            continue
        if not branch_merged_to_main(repo, branch, main=main):
            continue
        sha = resolve_branch_sha(repo, branch)
        if not sha:
            continue
        now = _now()
        with db._connect() as conn:
            # nodes is authoritative; set_topic_merged_sha already lockstep-mirrors
            # graph_topics, so stamp the node directly here (single store).
            conn.execute(
                "UPDATE nodes SET merged_sha=?, state='verified', updated_at=? WHERE id=?",
                (sha, now, node["id"]),
            )
            conn.commit()
        # Re-derive the topic state from its member tasks (idempotent on verified).
        try:
            db_topics.reconcile_topic_state(db, node["id"])
        except Exception:
            pass
        reconciled.append(node["id"])
    return reconciled


def flag_unmerged_completed_topics(db, *, dedup_window_hours: float = 24.0) -> list[str]:
    """Detect completed-but-unmerged topics and file a HIGH action item for each.

    Out-of-band merges are reconciled FIRST (``reconcile_out_of_band_merges``) so
    work already on main is verified, never re-flagged. Remaining orphans are
    deduped via ``watchdog_events`` (one flag per topic per ``dedup_window_hours``)
    so the watchdog tick can call this every cycle. Returns the topic ids flagged
    this pass.
    """
    reconcile_out_of_band_merges(db)
    orphans = find_unmerged_completed_topics(db)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=dedup_window_hours)).isoformat()

    flagged: list[str] = []
    for node in orphans:
        node_id = node["id"]
        with db._connect() as conn:
            recent = conn.execute(
                "SELECT id FROM watchdog_events "
                "WHERE thread_id=? AND event_type=? AND created_at > ?",
                (node_id, _ORPHAN_EVENT, cutoff),
            ).fetchone()
        if recent:
            continue
        label = node.get("title") or node_id
        # thread_id for the action item: the node's bound dispatch thread.
        thread_id = _dispatch_thread(db, node_id)
        db.add_action_item(
            thread_id=thread_id,
            message=(
                f"⚠️ topic {label} [{node_id}] completed but UNMERGED "
                f"(all tasks verified, no merged_sha) — run `juggle integrate "
                f"{node_id}` or recover its worktree. Never close-without-merge."
            ),
            type_="manual_step",
            priority="high",
        )
        db.add_watchdog_event(_GUARD_AGENT_ID, node_id, _ORPHAN_EVENT)
        flagged.append(node_id)
    return flagged
