"""Juggle Cockpit Static — text renders of the cockpit without a TUI.

Owns: render_static_from_state (pure compose of the four panes into plain
text) and render_static (DB snapshot + compose) used by ``cockpit --out``
and CI smoke checks.
Must not own: pane render functions (juggle_cockpit_view) or the snapshot
reader (juggle_cockpit_model).
"""

from __future__ import annotations

from juggle_cockpit_model import CockpitState
from juggle_cockpit_view import (
    render_actions,
    render_agents,
    render_notifications,
    render_topics,
)


def render_static_from_state(state: CockpitState, width: int = 120) -> str:
    """Render all four cockpit panes as plain text from a CockpitState.

    Mirrors the TUI 2D layout:
      Left column  : Topics (full height)
      Right top    : Actions + Agents side by side
      Right bottom : Notifications (full width of right)

    No DB I/O. Suitable for unit tests and CI smoke checks.
    """
    import io
    from rich.console import Console

    def _render(renderable, w: int) -> list[str]:
        """Render a Rich renderable into lines at width w without touching stdout."""
        buf = io.StringIO()
        con = Console(width=w, file=buf, no_color=True, highlight=False)
        con.print(renderable)
        return buf.getvalue().splitlines()

    left_w = width // 3
    right_w = width - left_w
    half_right = right_w // 2

    left_lines = _render(
        render_topics(
            state.topics,
            "wide",
            state.projects_by_id,
            graph_by_project=getattr(state, "graph_by_project", None),
        ),
        left_w,
    )
    actions_lines = _render(render_actions(state.actions), half_right)
    agents_lines = _render(
        render_agents(state.agents, state.scheduled), right_w - half_right
    )
    notif_lines = _render(render_notifications(state.notifications), right_w)

    # --- compose the 2D grid into lines ----------------------------------
    right_top_rows = max(len(actions_lines), len(agents_lines))
    total_rows = max(len(left_lines), right_top_rows + len(notif_lines))

    def _pad(lines: list[str], n: int, w: int) -> list[str]:
        padded = [ln.ljust(w)[:w] for ln in lines]
        padded += [" " * w] * (n - len(padded))
        return padded

    left_padded = _pad(left_lines, total_rows, left_w)
    actions_padded = _pad(actions_lines, right_top_rows, half_right)
    agents_padded = _pad(agents_lines, right_top_rows, right_w - half_right)
    notif_padded = _pad(notif_lines, len(notif_lines), right_w)

    right_padded: list[str] = []
    for i in range(right_top_rows):
        right_padded.append(actions_padded[i] + agents_padded[i])
    right_padded.extend(notif_padded)
    right_padded = _pad(right_padded, total_rows, right_w)

    output_lines = [lt + rt for lt, rt in zip(left_padded, right_padded)]
    return "\n".join(output_lines) + "\n"


def render_static(db_path: str | None = None, width: int = 120) -> str:
    """Snapshot the live juggle.db and render all four cockpit panes as plain text.

    Creates its own DB connection (does not reuse an existing one). Suitable for
    the ``--out`` CLI flag and CI health checks.
    """
    import sqlite3 as _sqlite3
    import sys as _sys
    from pathlib import Path as _Path

    _src = _Path(__file__).parent
    if str(_src) not in _sys.path:
        _sys.path.insert(0, str(_src))

    from juggle_cockpit_model import snapshot as _snapshot
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=db_path)
    db.init_db()
    conn = _sqlite3.connect(str(db.db_path))
    conn.row_factory = _sqlite3.Row
    db._connect = lambda: conn  # noqa: E731
    try:
        state = _snapshot(db)
        return render_static_from_state(state, width)
    finally:
        conn.close()
