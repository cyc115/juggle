"""juggle_cockpit_gitlog_screen — Surface-E full-screen git-log graph view
(cockpit-gitlog-view, 2026-07-02). A Textual ``Screen`` toggled from graph mode
via the ``l`` key (same key closes) that renders the COMPLETE task graph across
every project as a vertical, newest-at-top ``git log --graph`` (via the pure
``juggle_cockpit_gitlog_lines`` core). j/k scroll the cursor, Enter opens the
full node-detail modal, q/Esc/l close back to the dashboard.
"""
from __future__ import annotations

from rich.console import Group
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static


class GitlogScreen(Screen):
    """Full-screen git-log view of every project's task graph (Surface E)."""

    BINDINGS = [
        Binding("j", "cursor_down", "↓", show=False),
        Binding("k", "cursor_up", "↑", show=False),
        Binding("down", "cursor_down", "↓", show=False),
        Binding("up", "cursor_up", "↑", show=False),
        Binding("enter", "open_detail", "Open"),
        Binding("l", "close", "Close", show=False),
        Binding("q", "close", "Close"),
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(self, dags: list, db) -> None:
        super().__init__()
        self._dags = dags
        self._db = db
        self._sel = 0

    def _rows(self):
        from juggle_cockpit_gitlog_lines import gitlog_rows
        from juggle_cockpit_gitlog_meta import gitlog_meta_for

        ids = [n.id for d in self._dags for n in d.tasks
               if not getattr(n, "is_mirror", False)]
        meta = gitlog_meta_for(self._db, ids)
        return gitlog_rows(self._dags, selected_row=self._sel, meta_by_id=meta)

    def compose(self) -> ComposeResult:
        # _rebuild() (on_mount) overwrites this placeholder. NB: not named
        # _render — that shadows a reserved Textual Widget internal.
        with VerticalScroll(id="gitlog-rows"):
            yield Static("Git Log", id="gitlog-body", markup=False)
        yield Static("", id="gitlog-footer", markup=False)

    def on_mount(self) -> None:
        self._rebuild()

    def _rebuild(self) -> None:
        from juggle_cockpit_gitlog_lines import render_gitlog_line
        from juggle_cockpit_legend import graph_inline_legend

        rows = self._rows()
        if rows:
            self._sel = max(0, min(self._sel, len(rows) - 1))
        idx_w = max(3, len(str(max((r.idx for r in rows), default=0))))
        lines = [render_gitlog_line(r, idx_w=idx_w) for r in rows]
        self.query_one("#gitlog-body", Static).update(
            Group(*lines) if lines else "(no tasks)"
        )
        legend = graph_inline_legend()
        self.query_one("#gitlog-footer", Static).update(
            f"{legend}\nl/Esc/q close   ·   j k / ↑ ↓ scroll   ·   Enter open"
        )

    def action_cursor_down(self) -> None:
        self._sel += 1
        self._rebuild()

    def action_cursor_up(self) -> None:
        self._sel = max(0, self._sel - 1)
        self._rebuild()

    def action_open_detail(self) -> None:
        """Enter — open the read-only detail modal for the selected node."""
        rows = self._rows()
        if not (0 <= self._sel < len(rows)):
            return
        node_id = rows[self._sel].id
        from dbops import db_graph as _g
        from dbops import db_topics as _t
        from juggle_cockpit_modals import _NodeDetailModal, build_summary_ctx

        task_row = _g.get_task(self._db, node_id)
        is_topic = task_row is None
        full = task_row or _t.get_topic(self._db, node_id) or {}
        if not full:
            return
        try:
            deps = _g.get_deps(self._db, node_id)
        except Exception:
            deps = [d for dag in self._dags for (nid, d) in dag.edges if nid == node_id]
        if is_topic:
            ctx = build_summary_ctx(self._db, full.get("thread_id"))
            if not ctx.get("task_input") and (full.get("objective") or "").strip():
                ctx["task_input"] = full["objective"].strip()
            self.app.push_screen(_NodeDetailModal(
                full, deps, is_topic=True, summary_ctx=ctx, label=full.get("id", node_id),
            ))
        else:
            self.app.push_screen(_NodeDetailModal(full, deps, is_topic=False))

    def action_close(self) -> None:
        self.dismiss()
