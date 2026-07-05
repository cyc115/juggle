"""dbops.terminal_states — the SINGLE canonical home for node state-classification
sets (Phase 0, loop-entity plan 2026-07-04).

Before this module the terminal / in-flight / success vocabulary was re-declared
as ~20 scattered frozenset (and tuple) literals across the CLI + dbops layers.
Each site is now composed from a small set of disjoint atomic building blocks so
there is ONE source of truth. Adding a new terminal (Phase 3's ``delivered``)
becomes a single-line edit here that every consumer picks up through its import.

Design constraints:
  * PURE data + tiny predicates — no DB, no CLI imports (deepest consumers live
    in ``dbops`` — db_graph / db_topics / db_node_machine — so this must not
    import upward and create a cycle).
  * Each exported named set reproduces exactly ONE pre-Phase-0 literal's
    membership; ``tests/test_terminal_states.py`` pins every one to its frozen
    old value so the consolidation is provably behaviour-neutral.

Atomic building blocks (disjoint):
  ACTIVE_STATES            executing work: dispatching / running / integrating
  TERMINAL_SUCCESS_STATES  work-succeeded merged/verified terminal   (Phase 3 → +delivered)
  ASYNC_PENDING_STATES     tests-green, async-land pending (below verified)
  DONE_ROLLUP_STATES       G1-passed post-verified rollup terminal
  CANCELLED_STATES         soft completion terminal
  FAILURE_TERMINAL_STATES  the four failure/blocked terminals
  ARCHIVED_STATES          archived terminal
  BACKGROUND_STATES        background conversation agent (non-terminal, non-active)
"""
from __future__ import annotations

import re
from pathlib import Path

# ── Atomic building blocks ──────────────────────────────────────────────────────
ACTIVE_STATES = frozenset({"dispatching", "running", "integrating"})
# Two success-terminals (Phase 3, 2026-07-04): 'verified' (delivery='merge',
# backed by a merged_sha ancestor of main) and 'delivered' (delivery='deliver',
# non-merge work whose proof is a verify_cmd/attestation — NO merged_sha). Every
# consumer composed from this set (TASK_TERMINAL_STATES, TERMINAL_STATES, …)
# inherits 'delivered' for free. The merged_sha GATE (graph_guards.topic_is_merged
# / db_topics._verified_allowed) reads the column directly and is DELIBERATELY not
# composed from this set, so verified⟺merged stays verified-only.
TERMINAL_SUCCESS_STATES = frozenset({"verified", "delivered"})
ASYNC_PENDING_STATES = frozenset({"integrated-unlanded"})
DONE_ROLLUP_STATES = frozenset({"done"})
CANCELLED_STATES = frozenset({"cancelled"})
FAILURE_TERMINAL_STATES = frozenset(
    {"failed-exec", "failed-integration", "failed-verify", "blocked-failed"}
)
ARCHIVED_STATES = frozenset({"archived"})
BACKGROUND_STATES = frozenset({"background"})

# ── Headline sets (Phase 3 seam) ────────────────────────────────────────────────
# In-flight / not-yet-complete task states (== learnings-rollup's old _IN_PROGRESS).
IN_FLIGHT_STATES = frozenset({"open", "ready"}) | ACTIVE_STATES

# Every terminal state. Phase 3 gains "delivered" through TERMINAL_SUCCESS_STATES.
TERMINAL_STATES = (
    TERMINAL_SUCCESS_STATES
    | ASYNC_PENDING_STATES
    | DONE_ROLLUP_STATES
    | CANCELLED_STATES
    | FAILURE_TERMINAL_STATES
    | ARCHIVED_STATES
)

# ── Named consumer sets (one per migrated site; membership pinned by tests) ──────
# Completion gate for a topic's tasks (juggle_cmd_agents_graph_topics._TASK_TERMINAL,
# also juggle_cmd_graph_cancel). verified + failures + cancelled.
TASK_TERMINAL_STATES = (
    TERMINAL_SUCCESS_STATES | FAILURE_TERMINAL_STATES | CANCELLED_STATES
)

# Guarded-upsert protection: a re-load must not touch a task in one of these
# (db_graph.PROTECTED_STATES). Active work + verified.
PROTECTED_STATES = ACTIVE_STATES | TERMINAL_SUCCESS_STATES

# Watchdog-tick-owned states — manual send-task refuses without --force
# (db_graph.TICK_OWNED_STATES). ready + active + verified.
TICK_OWNED_STATES = frozenset({"ready"}) | ACTIVE_STATES | TERMINAL_SUCCESS_STATES

# Topic terminals add-task must REOPEN before attaching a task
# (juggle_graph_add.TERMINAL_REOPEN_TOPIC_STATES). verified + failures + async-pending.
TERMINAL_REOPEN_TOPIC_STATES = (
    TERMINAL_SUCCESS_STATES | FAILURE_TERMINAL_STATES | ASYNC_PENDING_STATES
)

# Spool replay/supersede success terminals (juggle_spool_apply._SUCCESS_TERMINAL).
SUCCESS_REPLAY_TERMINAL_STATES = TERMINAL_SUCCESS_STATES | ASYNC_PENDING_STATES

# Task counts-as-complete for topic rollup (reconcile._COMPLETION_TASK_STATES) and
# the frontier/plan-layout DONE_STATES. verified + cancelled.
COMPLETION_TERMINAL_STATES = TERMINAL_SUCCESS_STATES | CANCELLED_STATES

# Topic-done derivation terminals (juggle_topic_derive._MERGED_TERMINAL).
MERGED_TERMINAL_STATES = (
    TERMINAL_SUCCESS_STATES | DONE_ROLLUP_STATES | CANCELLED_STATES
)

# Cockpit-tree "merged/done" glyph set (juggle_cockpit_tree._MERGED).
MERGED_DONE_STATES = TERMINAL_SUCCESS_STATES | DONE_ROLLUP_STATES

# Topic-cap terminal-BY-OMISSION display statuses — NOT node states (these are
# node_translation display statuses: 'active'/'current' have no node-state form).
# A status absent from this set is treated as terminal by cap_topics.
NON_TERMINAL_DISPLAY_STATUSES = frozenset(
    {
        "active",
        "background",
        "running",
        "current",
        "ready",
        "dispatching",
        "integrating",
    }
)


# ── Predicates ──────────────────────────────────────────────────────────────────
def is_terminal(state: str) -> bool:
    """True iff ``state`` is any terminal state."""
    return state in TERMINAL_STATES


def is_success_terminal(state: str) -> bool:
    """True iff ``state`` is a success terminal (verified; Phase 3 + delivered)."""
    return state in TERMINAL_SUCCESS_STATES


def is_in_flight(state: str) -> bool:
    """True iff ``state`` is in-flight / not-yet-complete."""
    return state in IN_FLIGHT_STATES


# ── Raw-SQL 'verified' literal inventory (Phase-3 checklist / regression guard) ──
# These raw SQL string literals compare state to 'verified' and CANNOT import a
# Python set. Phase 0 does NOT rewrite them (behaviour-drift risk); instead this
# pinned inventory is asserted by tests so a NEW raw literal can't sneak in
# un-audited, and Phase 3 has an explicit checklist of sites to consider for the
# 'delivered' success-terminal.
_SQL_VERIFIED_RE = re.compile(r"(?:state\s*(?:!=|=)\s*'verified'|IN\s*\('verified')")


def scan_verified_sql_sites(src_root: str | Path) -> dict[str, tuple[str, ...]]:
    """Scan ``src_root`` for raw SQL 'verified' literals inside Python string
    fragments (stripped lines starting with a double-quote), returning
    {posix-relpath: sorted tuple of stripped matching lines}. Comments/docstrings
    (starting with '#', backtick or paren) are excluded."""
    root = Path(src_root)
    out: dict[str, list[str]] = {}
    for py in sorted(root.rglob("*.py")):
        if py.name == "terminal_states.py":
            continue  # this module holds the pinned inventory itself
        try:
            lines = py.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for raw in lines:
            s = raw.strip()
            if not s.startswith('"'):
                continue
            if not _SQL_VERIFIED_RE.search(s):
                continue
            rel = py.relative_to(root).as_posix()
            out.setdefault(rel, []).append(s)
    return {k: tuple(sorted(v)) for k, v in sorted(out.items())}


# Pinned as of Phase 0 (2026-07-04). Regenerate deliberately (reviewed) only when
# Phase 3 rewrites a literal to include 'delivered'.
VERIFIED_SQL_LITERAL_SITES: dict[str, tuple[str, ...]] = {
    "dbops/db_graph.py": (
        "\"  WHERE e.node_id=n.id AND e.kind='dep' AND d.state NOT IN ('verified','delivered')) \"",
    ),
    "dbops/db_graph_edges.py": (
        "\"WHERE e.node_id=? AND e.kind='dep' AND d.state NOT IN ('verified','delivered') \"",
    ),
    "dbops/db_topics.py": (
        "\"  AND dt.state NOT IN ('verified','delivered')) \"",
    ),
    "dbops/orphan_reconcile.py": (
        "\"UPDATE nodes SET merged_sha=?, state='verified', updated_at=? WHERE id=?\",",
    ),
    "juggle_add_node.py": (
        "\"WHERE e.node_id=? AND e.kind='dep' AND d.state NOT IN ('verified','delivered') LIMIT 1\",",
    ),
    # Phase 3 (2026-07-04): both rollup literals gained 'delivered' so a delivered
    # topic/task counts as done (open-count) — deliver loops never read incomplete.
    # P3b (2026-07-05): the open-count (opn) literal moved with gather_project_activity
    # to juggle_cockpit_graph_activity.py (extract-first); the per-topic done-count
    # stays here on _load_one.
    "juggle_cockpit_graph_activity.py": (
        "\"SUM(CASE WHEN state NOT IN ('verified','delivered','cancelled') THEN 1 ELSE 0 END) AS opn, \"",
    ),
    "juggle_cockpit_graph_dag.py": (
        "\"SUM(CASE WHEN state IN ('verified','delivered','cancelled') THEN 1 ELSE 0 END) AS done, \"",
    ),
    "juggle_learnings_rollup.py": (
        "\"AND state='verified'\",",
    ),
}
