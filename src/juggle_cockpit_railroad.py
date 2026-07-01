"""juggle_cockpit_railroad — Surface-B full-screen railroad Screen (2026-06-30
graph railroad, T5). A Textual ``Screen`` bound to ``shift+g`` that renders one
project's task DAG as a vertical git-graph (via the pure ``railroad_lines`` core)
with a moving cursor (j/k) and a bottom node-detail pane. ``node_detail_text``
assembles the same structured fields the ``_NodeDetailModal`` shows for a task
node, plus a run/token rollup from the agent_runs ledger. Tab cycles projects,
Enter opens the full detail modal, q/Esc pops back to the dashboard."""
from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Static


def node_detail_text(db, task_id: str) -> str:
    """Structured detail text for a task node — id / title / state / deps /
    thread / verify, a run+token rollup, and prompt/handoff excerpts. Mirrors
    ``_NodeDetailModal._field_lines`` (task branch); PURE string assembly."""
    from dbops import db_graph
    from dbops.db_graph_edges import get_deps

    task = db_graph.get_task(db, task_id) or {}
    try:
        deps = get_deps(db, task_id)
    except Exception:
        deps = []
    lines = [
        f"Task {task.get('id', task_id)}",
        "─" * 40,
        f"title    {task.get('title', '')}",
        f"state    {task.get('state', '')}",
        f"deps     {', '.join(deps) if deps else '(none)'}",
        f"thread   {task.get('thread_id') or '(unbound)'}",
        f"verify   {task.get('verify_cmd') or '(none)'}",
    ]
    try:
        runs = db.get_runs(task_id=task_id)
    except Exception:
        runs = []
    if runs:
        toks = sum((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in runs)
        lines += ["", f"runs     {len(runs)} ({toks} tok)"]
    prompt = (task.get("prompt") or "").strip()
    if prompt:
        lines += ["", "prompt:", prompt[:400]]
    handoff = (task.get("handoff") or "").strip()
    if handoff:
        lines += ["", "handoff:", handoff[:400]]
    return "\n".join(lines)


class RailroadScreen(Screen):
    """Full-screen vertical railroad for ONE project's DAG (Surface B)."""

    BINDINGS = [
        Binding("j", "cursor_down", "↓", show=False),
        Binding("k", "cursor_up", "↑", show=False),
        Binding("down", "cursor_down", "↓", show=False),
        Binding("up", "cursor_up", "↑", show=False),
        Binding("enter", "jump_topic", "Open"),
        Binding("tab", "next_project", "Proj"),
        Binding("f", "filter", "Filter", show=False),
        Binding("q", "close", "Close"),
        Binding("escape", "close", "Close", show=False),
    ]

    def __init__(self, dags: list, start_project: str, db) -> None:
        super().__init__()
        self._dags = dags
        self._db = db
        self._pids = [d.project_id for d in dags]
        self._pi = self._pids.index(start_project) if start_project in self._pids else 0
        self._sel = 0

    @property
    def _dag(self):
        return self._dags[self._pi]

    def _lines(self) -> list:
        from juggle_cockpit_graph_lanes import assign_lanes
        from juggle_cockpit_railroad_lines import railroad_lines

        layout = assign_lanes(self._dag.tasks, self._dag.edges)
        return railroad_lines(layout, self._dag.tasks, selected_row=self._sel)

    def compose(self) -> ComposeResult:
        # Seed placeholder content; _rebuild() (on_mount) overwrites both. NB the
        # rebuild hook is NOT named _render — that is a reserved Textual Widget
        # internal (must return a renderable); shadowing it renders a None visual
        # → compositor crash.
        with VerticalScroll(id="rail-rows"):
            yield Static("Railroad", id="rail-body", markup=False)
        yield Static("(loading)", id="rail-detail", markup=False)

    def on_mount(self) -> None:
        self._rebuild()

    def _rebuild(self) -> None:
        lines = self._lines()
        if lines:
            self._sel = max(0, min(self._sel, len(lines) - 1))
        header = f"Railroad — {self._dag.project_name or self._dag.project_id}"
        body = [header, "─" * 40]
        for ln in lines:
            cursor = "▶" if ln.selected else " "
            body.append(f"{cursor}{ln.rail} {ln.glyph} {ln.id}  {ln.title}")
        self._body_text = "\n".join(body)
        self.query_one("#rail-body", Static).update(self._body_text)
        sel_id = lines[self._sel].id if lines else None
        self._detail_text = node_detail_text(self._db, sel_id) if sel_id else "(no tasks)"
        self.query_one("#rail-detail", Static).update(self._detail_text)

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
            self._rebuild()

    def action_jump_topic(self) -> None:
        """Enter — open the full read-only detail modal for the selected task."""
        lines = self._lines()
        if not lines:
            return
        tid = lines[self._sel].id
        from dbops import db_graph
        from dbops.db_graph_edges import get_deps

        task = db_graph.get_task(self._db, tid)
        if not task:
            return
        try:
            deps = get_deps(self._db, tid)
        except Exception:
            deps = []
        from juggle_cockpit_modal_node import _NodeDetailModal

        self.app.push_screen(_NodeDetailModal(task, deps, is_topic=False))

    def action_filter(self) -> None:  # placeholder — no in-screen filter yet
        pass

    def action_close(self) -> None:
        self.dismiss()
