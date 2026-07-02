"""TDD pins for the graph-panel frontier prune (T-cockpit-frontier-prune).

The panel's default view hides verified noise: it shows every NON-verified task
plus the one-hop verified deps of any shown task, and reports how many verified
tasks were pruned. Pure filter over (tasks, edges) — no Rich, no DB. Regression
pin: 2026-07-01 user report (37-node P2 rendered as an all-green wall).
"""
from __future__ import annotations

from juggle_cockpit_graph_layout import GraphTask, frontier_visible


def _chain(states: list[str]) -> tuple[list[GraphTask], list[tuple[str, str]]]:
    """Linear a->b->c... chain with the given per-task states."""
    tasks = [GraphTask(f"n{i}", f"T{i}", s) for i, s in enumerate(states)]
    edges = [(f"n{i}", f"n{i-1}") for i in range(1, len(states))]
    return tasks, edges


def test_hides_verified_keeps_non_verified():
    # n0..n4 verified, n5 running, n6 ready — frontier keeps the active tail plus
    # n4 (the direct verified dep of n5), hides n0..n3.
    tasks, edges = _chain(["verified"] * 5 + ["running", "ready"])
    visible, hidden = frontier_visible(tasks, edges)
    ids = [n.id for n in visible]
    assert "n5" in ids and "n6" in ids       # non-verified always shown
    assert "n4" in ids                        # one-hop verified dep of n5
    assert "n0" not in ids and "n3" not in ids  # deeper verified hidden
    assert hidden == 4                        # n0..n3 pruned


def test_only_direct_dep_of_shown_task_included():
    # n4 (verified) is a dep of running n5, but n3 (verified) is a dep of n4 —
    # NOT of any non-verified task, so n3 stays hidden (one hop only).
    tasks, edges = _chain(["verified"] * 5 + ["running"])
    visible, _ = frontier_visible(tasks, edges)
    ids = {n.id for n in visible}
    assert "n4" in ids
    assert "n3" not in ids


def test_topological_order_and_global_index_preserved():
    tasks, edges = _chain(["verified"] * 5 + ["running", "ready"])
    visible, _ = frontier_visible(tasks, edges)
    # Visible list is a subsequence of the full topological order.
    ids = [n.id for n in visible]
    assert ids == sorted(ids, key=lambda x: int(x[1:]))
    assert ids == ["n4", "n5", "n6"]


def test_fully_done_shows_tail():
    tasks, edges = _chain(["verified"] * 8)
    visible, hidden = frontier_visible(tasks, edges, tail=5)
    assert [n.id for n in visible] == ["n3", "n4", "n5", "n6", "n7"]
    assert hidden == 3


def test_fully_done_short_project_shows_all():
    tasks, edges = _chain(["verified"] * 3)
    visible, hidden = frontier_visible(tasks, edges, tail=5)
    assert [n.id for n in visible] == ["n0", "n1", "n2"]
    assert hidden == 0


def test_failed_states_are_shown():
    tasks, edges = _chain(["verified", "failed-verify", "blocked-failed"])
    visible, _ = frontier_visible(tasks, edges)
    ids = {n.id for n in visible}
    assert "n1" in ids and "n2" in ids
    assert "n0" in ids  # direct verified dep of failed n1


def test_no_hidden_when_nothing_verified():
    tasks, edges = _chain(["ready", "open", "running"])
    visible, hidden = frontier_visible(tasks, edges)
    assert len(visible) == 3
    assert hidden == 0


# ---------------------------------------------------------------------------
# Panel integration: default view prunes verified & keeps global numbering
# ---------------------------------------------------------------------------


def _render(panel, width=100) -> str:
    import io
    import re
    from rich.console import Console

    buf = io.StringIO()
    Console(width=width, file=buf, no_color=True, highlight=False).print(panel)
    return re.sub(r"\x1b\[[0-9;]*m", "", buf.getvalue())


def test_panel_default_prunes_verified_and_shows_summary():
    from juggle_cockpit_graph_panel import build_graph_panel

    tasks, edges = _chain(["verified"] * 6 + ["running", "ready"])
    panel = build_graph_panel(
        project_id="P2", tasks=tasks, edges=edges,
        selection=0, unread=0, width=100, height=20, pan_offset=0,
    )
    out = _render(panel)
    assert "n0" not in out and "n2" not in out       # deep verified hidden
    assert "n6" in out and "n7" in out               # running/ready shown
    assert "earlier hidden" in out                   # dim summary cell
    assert "5" in out                                # 5 pruned (n0..n4)


def test_panel_preserves_global_index_of_visible_cells():
    """Visible cells keep their ORIGINAL topological number — the running task at
    global index 8 renders '8', not a renumbered '1'."""
    from juggle_cockpit_graph_panel import build_graph_panel

    tasks, edges = _chain(["verified"] * 7 + ["running"])
    panel = build_graph_panel(
        project_id="P2", tasks=tasks, edges=edges,
        selection=0, unread=0, width=100, height=20, pan_offset=0,
    )
    out = _render(panel)
    # n7 is the 8th task (global index 8); its cell must show 8.
    assert "8" in out
    assert "n7" in out
