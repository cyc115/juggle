"""Graph-mode controller mixin for the cockpit App.

Owns the lower-right panel's Notifications ⇄ Graph toggle, node selection,
horizontal pan, the unread badge accounting, the detail-modal launch, and the
in-graph key capture. Extracted from juggle_cockpit.py to keep that module
within its LOC budget. Read-only — never writes the DB.

Mixed into CockpitApp; relies on these attributes existing on self:
``_db``, ``_graph_mode``, ``_graph_sel``, ``_graph_pan``, ``_graph_unread``,
``_graph_unread_seen`` (initialised via ``_graph_state_init``), plus Textual's
``query_one``/``push_screen``/``screen_stack`` and the app's ``_refresh``.
"""
from __future__ import annotations

from juggle_cockpit_modals import _GraphNodeModal


class GraphModeMixin:
    """Provides graph-mode behaviour to CockpitApp."""

    def _graph_state_init(self) -> None:
        """Initialise graph view-state. Call from __init__."""
        self._graph_mode = False
        self._graph_sel = 0          # selected node index (rank-major, id order)
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
        """Build the graph Panel from the snapshot's lazily-loaded DAG."""
        from juggle_cockpit_graph_panel import build_graph_panel

        dag = getattr(state, "graph_dag", None)
        try:
            w = self.query_one("#notifications").size.width or 80
            h = self.query_one("#notifications").size.height or 20
        except Exception:
            w, h = 80, 20
        nodes = dag.nodes if dag else []
        self._graph_sel = min(self._graph_sel, max(0, len(nodes) - 1))
        return build_graph_panel(
            project_id=(dag.project_id if dag else None),
            nodes=nodes,
            edges=(dag.edges if dag else []),
            selection=self._graph_sel,
            unread=self._graph_unread,
            width=w, height=h, pan_offset=self._graph_pan,
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

    def _graph_select(self, delta: int) -> None:
        self._graph_sel = max(0, self._graph_sel + delta)
        self._refresh()

    def _graph_pan_by(self, delta: int) -> None:
        self._graph_pan = max(0, self._graph_pan + delta)
        self._refresh()

    def _open_graph_node_modal(self) -> None:
        """Enter — open the read-only detail modal for the selected node."""
        from juggle_cockpit_model import snapshot as _snapshot
        from dbops import db_graph as _g

        try:
            state = _snapshot(self._db, load_graph_dag=True)
        except Exception:
            return
        dag = getattr(state, "graph_dag", None)
        if not dag or not dag.nodes:
            return
        nodes = sorted(dag.nodes, key=lambda n: n.id)
        if not (0 <= self._graph_sel < len(nodes)):
            return
        node_id = nodes[self._graph_sel].id
        full = _g.get_node(self._db, node_id) or {}
        try:
            deps = _g.get_deps(self._db, node_id)
        except Exception:
            deps = [d for (nid, d) in dag.edges if nid == node_id]
        self.push_screen(_GraphNodeModal(full, deps))

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
                self._open_graph_node_modal()
            event.stop()
            event.prevent_default()
            return True
        if k == "escape":
            self._graph_mode = False
            event.stop()
            self._refresh()
            return True
        return False
