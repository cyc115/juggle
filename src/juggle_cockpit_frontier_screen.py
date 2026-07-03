"""juggle_cockpit_frontier_screen — the Frontier Railroad (fr-screen,
2026-07-02). ONE full-screen graph surface bound to both ``l`` (in Graph
mode) and ``G`` (muscle memory), replacing the removed GitlogScreen and
RailroadScreen. Rendering is delegated to juggle_frontier_render (pure text)
over juggle_frontier_layout.build_frontier_layout; this module owns only
compose/bindings/actions — cursor nav, topic drill-in (Enter), wave fold
('z'), project cycling (Tab), and the bottom node-detail pane
(juggle_cockpit_node_detail.node_detail_text, preserved from the removed
RailroadScreen)."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from juggle_cockpit_graph_layout import GraphTask
from juggle_cockpit_node_detail import node_detail_text
from juggle_frontier_layout import build_frontier_layout
from juggle_frontier_render import render_critical_path_footer, render_rows

_SELECTABLE_KINDS = ("running", "ready", "blocked", "failed")


def _task_edges_among(db, ids: set) -> list[tuple[str, str]]:
    """Dependency edges (task_id, depends_on_id) restricted to ``ids`` — the
    task-tier equivalent of juggle_cockpit_graph_dag's topic-edge query, used
    for topic drill-in (member tasks rendered in the same railroad grammar)."""
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    with db._connect() as conn:
        rows = conn.execute(
            f"SELECT node_id, depends_on_id FROM node_edges WHERE kind='dep' "
            f"AND node_id IN ({ph}) AND depends_on_id IN ({ph})",
            (*ids, *ids),
        ).fetchall()
    return [(r["node_id"], r["depends_on_id"]) for r in rows]


def _free_agent_slots(db) -> int:
    from juggle_cockpit_model import snapshot as _snapshot
    from juggle_settings import resolve_max_agents

    try:
        state = _snapshot(db)
        busy = sum(1 for a in state.agents if a.status == "busy")
    except Exception:
        busy = 0
    return max(0, resolve_max_agents() - busy)


class FrontierScreen(Screen):
    """Full-screen top-down railroad for ONE project's frontier (topics, or
    a drilled-in topic's member tasks)."""

    BINDINGS = [
        Binding("j", "cursor_down", "↓", show=False),
        Binding("k", "cursor_up", "↑", show=False),
        Binding("down", "cursor_down", "↓", show=False),
        Binding("up", "cursor_up", "↑", show=False),
        Binding("enter", "activate", "Open"),
        Binding("tab", "next_project", "Proj"),
        Binding("z", "toggle_fold", "Fold", show=False),
        Binding("escape", "back_or_close", "Back", show=False),
        Binding("q", "close", "Close"),
    ]

    def __init__(self, dags: list, start_project: str, db) -> None:
        super().__init__()
        self._dags = dags
        self._db = db
        self._pids = [d.project_id for d in dags]
        self._pi = self._pids.index(start_project) if start_project in self._pids else 0
        self._sel = 0
        self._folded: set[int] = set()
        self._drill_topic_id: "str | None" = None
        self._layout = None  # set by _rebuild; read by tests

    @property
    def _dag(self):
        return self._dags[self._pi]

    def _tier_tasks_edges(self):
        if self._drill_topic_id is None:
            return self._dag.tasks, self._dag.edges
        member = (self._dag.member_tasks or {}).get(self._drill_topic_id, [])
        tasks = [GraphTask(id=m["id"], title=m["title"], state=m["state"]) for m in member]
        edges = _task_edges_among(self._db, {t.id for t in tasks})
        return tasks, edges

    def _selectable(self, layout) -> list:
        return [r for r in layout.rows if r.kind in _SELECTABLE_KINDS]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="frontier-rows"):
            yield Static("Frontier Railroad", id="frontier-body", markup=False)
        yield Static("(loading)", id="frontier-detail", markup=False)

    def on_mount(self) -> None:
        self._rebuild()

    def _rebuild(self) -> None:
        tasks, edges = self._tier_tasks_edges()
        free_slots = _free_agent_slots(self._db)
        layout = build_frontier_layout(tasks, edges, free_slots, folded_waves=self._folded)
        self._layout = layout
        rows = self._selectable(layout)
        if rows:
            self._sel = max(0, min(self._sel, len(rows) - 1))
        selected_id = rows[self._sel].id if rows else None
        titles = {t.id: t.title for t in tasks}

        header = f"Frontier Railroad — {self._dag.project_name or self._dag.project_id}"
        if self._drill_topic_id:
            header += f" / {self._drill_topic_id}"
        body_lines = render_rows(layout, selected_id=selected_id)
        footer = render_critical_path_footer(layout, titles)
        self._body_text = "\n".join([header, "─" * 40, *body_lines, "", footer])
        self.query_one("#frontier-body", Static).update(self._body_text)

        self._detail_text = node_detail_text(self._db, selected_id) if selected_id else "(no tasks)"
        self.query_one("#frontier-detail", Static).update(self._detail_text)

    def action_cursor_down(self) -> None:
        self._sel += 1
        self._rebuild()

    def action_cursor_up(self) -> None:
        self._sel = max(0, self._sel - 1)
        self._rebuild()

    def action_next_project(self) -> None:
        if len(self._dags) > 1:
            self._pi = (self._pi + 1) % len(self._dags)
            self._sel = 0
            self._drill_topic_id = None
            self._folded = set()
            self._rebuild()

    def action_toggle_fold(self) -> None:
        rows = self._selectable(self._layout) if self._layout else []
        if not (0 <= self._sel < len(rows)):
            return
        wave = rows[self._sel].wave
        self._folded ^= {wave}
        self._rebuild()

    def action_activate(self) -> None:
        """Enter — drill into a topic's member tasks, or open the full detail
        modal for an already-drilled-in task."""
        rows = self._selectable(self._layout) if self._layout else []
        if not (0 <= self._sel < len(rows)):
            return
        node_id = rows[self._sel].id
        if self._drill_topic_id is None:
            member = (self._dag.member_tasks or {}).get(node_id)
            if member:
                self._drill_topic_id = node_id
                self._sel = 0
                self._folded = set()
                self._rebuild()
            return
        from dbops import db_graph
        from dbops.db_graph_edges import get_deps
        from juggle_cockpit_modal_node import _NodeDetailModal

        task = db_graph.get_task(self._db, node_id)
        if not task:
            return
        try:
            deps = get_deps(self._db, node_id)
        except Exception:
            deps = []
        self.app.push_screen(_NodeDetailModal(task, deps, is_topic=False))

    def action_back_or_close(self) -> None:
        if self._drill_topic_id is not None:
            self._drill_topic_id = None
            self._sel = 0
            self._folded = set()
            self._rebuild()
            return
        self.dismiss()

    def action_close(self) -> None:
        self.dismiss()
