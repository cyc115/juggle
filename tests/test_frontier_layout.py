"""Frontier Railroad layout engine tests (fr-layout, 2026-07-02). Pure module:
project snapshot (topics + task counts, topic dep edges, free slots) ->
deterministic row list (trunk stub, node rows in topological order, wave
bands) + lane assignments for forward rails + critical-path + fold state.

Wave/critical-path math adapted from the parked cyc_CR:530bee5 Plan-view
engine (juggle_plan_layout); lane packing for the forward-rail gutter reuses
the existing juggle_cockpit_graph_lanes.assign_lanes core (Surface-D/B,
2026-06-30 graph railroad) rather than re-deriving it.
"""
from juggle_cockpit_graph_layout import GraphTask
from juggle_frontier_layout import build_frontier_layout


def _t(i, state="open", done=None, total=None):
    return GraphTask(i, i.upper(), state, tasks_done=done, tasks_total=total)


# ---------------------------------------------------------------------------
# Trunk stub + basic wave layering
# ---------------------------------------------------------------------------


def test_all_verified_collapses_to_trunk_stub_only():
    tasks = [_t("a", "verified"), _t("b", "verified"), _t("c", "verified")]
    layout = build_frontier_layout(tasks, [("b", "a"), ("c", "b")], free_slots=3)
    assert layout.anchor_count == 3
    assert layout.rows == ()
    assert layout.critical_path == ()


def test_single_chain_layers_one_topic_per_wave_top_down():
    """Reading order = execution order: wave 0 (ready) row appears FIRST."""
    tasks = [_t("a", "ready"), _t("b", "open"), _t("c", "open")]
    layout = build_frontier_layout(tasks, [("b", "a"), ("c", "b")], free_slots=2)
    ids_in_order = [r.id for r in layout.rows if r.kind != "wave-band"]
    assert ids_in_order == ["a", "b", "c"]
    waves = [r.wave for r in layout.rows if r.kind != "wave-band"]
    assert waves == [0, 1, 2]


def test_wave_band_rows_are_inserted_between_depth_increments():
    tasks = [_t("a", "ready"), _t("b", "open")]
    layout = build_frontier_layout(tasks, [("b", "a")], free_slots=4)
    bands = [r for r in layout.rows if r.kind == "wave-band"]
    assert len(bands) == 1
    assert bands[0].wave == 1
    assert bands[0].parallel_count == 1


def test_wave_zero_band_shows_free_slot_budget():
    """wave-1 band (first open wave) shows 'N can start in parallel, M slots
    free -> K queues'."""
    tasks = [_t("a", "ready"), _t("b", "ready"), _t("c", "ready")]
    layout = build_frontier_layout(tasks, [], free_slots=2)
    band = next(r for r in layout.rows if r.kind == "wave-band")
    assert band.wave == 0
    assert band.parallel_count == 3
    assert band.free_slots == 2
    assert band.queued == 1


def test_diamond_dag_wave_layering():
    tasks = [_t("a", "ready"), _t("b", "open"), _t("c", "open"), _t("d", "open")]
    edges = [("b", "a"), ("c", "a"), ("d", "b"), ("d", "c")]
    layout = build_frontier_layout(tasks, edges, free_slots=4)
    by_id = {r.id: r for r in layout.rows if r.kind != "wave-band"}
    assert by_id["a"].wave == 0
    assert by_id["b"].wave == 1 and by_id["c"].wave == 1
    assert by_id["d"].wave == 2


# ---------------------------------------------------------------------------
# Row kind / running node placement / blocked-on
# ---------------------------------------------------------------------------


def test_running_node_stays_at_true_wave_never_pinned_to_now_band():
    tasks = [_t("a", "verified"), _t("b", "running"), _t("c", "open")]
    edges = [("b", "a"), ("c", "b")]
    layout = build_frontier_layout(tasks, edges, free_slots=2)
    by_id = {r.id: r for r in layout.rows if r.kind != "wave-band"}
    assert by_id["b"].kind == "running"
    assert by_id["b"].wave == 0  # true dependency-depth wave, not a NOW band
    assert by_id["c"].wave == 1


def test_blocked_row_names_unmet_dependency():
    tasks = [_t("a", "open"), _t("b", "open")]
    layout = build_frontier_layout(tasks, [("b", "a")], free_slots=1)
    row_b = next(r for r in layout.rows if r.id == "b")
    assert row_b.kind == "blocked"
    assert row_b.blocked_on == ("A",)


def test_task_counts_carried_onto_topic_rows():
    tasks = [_t("a", "running", done=2, total=5)]
    layout = build_frontier_layout(tasks, [], free_slots=1)
    row = next(r for r in layout.rows if r.id == "a")
    assert (row.tasks_done, row.tasks_total) == (2, 5)


# ---------------------------------------------------------------------------
# Critical path
# ---------------------------------------------------------------------------


def test_critical_path_is_the_longest_unfinished_chain():
    tasks = [_t("a", "ready"), _t("b", "open"), _t("c", "open"), _t("d", "open")]
    # a->nothing; c depends on b depends on a; d depends on a (shorter chain)
    edges = [("b", "a"), ("c", "b"), ("d", "a")]
    layout = build_frontier_layout(tasks, edges, free_slots=2)
    assert layout.critical_path == ("a", "b", "c")
    on_cp = {r.id for r in layout.rows if r.kind != "wave-band" and r.on_critical_path}
    assert on_cp == {"a", "b", "c"}
    assert len(layout.critical_path_lanes) == len(layout.critical_path)


def test_no_open_tasks_means_no_critical_path():
    layout = build_frontier_layout([_t("a", "verified")], [], free_slots=1)
    assert layout.critical_path == ()
    assert layout.critical_path_lanes == ()


# ---------------------------------------------------------------------------
# Lane assignment (forward-rail gutter) — reuses graph_lanes.assign_lanes
# ---------------------------------------------------------------------------


def test_lanes_are_non_negative_and_bounded_by_lane_count():
    tasks = [_t("a", "ready")] + [_t(f"m{i}", "open") for i in range(20)]
    edges = [(f"m{i}", "a") for i in range(20)]
    layout = build_frontier_layout(tasks, edges, free_slots=4)
    rows = [r for r in layout.rows if r.kind != "wave-band"]
    assert all(0 <= r.lane < layout.lane_count for r in rows)


def test_wide_fan_in_convergence_does_not_crash_lane_allocation():
    """Bounds-safety pin, carrying forward the 2026-07-02 gitlog-view
    IndexError incident: a fan-out branch lane closed by a smaller-index
    fan-in convergence before ever becoming any row's own `.lane` must not
    raise (root cause: lane_count computed from peak `len(lanes)`, not
    max(node.lane) — see juggle_cockpit_graph_lanes.assign_lanes)."""
    tasks = [_t("c", "verified"), _t("d", "open"), _t("e", "ready")]
    edges = [("d", "c"), ("e", "c"), ("e", "d")]
    layout = build_frontier_layout(tasks, edges, free_slots=2)  # must not raise
    assert {r.id for r in layout.rows if r.kind != "wave-band"} <= {"d", "e"}


def test_50_node_wide_fan_in_lane_allocation_is_bounded_and_deterministic():
    tasks = [_t("root", "verified")] + [_t(f"n{i}", "open") for i in range(50)]
    edges = [(f"n{i}", "root") for i in range(50)]
    first = build_frontier_layout(tasks, edges, free_slots=8)
    again = build_frontier_layout(tasks, edges, free_slots=8)
    assert [(r.id, r.lane) for r in first.rows if r.kind != "wave-band"] == \
        [(r.id, r.lane) for r in again.rows if r.kind != "wave-band"]
    assert first.lane_count >= 1


# ---------------------------------------------------------------------------
# Fold state (owned by the caller — screen toggles which waves are folded)
# ---------------------------------------------------------------------------


def test_folded_wave_replaced_by_a_single_summary_row():
    tasks = [_t("a", "ready"), _t("b", "open"), _t("c", "open")]
    layout = build_frontier_layout(
        tasks, [("b", "a"), ("c", "a")], free_slots=2, folded_waves={1},
    )
    kinds = [r.kind for r in layout.rows]
    assert "fold-summary" in kinds
    summary = next(r for r in layout.rows if r.kind == "fold-summary")
    assert summary.wave == 1
    assert summary.parallel_count == 2
    assert not any(r.id in ("b", "c") for r in layout.rows if r.kind != "fold-summary")


def test_no_folded_waves_by_default():
    tasks = [_t("a", "ready"), _t("b", "open")]
    layout = build_frontier_layout(tasks, [("b", "a")], free_slots=1)
    assert not any(r.kind == "fold-summary" for r in layout.rows)
