"""Textual action_* keybinding handlers for the cockpit App.

Owns the destructive/interactive thread + agent actions (switch, ack, close,
archive, decommission, filter, focus-pane, tail, task-detail) and the on_key
Tab/Shift+Tab/Escape interception that must run before Textual's focus
traversal. Extracted from juggle_cockpit.py to keep that module within its
LOC budget, mirroring the existing GraphModeMixin extraction.

Mixed into CockpitApp; relies on these attributes existing on self: ``_db``,
``_active_pane``, ``_filter``, ``_offsets``, ``_graph_mode``, plus Textual's
``notify``/``push_screen``/``screen_stack`` and the app's ``_refresh``,
``_cycle_pane``, ``_cycle_pane_backward``, and (from GraphModeMixin)
``_graph_handle_key``.
"""
from __future__ import annotations

from textual import events

from juggle_cockpit_helpers import (
    _resolve_actions_by_thread_label,
    _resolve_agent_by_index,
    _resolve_thread_by_label,
    _tmux_capture_pane,
    _tmux_focus_pane,
)
from juggle_cockpit_modals import (
    _ConfirmModal,
    _PromptModal,
    _TailModal,
)


class CockpitActionsMixin:
    """Provides action_* keybinding handlers to CockpitApp."""

    def action_switch(self) -> None:
        """s — switch active thread by label."""
        def _on_label(label: str | None) -> None:
            if label is None:
                return
            label_up = label.strip().upper()
            threads = self._db.get_all_threads()
            match = _resolve_thread_by_label(threads, label_up)
            if match is None:
                self.notify(f"Thread '{label_up}' not found", severity="warning", timeout=3)
                return
            try:
                self._db.set_current_thread(match["id"])
                self.notify(f"Switched to [{label_up}]", timeout=2)
                self._refresh()
            except Exception as exc:
                self.notify(f"Switch failed: {exc}", severity="error", timeout=4)

        self.push_screen(_PromptModal("Switch to thread (label):"), _on_label)

    def action_ack(self) -> None:
        """a — ack all open action items on a thread by label (Z = orphaned/null-thread)."""
        def _on_label(label: str | None) -> None:
            if label is None:
                return
            label_up = label.strip().upper()
            if label_up == "Z":
                try:
                    count = self._db.dismiss_orphan_action_items()
                    if count:
                        self.notify(f"Acked {count} orphaned action(s) [Z]", timeout=2)
                    else:
                        self.notify("No orphaned actions [Z]", severity="warning", timeout=3)
                    self._refresh()
                except Exception as exc:
                    self.notify(f"Ack failed: {exc}", severity="error", timeout=4)
                return
            threads = self._db.get_all_threads()
            match = _resolve_thread_by_label(threads, label_up)
            if match is None:
                self.notify(f"Thread '{label_up}' not found", severity="warning", timeout=3)
                return
            open_actions = self._db.get_open_action_items()
            matching = _resolve_actions_by_thread_label(threads, open_actions, label_up)
            if not matching:
                self.notify(f"No open actions on [{label_up}]", severity="warning", timeout=3)
                return
            try:
                count = self._db.dismiss_action_items_for_thread(match["id"])
                self.notify(f"Acked {count} action(s) on [{label_up}]", timeout=2)
                self._refresh()
            except Exception as exc:
                self.notify(f"Ack failed: {exc}", severity="error", timeout=4)

        self.push_screen(_PromptModal("Ack action(s) for thread (label):"), _on_label)

    def action_close(self) -> None:
        """C — close thread by label (y/N confirm)."""
        def _on_label(label: str | None) -> None:
            if label is None:
                return
            label_up = label.strip().upper()
            threads = self._db.get_all_threads()
            match = _resolve_thread_by_label(threads, label_up)
            if match is None:
                self.notify(f"Thread '{label_up}' not found", severity="warning", timeout=3)
                return

            def _on_confirm(confirmed: bool) -> None:
                if not confirmed:
                    return
                try:
                    self._db.set_thread_status(match["id"], "closed")
                    self.notify(f"Thread [{label_up}] closed", timeout=2)
                    self._refresh()
                except Exception as exc:
                    self.notify(f"Close failed: {exc}", severity="error", timeout=4)

            self.push_screen(_ConfirmModal(f"Close thread [{label_up}]?"), _on_confirm)

        self.push_screen(_PromptModal("Close thread (label):"), _on_label)

    def action_archive(self) -> None:
        """x — archive thread by label (y/N confirm)."""
        def _on_label(label: str | None) -> None:
            if label is None:
                return
            label_up = label.strip().upper()
            threads = self._db.get_all_threads()
            match = _resolve_thread_by_label(threads, label_up)
            if match is None:
                self.notify(f"Thread '{label_up}' not found", severity="warning", timeout=3)
                return

            def _on_confirm(confirmed: bool) -> None:
                if not confirmed:
                    return
                try:
                    self._db.archive_thread(match["id"])
                    self.notify(f"Thread [{label_up}] archived", timeout=2)
                    self._refresh()
                except Exception as exc:
                    self.notify(f"Archive failed: {exc}", severity="error", timeout=4)

            self.push_screen(_ConfirmModal(f"Archive thread [{label_up}]?"), _on_confirm)

        self.push_screen(_PromptModal("Archive thread (label):"), _on_label)

    def action_decommission(self) -> None:
        """d — decommission agent by 1-based index (y/N confirm)."""
        from juggle_cockpit_model import snapshot as _snapshot
        state = _snapshot(self._db)
        agents = state.agents
        if not agents:
            self.notify("No agents running", severity="warning", timeout=2)
            return

        def _on_index(raw: str | None) -> None:
            if raw is None:
                return
            try:
                idx_1based = int(raw.strip())
            except ValueError:
                self.notify("Type a number (e.g. 2)", severity="warning", timeout=2)
                return
            agent = _resolve_agent_by_index(agents, idx_1based)
            if agent is None:
                self.notify(
                    f"Agent index out of range (1–{len(agents)})",
                    severity="warning", timeout=2,
                )
                return

            def _on_confirm(confirmed: bool) -> None:
                if not confirmed:
                    return
                try:
                    # Agent.id_short is only 8 chars; resolve full ID via DB
                    all_db_agents = self._db.get_all_agents()
                    full = next(
                        (a for a in all_db_agents if a["id"].startswith(agent.id_short)),
                        None,
                    )
                    if full is None:
                        self.notify("Agent not found in DB", severity="error", timeout=3)
                        return
                    self._db.update_agent(full["id"], status="decommission_pending")
                    self.notify(
                        f"Agent #{idx_1based} ({agent.role}) decommission queued",
                        timeout=2,
                    )
                    self._refresh()
                except Exception as exc:
                    self.notify(f"Decommission failed: {exc}", severity="error", timeout=4)

            self.push_screen(
                _ConfirmModal(f"Decommission agent #{idx_1based} ({agent.role})?"),
                _on_confirm,
            )

        self.push_screen(_PromptModal(f"Decommission agent (1–{len(agents)}):"), _on_index)

    def action_filter(self) -> None:
        """/ — open filter prompt for the active pane."""
        pane = self._active_pane
        prompt = (
            f"Filter {pane}"
            + (
                " (blank=clear; 'priority:high [text]'):"
                if pane == "actions"
                else " (blank=clear):"
            )
        )

        def _on_text(text: str | None) -> None:
            if text is None:
                return  # Esc in modal — keep existing filter unchanged
            self._filter[pane] = text.strip()
            self._offsets[pane] = 0  # reset offset when filter changes
            self._refresh()

        # dismiss_empty_as="" so blank submit clears the filter (passes "" not None)
        self.push_screen(_PromptModal(prompt, dismiss_empty_as=""), _on_text)

    def action_focus_pane(self) -> None:
        """f — focus the tmux pane of an agent by 1-based index."""
        from juggle_cockpit_model import snapshot as _snapshot
        state = _snapshot(self._db)
        agents = state.agents
        if not agents:
            self.notify("No agents", severity="warning", timeout=2)
            return

        def _on_index(raw: str | None) -> None:
            if raw is None:
                return
            try:
                idx_1based = int(raw.strip())
            except ValueError:
                self.notify("Type a number (e.g. 2)", severity="warning", timeout=2)
                return
            agent = _resolve_agent_by_index(agents, idx_1based)
            if agent is None:
                self.notify(
                    f"Agent index out of range (1–{len(agents)})",
                    severity="warning", timeout=2,
                )
                return
            if not agent.pane_id:
                self.notify(
                    f"Agent #{idx_1based} has no tmux pane",
                    severity="warning", timeout=2,
                )
                return
            ok = _tmux_focus_pane(agent.pane_id)
            if ok:
                self.notify(f"Focused {agent.pane_id} ({agent.role})", timeout=2)
            else:
                self.notify(
                    f"tmux select-pane failed for {agent.pane_id}",
                    severity="error", timeout=3,
                )

        self.push_screen(_PromptModal(f"Focus agent (1–{len(agents)}):"), _on_index)

    def action_tail_toggle(self) -> None:
        """t — open tail modal for an agent's tmux pane."""
        from juggle_cockpit_model import snapshot as _snapshot
        state = _snapshot(self._db)
        agents = state.agents
        if not agents:
            self.notify("No agents", severity="warning", timeout=2)
            return

        def _on_index(raw: str | None) -> None:
            if raw is None:
                return
            try:
                idx_1based = int(raw.strip())
            except ValueError:
                self.notify("Type a number", severity="warning", timeout=2)
                return
            agent = _resolve_agent_by_index(agents, idx_1based)
            if agent is None:
                self.notify(
                    f"Agent index out of range (1–{len(agents)})",
                    severity="warning", timeout=2,
                )
                return
            if not agent.pane_id:
                self.notify(
                    f"Agent #{idx_1based} has no tmux pane",
                    severity="warning", timeout=2,
                )
                return
            self.push_screen(_TailModal(agent.pane_id, _tmux_capture_pane))

        self.push_screen(_PromptModal(f"Tail agent (1–{len(agents)}):"), _on_index)

    def action_task_detail(self) -> None:
        """i — prompt for a task id or label and show its detail.

        Resolution order:
          1. Thread/topic human-readable label (e.g. "AO") → topic _NodeDetailModal
          2. Graph-task id / prefix / _label               → task _NodeDetailModal
          3. Neither match → warning notification
        """
        from juggle_cockpit_model import snapshot as _snapshot
        from juggle_cockpit_modals import (
            resolve_task_detail,
            resolve_thread_detail,
            _NodeDetailModal,
            build_summary_ctx,
        )
        import dbops.db_graph as _g

        state = _snapshot(self._db)

        # Flatten all graph tasks across all projects, enriched with _label.
        label_by_thread: dict[str, str] = {t.id: t.label for t in state.topics}
        all_tasks: list[dict] = []
        try:
            import sqlite3
            with self._db._connect() as conn:
                conn.row_factory = sqlite3.Row
                # P8 c4-write-cut: task nodes are the authoritative store; project
                # the legacy graph_tasks row shape (objective->prompt, dispatch edge
                # ->thread_id) so the topic modal keeps its column names.
                rows = conn.execute(
                    "SELECT id, project_id, title, state, "
                    "(SELECT depends_on_id FROM node_edges WHERE node_id=nodes.id "
                    " AND kind='dispatch' LIMIT 1) AS thread_id, "
                    "verify_cmd, objective AS prompt, handoff "
                    "FROM nodes WHERE kind='task'"
                ).fetchall()
            for r in rows:
                d = dict(r)
                tid = d.get("thread_id")
                if tid and tid in label_by_thread:
                    d["_label"] = label_by_thread[tid]
                all_tasks.append(d)
        except Exception:
            pass

        # Build agent-lookup by assigned thread label for topic modal
        agent_by_label: dict[str, str] = {}
        for ag in state.agents:
            if ag.topic_id and ag.id_short:
                agent_by_label.setdefault(ag.topic_id.upper(), ag.id_short)

        def _on_query(q: str | None) -> None:
            if q is None:
                return

            # Priority 1: thread/topic human-readable label
            topic = resolve_thread_detail(state.topics, q)
            if topic is not None:
                # Shared summary-context builder (messages, task input/result).
                extra = build_summary_ctx(self._db, topic.id)
                agent = agent_by_label.get(topic.label.upper())
                if agent:
                    extra["agent"] = agent
                try:
                    thread = self._db.get_thread(topic.id)
                    if thread:
                        summary = (thread.get("summary") or "").strip()
                        if summary:
                            extra["summary"] = summary
                except Exception:
                    pass
                self.push_screen(_NodeDetailModal.from_conversation(topic, extra))
                return

            # Priority 2: graph-task id / prefix / _label fallback
            result = resolve_task_detail(all_tasks, q)
            if result is None:
                self.notify(f"No task matching '{q}'", severity="warning", timeout=3)
                return
            task, deps = result
            task_id = task.get("id", "")
            try:
                real_deps = _g.get_deps(self._db, task_id)
            except Exception:
                real_deps = deps
            self.push_screen(_NodeDetailModal(task, real_deps, is_topic=False, tasks=all_tasks))

        self.push_screen(
            _PromptModal("Task id or label (e.g. AI):", dismiss_empty_as=None),
            _on_query,
        )

    def on_key(self, event: events.Key) -> None:
        """Intercept Tab/Shift+Tab before Textual focus traversal; clear filter on Escape."""
        # Graph mode captures navigation keys so they don't leak to global
        # scroll/cycle. Only when no modal is open. (Logic in GraphModeMixin.)
        if self._graph_mode and len(self.screen_stack) <= 1:
            if self._graph_handle_key(event):
                return

        # Tab / Shift+Tab — must intercept here with prevent_default() so Textual's
        # built-in focus-traversal doesn't consume the key before our binding fires.
        if event.key in ("tab", "shift+tab", "backtab"):
            if len(self.screen_stack) > 1:  # modal open — let it handle Tab
                return
            if event.key == "tab":
                self._cycle_pane()          # advance forward
            else:
                self._cycle_pane_backward() # retreat backward
            event.stop()
            event.prevent_default()
            return

        if event.key == "escape" and any(self._filter.values()):
            if len(self.screen_stack) > 1:  # Modal is open — let it handle Esc
                return
            self._filter = {k: "" for k in self._filter}
            self._offsets[self._active_pane] = 0  # reset active pane offset
            event.stop()
            self._refresh()
