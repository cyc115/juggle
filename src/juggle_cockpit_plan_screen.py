"""juggle_cockpit_plan_screen — the full-screen Plan view (cockpit-plan-view,
2026-07-02). Bound to ``l`` in Graph mode (the old Frontier railroad moves to
``L``). Renders juggle_plan_layout's layered future DAG as dependency-wave
COLUMNS via juggle_plan_render; this module owns only compose/bindings/actions:
cursor nav across cards, selection auto-expanding member tasks in place, Enter
opening the selected topic's detail modal, ``z`` folding overflow waves,
``←/→`` paging wave columns, and ``Tab`` cycling projects. Pattern-matches the
FrontierScreen structure; card-render helpers live in juggle_plan_render so this
stays a thin controller (architecture LOC gate)."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static

from juggle_cockpit_frontier_screen import _free_agent_slots, _running_meta
from juggle_plan_layout import build_plan_layout
from juggle_plan_render import render_plan_body, render_plan_legend

# When folded ('z'), cap total visible open cards; trailing whole waves collapse
# into the "…N more open nodes, z expands" summary (never wave 0).
FOLD_MAX_TOPICS = 8


class PlanScreen(Screen):
    """Full-screen layered future-DAG Plan view for ONE project."""

    DEFAULT_CSS = """
    PlanScreen #plan-rows { height: 1fr; }
    PlanScreen #plan-legend { dock: bottom; height: auto; }
    """

    BINDINGS = [
        Binding("j", "cursor_down", "↓", show=False),
        Binding("k", "cursor_up", "↑", show=False),
        Binding("down", "cursor_down", "↓", show=False),
        Binding("up", "cursor_up", "↑", show=False),
        Binding("right", "pan_right", "→", show=False),
        Binding("left", "pan_left", "←", show=False),
        Binding("enter", "activate", "Open"),
        Binding("z", "toggle_fold", "Fold", show=False),
        Binding("tab", "next_project", "Proj"),
        Binding("escape", "close", "Back", show=False),
        Binding("q", "close", "Close"),
    ]

    def __init__(self, dags: list, start_project: str, db) -> None:
        super().__init__()
        self._dags = dags
        self._db = db
        self._pids = [d.project_id for d in dags]
        self._pi = self._pids.index(start_project) if start_project in self._pids else 0
        self._sel = 0
        self._pan = 0
        self._folded = False
        self._layout = None  # set by _rebuild; read by tests
        self._body_text = ""

    @property
    def _dag(self):
        return self._dags[self._pi]

    def _selectable(self) -> list:
        if not self._layout:
            return []
        return [c for w in self._layout.waves for c in w.cards]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="plan-rows"):
            yield Static("Plan view", id="plan-body", markup=True)
        yield Static(render_plan_legend(), id="plan-legend", markup=True)

    def on_mount(self) -> None:
        self._rebuild()

    def on_resize(self, event) -> None:
        self._rebuild()

    def _rebuild(self) -> None:
        dag = self._dag
        free_slots = _free_agent_slots(self._db)
        mv = FOLD_MAX_TOPICS if self._folded else None
        layout = build_plan_layout(dag.tasks, dag.edges, free_slots, max_visible_topics=mv)
        self._layout = layout
        cards = self._selectable()
        if cards:
            self._sel = max(0, min(self._sel, len(cards) - 1))
        self._pan = max(0, min(self._pan, max(0, len(layout.waves) - 1)))
        selected_id = cards[self._sel].id if cards else None
        running_ids = [c.id for c in cards if c.kind == "running"]
        meta = _running_meta(self._db, running_ids)
        titles = {t.id: t.title for t in dag.tasks}
        width = self.size.width or None
        body = render_plan_body(
            layout, selected_id=selected_id, meta_by_id=meta,
            member_tasks=dag.member_tasks, titles=titles, pan=self._pan,
            width=width, project_header=dag.project_name or dag.project_id,
        )
        self._body_text = "\n".join(body)
        self.query_one("#plan-body", Static).update(self._body_text)

    # -- nav ------------------------------------------------------------------

    def action_cursor_down(self) -> None:
        self._sel += 1
        self._rebuild()

    def action_cursor_up(self) -> None:
        self._sel = max(0, self._sel - 1)
        self._rebuild()

    def action_pan_right(self) -> None:
        self._pan += 1
        self._rebuild()

    def action_pan_left(self) -> None:
        self._pan = max(0, self._pan - 1)
        self._rebuild()

    def action_toggle_fold(self) -> None:
        self._folded = not self._folded
        self._rebuild()

    def action_next_project(self) -> None:
        if len(self._dags) > 1:
            self._pi = (self._pi + 1) % len(self._dags)
            self._sel = 0
            self._pan = 0
            self._folded = False
            self._rebuild()

    def action_activate(self) -> None:
        """Enter — open the selected topic's read-only detail modal."""
        cards = self._selectable()
        if not (0 <= self._sel < len(cards)):
            return
        node_id = cards[self._sel].id
        from dbops import db_topics as _t
        from dbops.db_graph_edges import get_deps
        from juggle_cockpit_modal_node import _NodeDetailModal

        node = _t.get_topic(self._db, node_id) or {"id": node_id}
        try:
            deps = get_deps(self._db, node_id)
        except Exception:
            deps = []
        members = (self._dag.member_tasks or {}).get(node_id, [])
        self.app.push_screen(_NodeDetailModal(
            node, deps, is_topic=True, tasks=members, label=node_id,
        ))

    def action_close(self) -> None:
        self.dismiss()
