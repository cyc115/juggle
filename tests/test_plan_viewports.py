"""Plan-view viewport smoke matrix (cockpit-plan-view, 2026-07-02). Renders the
pure wave-column grid at every real viewport profile (config/viewports.yaml)
and pins the narrow-viewport degradation: the anchor stays pinned and only as
many wave columns as fit are shown — the rest page in via ←/→ pan — so no grid
row ever exceeds the terminal width. Deterministic (pure render, no pty), so it
regression-guards the same overflow the CLI `--smoke --all-viewports` matrix
guards, without a flaky PTY drive of a pushed screen."""
from __future__ import annotations

import os
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cockpit_graph_layout import GraphTask  # noqa: E402
from juggle_frontier_render import strip_markup  # noqa: E402
from juggle_plan_layout import build_plan_layout  # noqa: E402
from juggle_plan_render import plan_grid_rows, render_plan_body  # noqa: E402


def _profiles() -> dict:
    path = os.path.join(os.path.dirname(__file__), "..", "config", "viewports.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)["profiles"]


def _chain_layout(n: int):
    """An n-deep chain → n waves, so even wide profiles exercise several columns
    and narrow profiles are forced to page."""
    tasks = [
        GraphTask(id=f"T{i}", title=f"Topic {i}", state="ready" if i == 0 else "open")
        for i in range(n)
    ]
    edges = [(f"T{i}", f"T{i - 1}") for i in range(1, n)]
    return build_plan_layout(tasks, edges, free_slots=2)


def test_plan_grid_fits_every_viewport_profile():
    profiles = _profiles()
    assert len(profiles) >= 7  # the documented 7-profile matrix
    layout = _chain_layout(8)
    for name, p in profiles.items():
        cols = p["cols"]
        grid, shown, total = plan_grid_rows(layout, pan=0, width=cols)
        assert grid, f"{name}: empty grid"
        for r in grid:
            assert len(strip_markup(r)) <= cols, (
                f"{name} ({cols} cols): grid row overflows — {len(strip_markup(r))} > {cols}"
            )


def test_narrow_viewport_pages_wave_columns():
    """The 80-col profile (2k_third) can't fit 8 columns → the anchor stays
    pinned, fewer waves show, and the pan affordance is surfaced."""
    layout = _chain_layout(8)
    grid, shown, total = plan_grid_rows(layout, pan=0, width=80)
    assert total == 8
    assert shown < total  # degradation engaged
    body = "\n".join(render_plan_body(layout, width=80))
    assert "showing waves" in body


def test_wide_viewport_shows_all_waves_without_pan():
    layout = _chain_layout(8)
    grid, shown, total = plan_grid_rows(layout, pan=0, width=240)
    assert shown == total  # all 8 waves fit on a 2k_full screen
    body = "\n".join(render_plan_body(layout, width=240))
    assert "showing waves" not in body  # no pan affordance when everything fits


def test_pan_reveals_later_waves():
    """Paging right advances the visible window without ever overflowing."""
    layout = _chain_layout(8)
    _, _, total = plan_grid_rows(layout, pan=0, width=80)
    grid0, _, _ = plan_grid_rows(layout, pan=0, width=80)
    grid_panned, shown, _ = plan_grid_rows(layout, pan=3, width=80)
    assert grid_panned != grid0  # window moved
    for r in grid_panned:
        assert len(strip_markup(r)) <= 80


def test_smoke_graph_seed_builds_a_wave_dag(tmp_path):
    """The --smoke-plan seed arms one project whose topic DAG has real waves
    (root → wide fan-out → tail), so g→l during smoke opens a populated Plan
    view."""
    from juggle_smoke import seed_smoke_graph_db
    from juggle_cockpit_graph_dag import load_graph_dags

    db_path = str(tmp_path / "juggle.db")
    seed_smoke_graph_db(db_path)
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=db_path)
    with db._connect() as conn:
        dags = load_graph_dags(conn)
    assert dags, "seed produced no armed-project DAG"
    dag = dags[0]
    layout = build_plan_layout(dag.tasks, dag.edges, free_slots=3)
    assert len(layout.waves) >= 3  # root, wide fan-out, tail
    widest = max(len(w.cards) for w in layout.waves)
    assert widest >= 20  # the wide fan-in the old railroad IndexError'd on


def test_plan_chrome_markers_match_plan_bands_not_cockpit():
    """The plan view replaces cockpit chrome with its own; the plan-aware
    markers accept its title + legend/critical bands, where cockpit markers
    would not."""
    from juggle_smoke import (
        _PLAN_FOOTER_MARKERS, _PLAN_HEADER_MARKERS, check_chrome_present,
    )

    grid = (
        ["◈ Plan — Smoke Plan", "WAVE 1 runnable now — 1 parallel", ""]
        + ["│ ○ Wide 0       │"] * 5
        + ["◐ running  ◇ ready · j/k select · z fold · L railroad · q/esc close"]
    )
    assert not check_chrome_present(grid)["pass"]  # cockpit markers miss
    assert check_chrome_present(
        grid, _PLAN_HEADER_MARKERS, _PLAN_FOOTER_MARKERS)["pass"]
