"""Graph-mode controller mixin for the cockpit App.

Owns the lower-right panel's Notifications ⇄ Graph toggle, task selection,
horizontal pan, the unread badge accounting, the detail-modal launch, and the
in-graph key capture. Extracted from juggle_cockpit.py to keep that module
within its LOC budget. Read-only — never writes the DB.

Mixed into CockpitApp; relies on these attributes existing on self:
``_db``, ``_graph_mode``, ``_graph_sel``, ``_graph_pan``, ``_graph_unread``,
``_graph_unread_seen`` (initialised via ``_graph_state_init``), plus Textual's
``query_one``/``push_screen``/``screen_stack`` and the app's ``_refresh``.
"""
from __future__ import annotations

from juggle_cockpit_modals import _NodeDetailModal, build_summary_ctx


class GraphModeMixin:
    """Provides graph-mode behaviour to CockpitApp."""

    def _graph_state_init(self) -> None:
        """Initialise graph view-state. Call from __init__."""
        self._graph_mode = False
        self._graph_sel = 0          # selected task index (rank-major, id order)
        self._graph_pan = 0          # horizontal rank pan offset
        self._graph_unread = 0       # notifications landed while graph shown
        self._graph_unread_seen: set[str] = set()  # notif texts at enter-time

    # -- unread badge ---------------------------------------------------------

    def _graph_update_unread(self, state) -> None:
        """Recompute the unread badge from the current snapshot (text-based so
        it's stable across the per-tick age refresh)."""
        if not self._graph_mode:
            return
        self._graph_unread = sum(
            1 for n in state.notifications if n.text not in self._graph_unread_seen
        )

    # -- render ---------------------------------------------------------------

    def _render_graph_panel(self, state):
        """Build the graph Panel from the snapshot's lazily-loaded DAGs."""
        from juggle_cockpit_graph_panel import build_multi_graph_panel

        dags = getattr(state, "graph_dags", None) or (
            [state.graph_dag] if getattr(state, "graph_dag", None) else []
        )
        try:
            # The viewport (#graph-scroll) reserves a column for its scrollbar;
            # trim width so the grid never spills into a horizontal scroll.
            w = (self.query_one("#graph-scroll").size.width or 80) - 2
            h = self.query_one("#graph-scroll").size.height or 20
        except Exception:
            w, h = 80, 20
        total_tasks = sum(len(d.tasks) for d in dags)
        self._graph_sel = min(self._graph_sel, max(0, total_tasks - 1))
        return build_multi_graph_panel(
            dags=dags,
            selection=self._graph_sel,
            unread=self._graph_unread,
            width=max(8, w), height=h, pan_offset=self._graph_pan,
            scroll=True,
        )

    # -- toggle + nav ---------------------------------------------------------

    def action_toggle_graph(self) -> None:
        """g — toggle the lower-right panel between Notifications and Graph."""
        self._graph_mode = not self._graph_mode
        if self._graph_mode:
            from juggle_cockpit_model import snapshot as _snapshot
            try:
                st = _snapshot(self._db)
                self._graph_unread_seen = {n.text for n in st.notifications}
            except Exception:
                self._graph_unread_seen = set()
            self._graph_unread = 0
            self._graph_sel = 0
            self._graph_pan = 0
        self._refresh()
        if self._graph_mode:
            # Focus the viewport so the mouse wheel and scroll keys target it.
            try:
                self.query_one("#graph-scroll").focus()
            except Exception:
                pass

    def _graph_select(self, delta: int) -> None:
        self._graph_sel = max(0, self._graph_sel + delta)
        self._refresh()

    def _graph_pan_by(self, delta: int) -> None:
        self._graph_pan = max(0, self._graph_pan + delta)
        self._refresh()

    def _graph_scroll_by(self, key: str) -> bool:
        """Scroll the graph viewport. Returns True if the viewport was found."""
        try:
            sc = self.query_one("#graph-scroll")
        except Exception:
            return False
        if key == "j":
            sc.scroll_down(animate=False)
        elif key == "k":
            sc.scroll_up(animate=False)
        elif key == "pagedown":
            sc.scroll_page_down(animate=False)
        elif key == "pageup":
            sc.scroll_page_up(animate=False)
        return True

    def _open_graph_task_modal(self) -> None:
        """Enter — open the read-only detail modal for the selected topic/task."""
        from juggle_cockpit_model import snapshot as _snapshot
        from dbops import db_graph as _g
        from dbops import db_topics as _t

        try:
            state = _snapshot(self._db, load_graph_dag=True)
        except Exception:
            return
        dags = getattr(state, "graph_dags", None) or (
            [state.graph_dag] if getattr(state, "graph_dag", None) else []
        )
        if not dags:
            return
        # Concatenated flat list (same order as the panel).
        from juggle_cockpit_graph_panel import topological_order
        flat = [n for d in dags for n in topological_order(d.tasks, d.edges)]
        if not (0 <= self._graph_sel < len(flat)):
            return
        task_id = flat[self._graph_sel].id
        # Find which DAG owns this task and get its task list.
        owner_dag = next((d for d in dags if any(n.id == task_id for n in d.tasks)), None)
        tasks = (owner_dag.member_tasks or {}).get(task_id, []) if owner_dag else []
        # DAG roots are topics (kind='topic') OR parentless tasks (kind='task').
        # get_task filters kind='task' and returns None for a topic id, so fall
        # back to get_topic to populate the modal from the authoritative nodes
        # row (P8 c4-topic-dag flip missed this read-path → blank 'Task ?').
        task_row = _g.get_task(self._db, task_id)
        is_topic = task_row is None
        full = task_row or _t.get_topic(self._db, task_id) or {}
        try:
            deps = _g.get_deps(self._db, task_id)
        except Exception:
            deps = [d for dag in dags for (nid, d) in dag.edges if nid == task_id]
        if is_topic:
            # Topic node → unified modal with the async LLM summary streamed in
            # below the structured fields (header renders immediately).
            ctx = build_summary_ctx(self._db, full.get("thread_id"))
            if not ctx.get("task_input") and (full.get("objective") or "").strip():
                ctx["task_input"] = full["objective"].strip()
            self.push_screen(_NodeDetailModal(
                full, deps, is_topic=True, tasks=tasks,
                summary_ctx=ctx, label=full.get("id", task_id),
            ))
        else:
            self.push_screen(_NodeDetailModal(full, deps, is_topic=False, tasks=tasks))

    # -- key capture ----------------------------------------------------------

    def _graph_handle_key(self, event) -> bool:
        """Handle a key in graph mode. Return True if consumed (don't bubble)."""
        k = event.key
        if k in ("up", "down", "left", "right", "enter"):
            if k == "up":
                self._graph_select(-1)
            elif k == "down":
                self._graph_select(+1)
            elif k == "left":
                self._graph_pan_by(-1)
            elif k == "right":
                self._graph_pan_by(+1)
            else:  # enter
                self._open_graph_task_modal()
            event.stop()
            event.prevent_default()
            return True
        # j/k/PgUp/PgDn scroll the viewport over a large / multi-project graph.
        if k in ("j", "k", "pageup", "pagedown"):
            if self._graph_scroll_by(k):
                event.stop()
                event.prevent_default()
                return True
        if k == "escape":
            self._graph_mode = False
            event.stop()
            self._refresh()
            return True
        return False
