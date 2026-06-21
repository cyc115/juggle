"""TDD tests for the graph panel Rich renderable builder.

build_graph_panel turns (GraphDag-ish input + selection + unread badge + width)
into a Rich Panel: header progress line, per-rank columns flowing rightward,
state glyphs, selection highlight, unread badge in the title, minimap when
panned, and the "no armed graph" path. Pure (Rich only).
"""
from __future__ import annotations

import io
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_cockpit_graph_layout import GraphTask  # noqa: E402
from juggle_cockpit_graph_panel import build_graph_panel  # noqa: E402
from juggle_cockpit_view import TASK_STATE_GLYPHS  # noqa: E402


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _text(panel, width=80) -> str:
    from rich.console import Console

    buf = io.StringIO()
    Console(width=width, file=buf, no_color=True, highlight=False).print(panel)
    # Console(no_color=True) strips color but still emits dim/bold style codes;
    # strip them so overflow assertions measure visible width, not raw bytes.
    return _ANSI_RE.sub("", buf.getvalue())


def _dag():
    tasks = [
        GraphTask("a", "Setup", "verified"),
        GraphTask("b", "Build", "running", thread_id="t1"),
        GraphTask("c", "Ship", "ready"),
    ]
    edges = [("b", "a"), ("c", "b")]
    return tasks, edges


# ---------------------------------------------------------------------------
# Header + progress
# ---------------------------------------------------------------------------


def test_header_shows_project_and_progress():
    tasks, edges = _dag()
    panel = build_graph_panel(
        project_id="oauth-login", tasks=tasks, edges=edges,
        selection=0, unread=0, width=80, height=20, pan_offset=0,
    )
    out = _text(panel)
    assert "oauth-login" in out
    assert "done" in out  # format_progress segment "1/3 done"


# ---------------------------------------------------------------------------
# Task glyphs from state
# ---------------------------------------------------------------------------


def test_task_glyphs_from_state():
    tasks, edges = _dag()
    panel = build_graph_panel(
        project_id="p", tasks=tasks, edges=edges,
        selection=0, unread=0, width=80, height=20, pan_offset=0,
    )
    out = _text(panel)
    assert TASK_STATE_GLYPHS["verified"] in out
    assert TASK_STATE_GLYPHS["running"] in out
    assert TASK_STATE_GLYPHS["ready"] in out


# ---------------------------------------------------------------------------
# Unread badge
# ---------------------------------------------------------------------------


def test_unread_badge_in_title():
    tasks, edges = _dag()
    panel = build_graph_panel(
        project_id="p", tasks=tasks, edges=edges,
        selection=0, unread=3, width=80, height=20, pan_offset=0,
    )
    assert "3" in str(panel.title)
    assert "Graph" in str(panel.title)


def test_no_badge_when_zero_unread():
    tasks, edges = _dag()
    panel = build_graph_panel(
        project_id="p", tasks=tasks, edges=edges,
        selection=0, unread=0, width=80, height=20, pan_offset=0,
    )
    title = str(panel.title)
    assert "Graph" in title
    # no "· N" badge segment
    assert "·" not in title or "0" not in title


# ---------------------------------------------------------------------------
# No project selected (P7: "no armed graph" replaced by "no project selected")
# ---------------------------------------------------------------------------


def test_no_project_selected_hint():
    """REGRESSION PIN (P7): project_id=None shows 'no project selected',
    NOT the old 'no armed graph' message."""
    panel = build_graph_panel(
        project_id=None, tasks=[], edges=[],
        selection=0, unread=0, width=80, height=20, pan_offset=0,
    )
    out = _text(panel)
    assert "armed" not in out.lower(), "P7: 'armed' must not appear in panel"
    assert "no project" in out.lower() or "selected" in out.lower()


def test_armed_but_empty_graph_hint():
    panel = build_graph_panel(
        project_id="p", tasks=[], edges=[],
        selection=0, unread=0, width=80, height=20, pan_offset=0,
    )
    out = _text(panel)
    assert "no" in out.lower() and ("task" in out.lower() or "graph" in out.lower())


# ---------------------------------------------------------------------------
# Narrow width never overflows
# ---------------------------------------------------------------------------


def test_narrow_width_no_overflow():
    tasks = [GraphTask(f"n{i}", f"Task {i}", "verified" if i < 8 else "ready") for i in range(10)]
    edges = [(f"n{i}", f"n{i-1}") for i in range(1, 10)]
    panel = build_graph_panel(
        project_id="p", tasks=tasks, edges=edges,
        selection=0, unread=0, width=40, height=20, pan_offset=0,
    )
    out = _text(panel, width=40)
    for line in out.splitlines():
        assert len(line) <= 40, f"overflow: {len(line)} > 40: {line!r}"


def test_long_task_name_keeps_id_badge_visible():
    """2026-06-16 (user feedback + screenshot): the [thread/topic id] badge must
    survive task-name truncation. It used to render as an end-suffix and got
    ellipsized away on a long task name; it now renders BEFORE the (possibly
    truncated) name so the id stays visible."""
    long_id = "reconcile-orphan-integrating-and-then-some-more-name"
    tasks = [GraphTask(long_id, "Long", "running",
                       thread_id="abcd1234", user_label="WK")]
    panel = build_graph_panel(
        project_id="P1", tasks=tasks, edges=[],
        selection=0, unread=0, width=28, height=20, pan_offset=0,
    )
    out = _text(panel, width=28)
    assert "[WK]" in out, f"id badge lost on truncation:\n{out}"


# ── Task-progress cell suffix + multi-DAG stacking (R5, 2026-06-11) ──────────

def test_topic_cell_shows_task_progress():
    from juggle_cockpit_graph_panel import build_graph_panel

    tasks = [GraphTask(id="auth", state="running", title="auth",
                       tasks_done=2, tasks_total=6)]
    panel = build_graph_panel(
        project_id="P1", tasks=tasks, edges=[],
        selection=0, unread=0, width=80, height=20, pan_offset=0,
    )
    out = _text(panel)
    assert "2/6" in out


def test_header_shows_project_name():
    """T-cockpit-graph-pane-ux #1: section header renders '<id> · <name>',
    not just the bare project id."""
    from juggle_cockpit_graph_dag import GraphDag
    from juggle_cockpit_graph_panel import build_multi_graph_panel

    dags = [
        GraphDag(project_id="P1", tasks=[GraphTask("a", "A", "verified")],
                 edges=[], member_tasks={}, project_name="Trading Edge"),
        GraphDag(project_id="P2", tasks=[GraphTask("b", "B", "ready")],
                 edges=[], member_tasks={}, project_name="Juggle Claude Code Plugin"),
    ]
    panel = build_multi_graph_panel(
        dags=dags, selection=0, unread=0, width=120, height=40, pan_offset=0
    )
    out = _text(panel, width=120)
    assert "Juggle Claude Code Plugin" in out
    assert "P2" in out
    # done/running counts still rendered after the name.
    assert "done" in out


def test_single_header_shows_project_name():
    tasks, edges = _dag()
    from juggle_cockpit_graph_panel import build_graph_panel

    panel = build_graph_panel(
        project_id="P2", project_name="Juggle Claude Code Plugin",
        tasks=tasks, edges=edges, selection=0, unread=0,
        width=120, height=20, pan_offset=0,
    )
    out = _text(panel, width=120)
    assert "Juggle Claude Code Plugin" in out
    assert "done" in out


def test_long_project_name_truncated_to_width():
    tasks, edges = _dag()
    from juggle_cockpit_graph_panel import build_graph_panel

    panel = build_graph_panel(
        project_id="P2", project_name="X" * 200,
        tasks=tasks, edges=edges, selection=0, unread=0,
        width=60, height=20, pan_offset=0,
    )
    out = _text(panel, width=60)
    for line in out.splitlines():
        assert len(line) <= 60, f"overflow: {len(line)} > 60: {line!r}"
    assert "…" in out  # name was ellipsised


def test_multi_panel_stacks_each_armed_dag_with_header():
    """REGRESSION PIN (2026-06-11): graph panel rendered only the first armed
    DAG — with two dags both project headers must render, P1 before P2."""
    from juggle_cockpit_graph_dag import GraphDag
    from juggle_cockpit_graph_panel import build_multi_graph_panel

    tasks1 = [GraphTask("a", "A", "verified")]
    tasks2 = [GraphTask("b", "B", "ready")]
    dags = [
        GraphDag(project_id="P1", tasks=tasks1, edges=[], member_tasks={}),
        GraphDag(project_id="P2", tasks=tasks2, edges=[], member_tasks={}),
    ]
    panel = build_multi_graph_panel(
        dags=dags, selection=0, unread=0, width=80, height=30, pan_offset=0
    )
    out = _text(panel)
    p1_pos = out.find("P1")
    p2_pos = out.find("P2")
    assert p1_pos != -1 and p2_pos != -1 and p1_pos < p2_pos


# ── Scrollable graph viewport (T-cockpit-graph-pane-ux #2) ───────────────────


def _armed_many_db(tmp_path, n=120, projects=("P",)):
    """Armed DB with many graph tasks so the graph pane overflows its viewport."""
    from juggle_db import JuggleDB
    from dbops import db_graph as g
    from juggle_graph_dispatch import ARMED_PROJECT_KEY
    from datetime import datetime, timezone

    db_path = str(tmp_path / "juggle.db")
    db = JuggleDB(db_path=db_path)
    db.init_db()
    db.set_active(True)
    now = datetime.now(timezone.utc).isoformat()
    with db._connect() as conn:
        for pid in projects:
            conn.execute(
                "INSERT INTO projects(id,name,status,created_at,last_active) "
                "VALUES(?,?,?,?,?)",
                (pid, "Juggle Claude Code Plugin", "active", now, now),
            )
        conn.commit()
    for pid in projects:
        for i in range(n):
            g.create_task(db, task_id=f"{pid}-n{i}", project_id=pid,
                          title=f"Task {i}", prompt="x")
    db.set_setting(ARMED_PROJECT_KEY, ",".join(projects))
    return db_path


@pytest.mark.asyncio
async def test_graph_pane_is_scrollable_when_overflowing(tmp_path):
    from juggle_cockpit import CockpitApp
    from textual.containers import VerticalScroll

    app = CockpitApp(db_path=_armed_many_db(tmp_path, n=120))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("g")
        await pilot.pause(0.2)
        sc = app.query_one("#graph-scroll", VerticalScroll)
        assert sc.max_scroll_y > 0, "tall graph should overflow the viewport"


@pytest.mark.asyncio
async def test_pagedown_and_j_move_the_viewport(tmp_path):
    from juggle_cockpit import CockpitApp
    from textual.containers import VerticalScroll

    app = CockpitApp(db_path=_armed_many_db(tmp_path, n=120))
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("g")
        await pilot.pause(0.2)
        sc = app.query_one("#graph-scroll", VerticalScroll)
        before = sc.scroll_offset.y
        await pilot.press("pagedown")
        await pilot.pause(0.2)
        after_pgdn = sc.scroll_offset.y
        assert after_pgdn > before, "PageDown should advance the viewport"
        await pilot.press("j")
        await pilot.pause(0.2)
        assert sc.scroll_offset.y > after_pgdn, "j should advance the viewport"


@pytest.mark.asyncio
async def test_graph_header_shows_project_name_in_app(tmp_path):
    from juggle_cockpit import CockpitApp

    app = CockpitApp(db_path=_armed_many_db(tmp_path, n=4))
    async with app.run_test(size=(160, 30)) as pilot:
        await pilot.press("g")
        await pilot.pause(0.2)
        # End-to-end: the armed project's name flows through GraphDag into
        # the rendered graph pane header.
        from textual.widgets import Static
        from rich.console import Console

        panel = app.query_one("#graph-body", Static).render()._renderable
        buf = io.StringIO()
        Console(width=158, file=buf, no_color=True).print(panel)
        assert "Juggle Claude Code Plugin" in buf.getvalue()
