"""Phase-0 membership pins for the canonical dbops.terminal_states module.

Purpose (loop-entity plan 2026-07-04, Phase 0): the terminal / in-flight /
success-terminal vocabulary was re-declared as ~20 scattered frozenset/tuple
literals. Consolidating them into one module is a PURE mechanical refactor with
ZERO behaviour change. Each pin below freezes ONE migrated site's exact
pre-Phase-0 membership AND asserts the site now references the canonical object,
so the consolidation is provably behaviour-neutral and Phase 3's `delivered`
success-terminal becomes a single-line, test-guarded edit.

PHASE 3 UPDATE (2026-07-04, loop-entity V1): `delivered` was added to
TERMINAL_SUCCESS_STATES. Every set composed from that atom (the eight below
marked "+delivered") now INTENTIONALLY includes it — this is the reviewed,
behaviour-changing pin edit the regression-pin gate allows: `delivered` is a
second success-terminal that must be accepted everywhere `verified` is treated
as terminal-success (completion gate, guarded-upsert protection, tick-ownership,
reopen-before-add-task, replay supersede, reconcile/frontier/plan done-rollup,
topic-done derivation, cockpit done-glyph). The sets NOT composed from
TERMINAL_SUCCESS_STATES (IN_FLIGHT_STATES inverse, NON_TERMINAL_DISPLAY_STATUSES)
are unchanged — `delivered` is terminal, so it is correctly absent from both, and
the merged_sha GATE (graph_guards.topic_is_merged) stays verified-only.
"""
from __future__ import annotations

from pathlib import Path

from dbops import terminal_states as ts

_SRC = Path(__file__).resolve().parent.parent / "src"


def test_task_terminal_membership_unchanged():
    """_TASK_TERMINAL completion gate. Miss → a deliver-topic never completes →
    overlap-skip then skips every future fire → silent permanent loop death
    (critique LEAD FINDING)."""
    import juggle_cmd_agents_graph_topics as m

    expected = frozenset(
        {"verified", "delivered", "failed-exec", "failed-integration",
         "failed-verify", "blocked-failed", "cancelled"}  # +delivered (Phase 3)
    )
    assert ts.TASK_TERMINAL_STATES == expected
    assert m._TASK_TERMINAL is ts.TASK_TERMINAL_STATES


def test_protected_states_membership_unchanged():
    """db_graph.PROTECTED_STATES — guarded-upsert protection: a re-load must not
    touch a task in one of these."""
    from dbops import db_graph

    expected = frozenset({"dispatching", "running", "integrating", "verified",
                          "delivered"})  # +delivered (Phase 3): don't clobber it
    assert ts.PROTECTED_STATES == expected
    assert db_graph.PROTECTED_STATES is ts.PROTECTED_STATES


def test_tick_owned_states_membership_unchanged():
    """db_graph.TICK_OWNED_STATES — tick-owned states; manual send-task refuses
    without --force-task."""
    from dbops import db_graph

    expected = frozenset(
        {"ready", "dispatching", "running", "integrating", "verified",
         "delivered"}  # +delivered (Phase 3): tick owns the deliver terminal too
    )
    assert ts.TICK_OWNED_STATES == expected
    assert db_graph.TICK_OWNED_STATES is ts.TICK_OWNED_STATES


def test_terminal_reopen_topic_states_unchanged():
    """juggle_graph_add.TERMINAL_REOPEN_TOPIC_STATES — topic terminals add-task
    must reopen before attaching a task."""
    import juggle_graph_add as m

    expected = frozenset(
        {"verified", "delivered", "failed-exec", "failed-integration",
         "failed-verify", "blocked-failed", "integrated-unlanded"}  # +delivered
    )
    assert ts.TERMINAL_REOPEN_TOPIC_STATES == expected
    assert m.TERMINAL_REOPEN_TOPIC_STATES is ts.TERMINAL_REOPEN_TOPIC_STATES


def test_success_terminal_membership_unchanged():
    """juggle_spool_apply._SUCCESS_TERMINAL — spool replay/supersede success-sign set.
    D3 (2026-07-04): broadened from {verified,delivered,integrated-unlanded} to the
    FULL non-failure terminal set (adds done/cancelled/archived) so a legit completion
    replay against a reconciled/cancelled/archived target is superseded, not
    dead-lettered. This IS the reviewed, behaviour-changing pin edit the regression-pin
    gate allows — it must equal TERMINAL_STATES − FAILURE_TERMINAL_STATES."""
    import juggle_spool_apply as m

    expected = frozenset(
        {"verified", "delivered", "integrated-unlanded",
         "done", "cancelled", "archived"}  # +done/cancelled/archived (D3)
    )
    assert ts.SUCCESS_REPLAY_TERMINAL_STATES == expected
    assert ts.SUCCESS_REPLAY_TERMINAL_STATES == ts.TERMINAL_STATES - ts.FAILURE_TERMINAL_STATES
    assert m._SUCCESS_TERMINAL is ts.SUCCESS_REPLAY_TERMINAL_STATES


def test_completion_task_states_unchanged():
    """db_topics_reconcile._COMPLETION_TASK_STATES — reconcile completion set."""
    from dbops import db_topics  # noqa: F401  parent must load first (import-order)
    from dbops import db_topics_reconcile as m

    expected = frozenset({"verified", "delivered", "cancelled"})  # +delivered (Phase 3)
    assert ts.COMPLETION_TERMINAL_STATES == expected
    assert m._COMPLETION_TASK_STATES is ts.COMPLETION_TERMINAL_STATES


def test_merged_terminal_unchanged():
    """juggle_topic_derive._MERGED_TERMINAL — topic-done derivation set."""
    import juggle_topic_derive as m

    expected = frozenset({"verified", "delivered", "done", "cancelled"})  # +delivered
    assert ts.MERGED_TERMINAL_STATES == expected
    assert m._MERGED_TERMINAL is ts.MERGED_TERMINAL_STATES


def test_cockpit_merged_unchanged():
    """juggle_cockpit_tree._MERGED — cockpit tree done-glyph rollup set."""
    import juggle_cockpit_tree as m

    expected = frozenset({"verified", "delivered", "done"})  # +delivered (Phase 3)
    assert ts.MERGED_DONE_STATES == expected
    assert m._MERGED is ts.MERGED_DONE_STATES


def test_done_states_both_defs_unchanged():
    """The two duplicated DONE_STATES defs (frontier_layout + plan_layout) stay
    identical AND equal the canonical completion set — kills the sync-by-comment
    hazard."""
    import juggle_frontier_layout as fl
    import juggle_plan_layout as pl

    expected = frozenset({"verified", "delivered", "cancelled"})  # +delivered (Phase 3)
    assert frozenset(fl.DONE_STATES) == expected
    assert frozenset(pl.DONE_STATES) == expected
    assert fl.DONE_STATES is pl.DONE_STATES
    assert fl.DONE_STATES is ts.COMPLETION_TERMINAL_STATES


def test_in_progress_inverse_unchanged():
    """juggle_learnings_rollup._IN_PROGRESS — INVERSE set: pin the complement so
    a later `delivered` counts as done, not in-progress-forever."""
    import juggle_learnings_rollup as m

    expected = frozenset(
        {"open", "ready", "dispatching", "running", "integrating"}
    )
    assert ts.IN_FLIGHT_STATES == expected
    assert m._IN_PROGRESS is ts.IN_FLIGHT_STATES


def test_non_terminal_statuses_by_omission_unchanged():
    """juggle_cockpit_topic_cap._NON_TERMINAL_STATUSES — terminal-BY-OMISSION
    display statuses; a status absent from this set is treated as terminal."""
    import juggle_cockpit_topic_cap as m

    expected = frozenset(
        {"active", "background", "running", "current", "ready",
         "dispatching", "integrating"}
    )
    assert ts.NON_TERMINAL_DISPLAY_STATUSES == expected
    assert m._NON_TERMINAL_STATUSES is ts.NON_TERMINAL_DISPLAY_STATUSES


def test_no_replay_state_falls_through_to_spurious_dead_letter():
    """RCA D3 2026-07-04: supersede/advance sets not complementary → legit replay
    dead-letters. Every markable task state must be EITHER advanceable-to-integrating
    (mark_completion walks it) OR classified terminal for supersede (success-sign
    replay set ∪ failure set). A state in neither → mark_completion raises → the
    spool handler's sys.exit(1) → a legit, agreeing completion spuriously dead-lettered.
    Residual before the fix: {done, cancelled, archived}."""
    from dbops.db_graph_marking import _ADVANCE_TO_INTEGRATING

    covered = (
        frozenset(_ADVANCE_TO_INTEGRATING)
        | ts.SUCCESS_REPLAY_TERMINAL_STATES
        | ts.FAILURE_TERMINAL_STATES
    )
    assert covered == ts.TERMINAL_STATES | ts.IN_FLIGHT_STATES


def test_node_machine_terminal_targets_subset_of_canonical():
    """Every node-machine transition-target state is a KNOWN canonical state
    (terminal ∪ in-flight ∪ the one background carve-out). Forces Phase 3 to
    register `delivered` in TERMINAL_STATES before adding a `delivered` edge."""
    from dbops.db_node_machine import _NODE_TRANSITIONS

    targets = set(_NODE_TRANSITIONS.values())
    known = ts.TERMINAL_STATES | ts.IN_FLIGHT_STATES | ts.BACKGROUND_STATES
    missing = targets - known
    assert not missing, f"unregistered node-machine target states: {missing}"


def test_verified_sql_literal_inventory_pinned():
    """Grep-guard: the set of raw SQL 'verified' literal sites equals the pinned
    inventory. Regression guard so a new raw literal can't sneak in un-audited
    before Phase 3 (which uses this inventory as its `delivered` checklist)."""
    got = ts.scan_verified_sql_sites(_SRC)
    assert got == ts.VERIFIED_SQL_LITERAL_SITES
