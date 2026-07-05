"""P5a — Loops render band (spec §4) regression pins.

Incident/date anchors are in each test docstring. These pins lock the
loops sub-section of the Agents panel: it renders active loops, derives the
``▸Pn`` pointer from the loop PROJECT (never the INBOX conversation mirror),
computes the last-outcome badge from EXISTING columns only (no ``last_status``
column exists), shows a paused loop distinctly, keeps loops out of the P-slot
project headers, and collapses to a summary at scale.
"""
from __future__ import annotations

import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

pytest.importorskip("rich")

from rich.console import Console

from juggle_cockpit_loops import (
    LoopRow,
    collapse_loop_lines,
    fetch_loops,
    loop_badge,
    loop_glyph,
)
from juggle_cockpit_view import render_agents
from juggle_cockpit_model import snapshot
from juggle_db import JuggleDB
from juggle_cmd_loop_create import create_loop_atomic


# ── fixtures ─────────────────────────────────────────────────────────────────


def _single_topic_template(delivery="merge", role="coder", ntasks=1):
    tasks = [
        {"id": f"t{i}", "title": f"task {i}", "prompt": f"do {i}",
         "role": role, "model": "sonnet", "verify_cmd": None, "deps": []}
        for i in range(ntasks)
    ]
    return {"topics": [{
        "id": "digest", "title": "Daily digest", "objective": "obj",
        "delivery": delivery, "tasks": tasks,
    }]}


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    d._set_session_key_external("session_id", "sessA")
    return d


def _row(**kw) -> LoopRow:
    base = dict(
        loop_id="L1", name="issue-triage", cadence="every 6h", next_run=None,
        project_id="P8", status="active", consecutive_failures=0, run_seq=3,
        last_run_at="2026-07-05 10:00",
    )
    base.update(kw)
    return LoopRow(**base)


def _render(panel, width=60) -> str:
    buf = io.StringIO()
    Console(file=buf, no_color=True, width=width).print(panel)
    return buf.getvalue()


# ── pins ─────────────────────────────────────────────────────────────────────


def test_band_renders_active_loops():
    """band-renders-active-loops (P5a, 2026-07-05): the Agents panel gains a
    'Loops' sub-section that renders each active loop with the 🔁 glyph."""
    panel = render_agents([], loops=[_row(name="issue-triage")])
    text = _render(panel)
    assert "Loops" in text, f"missing Loops heading: {text!r}"
    assert "issue-triage" in text, f"missing loop name: {text!r}"
    assert "🔁" in text, f"missing loop glyph: {text!r}"


def test_pn_is_loop_project_not_inbox(db):
    """Pn-is-loop-project-not-INBOX (P5a, 2026-07-05): ▸Pn points at the loop's
    own kind='loop' PROJECT, never the INBOX conversation mirror node."""
    created = create_loop_atomic(
        db, template=_single_topic_template(), cadence="every 6h", name="issue-triage"
    )
    rows = fetch_loops(db)
    assert len(rows) == 1
    r = rows[0]
    assert r.project_id == created["project_id"], (
        f"loop project mismatch: {r.project_id!r} != {created['project_id']!r}"
    )
    assert r.project_id != "INBOX"
    text = _render(render_agents([], loops=rows))
    assert f"▸{created['project_id']}" in text, f"missing ▸Pn pointer: {text!r}"
    assert "▸INBOX" not in text


def test_badge_reflects_failure_success_never_run():
    """badge-reflects-failure-success-never-run (P5a, 2026-07-05): the last-outcome
    badge is derived from consecutive_failures / last_run_at / run_seq only —
    there is NO loops.last_status column."""
    # failing: consecutive_failures > 0 wins
    assert loop_badge(2, "2026-07-05 10:00", 5) == "⚠2"
    # succeeded: no failures + a completed run → ✓<run_seq>
    assert loop_badge(0, "2026-07-05 10:00", 3) == "✓3"
    # never run: no failures, no last_run_at → ·
    assert loop_badge(0, None, 0) == "·"


def test_paused_loop_shown_distinctly():
    """paused-loop-shown-distinctly (P5a, 2026-07-05): a paused (circuit-broken)
    loop renders with a glyph distinct from an active loop."""
    assert loop_glyph("paused") != loop_glyph("active")
    panel = render_agents(
        [], loops=[_row(loop_id="L1", name="alive", status="active"),
                   _row(loop_id="L2", name="broken", status="paused")]
    )
    text = _render(panel)
    assert loop_glyph("paused") in text, f"paused glyph missing: {text!r}"
    assert "alive" in text and "broken" in text


def test_loop_still_excluded_from_p_slots(db):
    """loop-still-excluded-from-P-slots (P5a, 2026-07-05): adding the Loops band
    must NOT leak a loop's kind='loop' project into the P-slot project headers
    (exclusion enforced in the snapshot projects query)."""
    created = create_loop_atomic(
        db, template=_single_topic_template(), cadence="every 6h", name="issue-triage"
    )
    state = snapshot(db)
    assert created["project_id"] not in (state.projects_by_id or {}), (
        f"loop project leaked into P-slots: {state.projects_by_id!r}"
    )
    # but it IS present in the loops band snapshot field
    assert any(r.project_id == created["project_id"] for r in (state.loops or []))


def test_collapse_never_hides_a_broken_loop():
    """collapse-surfaces-attention-first (P5a review, 2026-07-05): a paused/failing
    loop must never be the row the collapse HIDES behind healthy older loops —
    the band orders attention-first (triage), mirroring the Agents-pane sort."""
    healthy = [_row(loop_id=f"L{i}", name=f"ok-{i:02d}", status="active",
                    consecutive_failures=0) for i in range(10)]
    # broken loop created LAST (would sort to the tail by created_at)
    broken = _row(loop_id="L99", name="broken-last", status="paused")
    lines = collapse_loop_lines(healthy + [broken])
    shown = lines[:-1]  # last line is the '+N more' summary
    assert any("broken-last" in ln for ln in shown), (
        f"broken loop was hidden by the collapse: {lines!r}"
    )


def test_many_loops_collapse_to_summary():
    """many-loops-collapse-to-summary (P5a §8, 2026-07-05): dozens of loops must
    NOT render one-row-each unconditionally — collapse to a summary at scale."""
    rows = [_row(loop_id=f"L{i}", name=f"loop-{i:02d}") for i in range(20)]
    lines = collapse_loop_lines(rows)
    assert len(lines) < len(rows), "20 loops should collapse to fewer rows"
    assert any("more" in ln for ln in lines), f"no summary row: {lines!r}"
    # end-to-end: the rendered band must not print all 20 names, and shows a summary
    text = _render(render_agents([], loops=rows), width=80)
    shown = sum(1 for i in range(20) if f"loop-{i:02d}" in text)
    assert shown < 20, f"all loops rendered (no collapse): {shown}"
    assert "more" in text, f"no summary in render: {text!r}"
