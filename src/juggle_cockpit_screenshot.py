"""Cockpit screenshot rendering (PNG/JPG/SVG via Rich record + cairosvg).

Extracted from juggle_cockpit.py's __main__ block to keep that module within
its LOC budget. Renders the four panes (or the graph panel, when graph_mode)
to a recorded Rich Console and saves an image. Read-only.
"""
from __future__ import annotations

import os
import subprocess
import sqlite3
import sys


def save_screenshot(path: str, db_path: str | None, *, graph_mode: bool = False) -> str:
    """Render the cockpit to ``path`` (PNG/JPG/SVG). Returns the written path.

    graph_mode: render the lower-right panel as the task-graph panel instead of
    Notifications (used for graph-viz screenshots).
    """
    from rich.console import Console
    from juggle_db import JuggleDB
    from juggle_cockpit_model import snapshot
    from juggle_cockpit_view import (
        render_topics, render_actions, render_agents, render_notifications,
    )

    db = JuggleDB(db_path=db_path)
    db.init_db()
    conn = sqlite3.connect(str(db.db_path))
    conn.row_factory = sqlite3.Row
    db._connect = lambda: conn  # noqa: E731
    try:
        state = snapshot(db, load_graph_dag=graph_mode)
    finally:
        conn.close()

    from juggle_cockpit_title import _get_version
    svg_title = f"Juggle Cockpit \u00b7 v{_get_version()}"

    con = Console(record=True, force_terminal=True, width=220, color_system="truecolor")
    con.print(render_topics(
        state.topics, "wide", state.projects_by_id,
        graph_by_project=getattr(state, "graph_by_project", None),
    ))
    con.print(render_actions(state.actions))
    con.print(render_agents(state.agents, state.scheduled))
    if graph_mode:
        from juggle_cockpit_graph_panel import build_graph_panel
        dag = getattr(state, "graph_dag", None)
        con.print(build_graph_panel(
            project_id=(dag.project_id if dag else None),
            tasks=(dag.tasks if dag else []),
            edges=(dag.edges if dag else []),
            selection=0, unread=0, width=80, height=20, pan_offset=0,
        ))
    else:
        con.print(render_notifications(state.notifications))

    ext = path.rsplit(".", 1)[-1].lower() if "." in path else "png"
    if ext == "svg":
        con.save_svg(path, title=svg_title)
        return path

    svg_path = path.rsplit(".", 1)[0] + ".svg"
    con.save_svg(svg_path, title=svg_title)
    try:
        result = subprocess.run(
            ["uv", "run", "--with", "cairosvg", "python3", "-c",
             f"import cairosvg; cairosvg.svg2png(url='file://{os.path.abspath(svg_path)}', "
             f"write_to='{path}', scale=2)"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            os.unlink(svg_path)
            return path
        print(f"PNG conversion failed, SVG saved to {svg_path}", file=sys.stderr)
        return svg_path
    except Exception as e:
        print(f"PNG conversion error: {e}, SVG at {svg_path}", file=sys.stderr)
        return svg_path
