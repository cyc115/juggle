"""dbops.orphan_guard — detect & surface completed-but-unmerged topics (G5).

P8 (Task 4.2): orphan detection reads exclusively from the unified nodes table —
topic nodes (kind='topic'), their child states, and the bound dispatch thread
(the typed kind='dispatch' node_edge). reconcile_out_of_band_merges stamps
nodes.merged_sha (lockstep set_topic_merged_sha mirrors graph_topics).

Incident (2026-06-17): a send_task false-negative made the watchdog treat a
successful dispatch as failed, so a completed topic's work sat committed-in-
worktree but UNMERGED. G1 (graph_guards.topic_is_merged) keeps such a topic out
of ``verified``; this guard DETECTS it and files a HIGH action item so a
completed topic is never silently closed without merge — always a blocker.

Pure detection + flagging. Owns no state transition (db_topics stays the writer)
and no integrate logic (juggle_cmd_integrate).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from dbops.graph_guards import sha_is_ancestor

_log = logging.getLogger(__name__)

# watchdog_events.event_type used to dedup repeated flags for the same topic.
_ORPHAN_EVENT = "topic_unmerged_orphan"
# Sentinel agent_id (watchdog_events.agent_id is NOT NULL) — this guard is not
# bound to a single agent.
_GUARD_AGENT_ID = "orphan-guard"

# Grace period (minutes) after the last child task turns verified before a topic
# becomes eligible for orphan detection. Incident (2026-07-02): mark-task
# verified happens BEFORE the owning agent's own finalize (agent complete ->
# integrate), so detecting the instant all children verify races that finalize
# and fires a false HIGH item on nearly every normal completion.
_DEFAULT_ORPHAN_GRACE_MINUTES = 5.0

# Hard-wedge escalation (2026-07-03 integrate-wedge fix 5): a topic that has sat
# in 'integrating' longer than this is a STUCK agent, not a mid-finalize one —
# surface it as a HIGH blocker EVEN THOUGH the finalize-race guard (busy agent /
# grace) would otherwise skip it. The incident: 3 topics wedged 1.5 h with
# still-'busy' agents produced ZERO action items.
_DEFAULT_INTEGRATING_WEDGE_MINUTES = 60.0


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


def _integrating_wedge_minutes() -> float:
    """``watchdog.integrating_wedge_minutes`` setting (default
    ``_DEFAULT_INTEGRATING_WEDGE_MINUTES``)."""
    try:
        from juggle_settings import get_settings
        wd = get_settings().get("watchdog", {}) or {}
    except Exception:
        wd = {}
    return float(wd.get("integrating_wedge_minutes", _DEFAULT_INTEGRATING_WEDGE_MINUTES))


def _hard_wedged(node: dict, *, wedge_minutes: float) -> bool:
    """True iff the topic has been in 'integrating' longer than ``wedge_minutes``
    (its ``updated_at`` is stamped when it entered that state) — a stuck agent,
    which the finalize-race busy/grace guard must NOT keep suppressing (fix 5)."""
    if node.get("state") != "integrating":
        return False
    ts = node.get("updated_at")
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt) >= timedelta(minutes=wedge_minutes)


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
    wedge_minutes = _integrating_wedge_minutes()
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
        # Fix 5: a HARD-wedged 'integrating' topic (stuck agent) escalates past
        # the finalize-race busy/grace guard — otherwise a still-'busy' stuck
        # agent suppresses the blocker forever (the 1.5 h incident, zero items).
        if not _hard_wedged(node, wedge_minutes=wedge_minutes):
            if _owning_agent_busy(db, node):
                continue
            if not _grace_period_elapsed(child_rows, grace_minutes=grace_minutes):
                continue
        orphans.append(node)
    return orphans


def _topic_branch(db, node: dict) -> str:
    """The topic's OWN durably-recorded branch (nodes.worktree_branch);
    falls back to the bound thread's live branch ONLY when that thread is
    proven to not ALSO be some OTHER topic's dispatch home (T-fix-backfill-
    sha-misattribution, 2026-07-03 incident — a reused/shared thread's live
    field reflects whichever topic dispatched through it MOST RECENTLY, not
    necessarily this one; see ``dbops.graph_guards.thread_solely_bound_to``).
    """
    from dbops.graph_guards import thread_solely_bound_to

    branch = (node.get("worktree_branch") or "").strip()
    if branch:
        return branch
    thread_id = _dispatch_thread(db, node["id"])
    if thread_id and thread_solely_bound_to(db, thread_id, node["id"]):
        thread = db.get_thread(thread_id) or {}
        return (thread.get("worktree_branch") or "").strip()
    return ""


def _branch_ahead_of_trunk(repo: str, branch: str) -> bool:
    """True iff ``branch`` is a LIVE ref in ``repo`` with >= 1 commit ahead of
    trunk — genuine unmerged work. Distinguishes it from a PHANTOM branch:
    recorded in nodes.worktree_branch but GC'd / never diverged (2026-07-20
    false-escalation fix). Reuses the same rev-list/trunk primitives the
    integrate path already relies on — never hand-rolled here."""
    from dbops.graph_guards import _repo_trunk, resolve_branch_sha
    from juggle_integrate_submit import _commit_count

    if not repo or not branch:
        return False
    if not resolve_branch_sha(repo, branch):
        return False  # branch ref missing/gone — phantom, fail-closed
    trunk = _repo_trunk(repo)
    return _commit_count(repo, trunk, branch) > 0


def flag_unmerged_completed_topics(db, *, dedup_window_hours: float = 24.0) -> list[str]:
    """Detect completed-but-unmerged topics and file a HIGH action item for each.

    Out-of-band merges are reconciled FIRST (``reconcile_out_of_band_merges``) so
    work already on main is verified, never re-flagged. Remaining orphans are
    deduped via ``watchdog_events`` (one flag per topic per ``dedup_window_hours``)
    so the watchdog tick can call this every cycle. Returns the topic ids flagged
    this pass. (``reconcile_out_of_band_merges`` is re-exported at module bottom.)
    """
    reconcile_out_of_band_merges(db)
    orphans = find_unmerged_completed_topics(db)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=dedup_window_hours)).isoformat()

    flagged: list[str] = []
    for node in orphans:
        node_id = node["id"]
        branch = _topic_branch(db, node)
        if not branch:
            # Report-only node (e.g. weekly refactor SCAN) — never produces a
            # branch, so there is nothing to merge. Skip silently.
            continue
        repo = _node_repo(db, node)
        if not _branch_ahead_of_trunk(repo, branch):
            # Phantom branch: recorded but absent from git (GC'd) or 0 commits
            # ahead of trunk — the "run juggle integrate" advice would be
            # unactionable/misleading. Log a distinct diagnostic instead of
            # silently dropping it (no action item — nothing to escalate).
            _log.info(
                "orphan_guard: topic %s [%s] recorded branch %r in %s is "
                "missing from git or has 0 commits ahead of trunk — skipping "
                "the 'run juggle integrate' escalation (nothing to merge)",
                node.get("title") or node_id, node_id, branch, repo,
            )
            continue
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
        item_id = db.add_action_item(
            thread_id=thread_id,
            message=(
                f"⚠️ topic {label} [{node_id}] completed but UNMERGED "
                f"(all tasks verified, no merged_sha) — run `juggle integrate "
                f"{node_id}` or recover its worktree. Never close-without-merge."
            ),
            type_="manual_step",
            priority="high",
        )
        # Record the action_item_id on the marker event so a later merge can
        # auto-ack THIS exact item (clear_stale_unmerged_escalations) — exact
        # linkage, never a message-string grep (2026-07-04 autoclear fix).
        db.add_watchdog_event(
            _GUARD_AGENT_ID, node_id, _ORPHAN_EVENT, snapshot_path=str(item_id)
        )
        flagged.append(node_id)
    return flagged


# ── reconciler extracted to dbops.orphan_reconcile (LOC gate + concern split) ──
# Re-exported so the existing orphan_guard.reconcile_out_of_band_merges call
# surface (juggle_cmd_doctor, tests) is unchanged. Bottom import breaks the
# detection⇄reconcile cycle (orphan_reconcile imports our helpers lazily).
from dbops.orphan_reconcile import reconcile_out_of_band_merges  # noqa: E402,F401

# Auto-clear (2026-07-04) extracted to dbops.orphan_autoclear (LOC gate).
# Re-exported so orphan_guard.clear_stale_unmerged_escalations (reconcile +
# tests) is unchanged; that module imports our helpers lazily (no cycle).
from dbops.orphan_autoclear import clear_stale_unmerged_escalations  # noqa: E402,F401
