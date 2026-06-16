#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.14"
# dependencies = ["rich", "textual>=0.85"]
# ///
"""Juggle Cockpit — Textual-based dashboard with drag-to-resize panels.

Display-only. Never writes to DB. Never calls subprocess.

Run:  uv run src/juggle_cockpit.py [--db PATH]
Exit: q or Ctrl-C
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SRC_DIR = Path(__file__).parent
sys.path.insert(0, str(SRC_DIR))

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Static

from juggle_db import JuggleDB
from juggle_settings import get_settings as _get_settings
from juggle_cockpit_helpers import (
    _PRIORITY_TIER_MAP,
    _SCROLL_PANES,
    _apply_filter_actions,
    _apply_filter_text,
    _new_blocker_actions,
    _newly_failed_agents,
    _parse_filter,
    _resolve_actions_by_thread_label,
    _resolve_agent_by_index,
    _resolve_thread_by_label,
    _send_desktop_notification,
    _tmux_capture_pane,
    _tmux_focus_pane,
)
from juggle_cockpit_modals import (
    _ConfirmModal,
    _GraphTaskModal,
    _HelpModal,
    _PromptModal,
    _TailModal,
)
from juggle_cockpit_widgets import HSplitter, Splitter
from juggle_cockpit_graph_mode import GraphModeMixin
from juggle_cockpit_title import _cockpit_subtitle, _get_version
from juggle_watchdog_singleton import (
    canonical_repo_path,
    ensure_watchdog,
    is_watchdog_alive as _lock_watchdog_alive,
    read_lock_pid,
    restart_watchdog,
    toggle_watchdog,
)


# ---------------------------------------------------------------------------
# Column-ratio helpers (re-exported from juggle_cockpit_layout)
# ---------------------------------------------------------------------------
from juggle_cockpit_layout import (  # noqa: F401
    _DEFAULT_COL_RATIOS,
    _MAX_TOPICS_PCT,
    _MIN_ACTIONS_RATIO,
    _MIN_AGENTS_RATIO,
    _MIN_TOPICS_PCT,
    _MIN_TOPICS_RATIO,
    _clamp_col_pct,
    _compute_ratios,
    _sanitize_col_ratios,
    _write_ratios,
)

_SETTINGS = _get_settings()
REFRESH_INTERVAL: float = _SETTINGS["cockpit"]["refresh_interval_secs"]
_COL_RATIOS: list[float] = _sanitize_col_ratios(_SETTINGS["cockpit"]["column_ratios"])  # [topics, actions, agents]
_NOTIF_RATIO: int = _SETTINGS["cockpit"]["notification_ratio"]  # % height for notifications

# ---------------------------------------------------------------------------
# Persistent DB (same monkey-patch as v1)
# ---------------------------------------------------------------------------


def _make_cockpit_db(db_path: str | None = None) -> JuggleDB:
    """Create and initialise a JuggleDB for the cockpit.

    snapshot() opens its own fresh connection per call, so no connection caching
    is needed here.  Other JuggleDB methods (is_active, get_all_threads, …) open
    and close their own connections normally.
    """
    db = JuggleDB(db_path=db_path)
    db.init_db()
    return db


# ---------------------------------------------------------------------------
# CockpitApp
# ---------------------------------------------------------------------------


class CockpitApp(GraphModeMixin, App):
    """Juggle Cockpit v2."""

    BINDINGS = [
        Binding("ctrl+c",       "quit",          "Quit",    show=False),
        Binding("question_mark","help",          "Help"),
        Binding("j",            "scroll_down",   "↓",       show=False),
        Binding("k",            "scroll_up",     "↑",       show=False),
        Binding("down",         "scroll_down",   "↓",       show=False),
        Binding("up",           "scroll_up",     "↑",       show=False),
        Binding("pagedown",     "page_down",     "PgDn",    show=False),
        Binding("pageup",       "page_up",       "PgUp",    show=False),
        Binding("tab",          "cycle_pane",    "Tab"),
        Binding("s",            "switch",        "Sw"),
        Binding("a",            "ack",           "Ack"),
        Binding("shift+c",      "close",         "Cl",  key_display="C"),
        Binding("x",            "archive",       "Ar"),
        Binding("d",            "decommission",  "Dc",  show=False),
        Binding("slash",        "filter",        "Flt"),
        Binding("f",            "focus_pane",    "Foc"),
        Binding("t",            "tail_toggle",   "Tl"),
        Binding("T",            "task_detail",   "Tk"),
        Binding("g",            "toggle_graph",  "Gr"),
        Binding("p",            "projects",      "Proj"),
        Binding("w",            "watchdog_toggle",  "Wd"),
        Binding("r",            "watchdog_restart", "Rwd"),
        Binding("shift+w",      "watchdog_restart", "Rwd", show=False),
    ]

    CSS = """
    Screen {
        layers: base overlay;
    }
    #layout {
        layout: horizontal;
        height: 1fr;
    }
    #topics {
        height: 100%;
        min-width: 24;
    }
    #right {
        height: 100%;
        layout: vertical;
        min-width: 20;
    }
    #upper {
        layout: horizontal;
    }
    #actions {
        height: 100%;
    }
    #agents {
        height: 100%;
    }
    #notif-region {
        layout: vertical;
    }
    #notifications {
        height: 100%;
    }
    #graph-scroll {
        height: 100%;
        display: none;
    }
    #graph-body {
        height: auto;
    }
    Footer {
        layer: base;
    }
    #wd-status {
        layer: overlay;
        dock: bottom;
        width: 16;
        height: 1;
        offset: 0 -1;
        background: $panel;
        color: $success;
        content-align: right middle;
    }
    """

    def __init__(self, db_path: str | None = None) -> None:
        super().__init__()
        self._db = _make_cockpit_db(db_path)
        self._offsets: dict[str, int] = {p: 0 for p in _SCROLL_PANES}
        self._active_pane: str = "notifications"
        self._last_bp: str = "wide"  # tracks previous breakpoint for resize transitions
        self._filter: dict[str, str] = {
            "actions": "",
            "agents": "",
            "notifications": "",
        }
        # Phase 4: Bell / desktop notification diff state
        self._prev_action_ids: set[str] = set()
        self._prev_agent_statuses: dict[str, str] = {}  # id_short → status
        _settings = _get_settings()
        self._bell_enabled: bool = bool(
            _settings.get("cockpit", {}).get("bell", True)
        )
        self._desktop_notif_enabled: bool = bool(
            _settings.get("cockpit", {}).get("desktop_notifications", False)
        )
        self._cockpit_mgr = None
        try:
            from juggle_tmux import JuggleTmuxManager
            self._cockpit_mgr = JuggleTmuxManager()
        except Exception:
            pass
        # Graph-mode view state (lower-right panel swaps Notifications ⇄ Graph).
        self._graph_state_init()

    def exit(self, result=None, return_code: int = 0, message=None) -> None:
        """Persist column widths before handing off to Textual's exit machinery.

        Overrides App.exit() so _persist_ratios fires on every clean exit path:
        q binding, Ctrl+C (Textual converts SIGINT to exit()), and programmatic
        self.exit() calls. size.width is still valid here — widgets unmount after.
        """
        self._persist_ratios()
        super().exit(result=result, return_code=return_code, message=message)

    def _persist_ratios(self) -> None:
        """Write current column widths to config.json. Last-writer-wins on quit."""
        config_path = Path(
            os.environ.get("_JUGGLE_CONFIG_PATH", str(Path.home() / ".juggle" / "config.json"))
        )
        try:
            t_cells = self.query_one("#topics").size.width
            a_cells = self.query_one("#actions").size.width
            ag_cells = self.query_one("#agents").size.width
        except Exception:
            return
        ratios = _compute_ratios(t_cells, a_cells, ag_cells)
        if not ratios:
            return
        _write_ratios(config_path, ratios)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="layout"):
            yield Static("", id="topics")
            yield Splitter(
                "topics", "right",
                min_left_pct=_MIN_TOPICS_PCT,
                min_right_pct=100 - _MAX_TOPICS_PCT,
            )
            with Vertical(id="right"):
                with Horizontal(id="upper"):
                    yield Static("", id="actions")
                    yield Splitter(
                        "actions", "agents",
                        min_left_pct=int(_MIN_ACTIONS_RATIO * 100),
                        min_right_pct=int(_MIN_AGENTS_RATIO * 100),
                    )
                    yield Static("", id="agents")
                yield HSplitter("upper", "notif-region")
                with Vertical(id="notif-region"):
                    yield Static("", id="notifications")
                    with VerticalScroll(id="graph-scroll"):
                        yield Static("", id="graph-body")
        yield Footer(compact=True)
        yield Static("", id="wd-status")

    def on_mount(self) -> None:
        self.title = "Juggle"
        self.sub_title = _cockpit_subtitle(_get_version(), width=self.size.width)

        if not self._db.is_active():
            self.notify("Juggle inactive. Run /juggle:start first.", severity="error")
            self.exit(1)
            return

        # Apply settings-driven initial sizes.
        t, a, ag = _COL_RATIOS
        topics_w = _clamp_col_pct(int(t * 100))
        right_w = 100 - topics_w
        inner_total = a + ag
        actions_pct = int(a / inner_total * 100) if inner_total > 0 else 50
        agents_pct = 100 - actions_pct
        notif_pct = _NOTIF_RATIO       # height % for #notifications
        upper_pct = 100 - notif_pct   # height % for #upper

        self.query_one("#topics").styles.width = f"{topics_w}%"
        self.query_one("#right").styles.width = f"{right_w}%"
        self.query_one("#actions").styles.width = f"{actions_pct}%"
        self.query_one("#agents").styles.width = f"{agents_pct}%"
        self.query_one("#upper").styles.height = f"{upper_pct}%"
        self.query_one("#notif-region").styles.height = f"{notif_pct}%"

        self._check_tmux_mouse()
        self.set_interval(REFRESH_INTERVAL, self._refresh)
        # Cockpit is the lock-gated ensure-exists owner of ONE detached watchdog
        # (T-cockpit-watchdog-owner). The singleton flock — not a child handle —
        # owns singleton-ness, so the daemon survives cockpit exit. Re-ensure on
        # an interval to self-heal a crashed daemon; NEVER kill it on close.
        self._ensure_watchdog()
        self.set_interval(15.0, self._ensure_watchdog)

    def _watchdog_db_path(self) -> str:
        return str(self._db.db_path)

    def _ensure_watchdog(self) -> None:
        try:
            ensure_watchdog(
                self._watchdog_db_path(), repo_path=canonical_repo_path()
            )
        except Exception:
            pass
        self._update_watchdog_status()

    def action_watchdog_toggle(self) -> None:
        """W — toggle: stop a live watchdog, or start a detached one if none."""
        try:
            action = toggle_watchdog(
                self._watchdog_db_path(), repo_path=canonical_repo_path()
            )
            self.notify(f"watchdog {action}", timeout=4)
        except Exception as e:
            self.notify(f"watchdog toggle failed: {e}", severity="error")
        self._update_watchdog_status()

    def action_watchdog_restart(self) -> None:
        """R / shift+W — kill + relaunch from canonical main (latest code)."""
        try:
            restart_watchdog(
                self._watchdog_db_path(), repo_path=canonical_repo_path()
            )
            self.notify("watchdog restarted from main", timeout=4)
        except Exception as e:
            self.notify(f"watchdog restart failed: {e}", severity="error")
        self._update_watchdog_status()

    def _update_watchdog_status(self) -> None:
        try:
            db_path = self._watchdog_db_path()
            alive = _lock_watchdog_alive(db_path)
            if alive:
                pid = read_lock_pid(db_path)
                dot = f"● wd {pid}" if pid else "● wd"
            else:
                dot = "○ wd"
            widget = self.query_one("#wd-status", Static)
            widget.update(dot)
            widget.styles.color = "green" if alive else "red"
        except Exception:
            pass

    def _check_tmux_mouse(self) -> None:
        """Warn if running inside tmux with mouse mode disabled."""
        if not sys.stdin.isatty():
            return
        try:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "#{mouse}"],
                capture_output=True, text=True, timeout=1,
            )
            if result.returncode == 0 and result.stdout.strip() == "0":
                self.notify(
                    "tmux mouse mode off — drag-to-resize disabled. Enable: set -g mouse on",
                    severity="warning",
                    timeout=8,
                )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass  # not in tmux, or tmux not available

    def _refresh(self) -> None:
        from juggle_cockpit_model import snapshot as _snapshot
        from juggle_cockpit_view import (
            pick_breakpoint,
            render_actions,
            render_agents,
            render_notifications,
            render_topics,
        )

        try:
            state = _snapshot(self._db, load_graph_dag=self._graph_mode)

            # Unread-badge accounting while graph mode hides Notifications.
            self._graph_update_unread(state)

            # --- Bell / desktop notification diff (skip first tick; prev is empty) ---
            if self._prev_action_ids or self._prev_agent_statuses:
                new_blockers = _new_blocker_actions(self._prev_action_ids, state.actions)
                failed_agents = _newly_failed_agents(self._prev_agent_statuses, state.agents)
                if self._bell_enabled and (new_blockers or failed_agents):
                    self.bell()
                    if self._desktop_notif_enabled:
                        if new_blockers:
                            _send_desktop_notification(
                                "Juggle: Blocker",
                                f"{len(new_blockers)} new blocker(s): {new_blockers[0].text[:80]}",
                            )
                        elif failed_agents:
                            _send_desktop_notification(
                                "Juggle: Agent Failed",
                                f"Agent {failed_agents[0].role} went stale",
                            )

            # Update previous-state snapshots (always, even if bell disabled)
            self._prev_action_ids = {a.id for a in state.actions}
            self._prev_agent_statuses = {a.id_short: a.status for a in state.agents}

            # Apply active filters
            filtered_actions = _apply_filter_actions(
                state.actions, self._filter.get("actions", "")
            )
            filtered_agents = _apply_filter_text(
                state.agents, self._filter.get("agents", "")
            )
            filtered_notifs = _apply_filter_text(
                state.notifications, self._filter.get("notifications", "")
            )

            # Clamp offsets to content length (use unfiltered for bound — conservative)
            self._offsets["topics"] = min(self._offsets["topics"], max(0, len(state.topics) - 3))
            self._offsets["actions"] = min(self._offsets["actions"], max(0, len(state.actions) - 3))
            self._offsets["agents"] = min(self._offsets["agents"], max(0, len(state.agents) - 3))
            self._offsets["notifications"] = min(
                self._offsets["notifications"], max(0, len(state.notifications) - 3)
            )

            bp = pick_breakpoint(self.size.width)
            off = self._offsets
            active = self._active_pane

            self.query_one("#topics").update(
                render_topics(state.topics, bp, state.projects_by_id, off["topics"], active == "topics", graph_by_project=getattr(state, "graph_by_project", None))
            )
            self.query_one("#actions").update(
                render_actions(
                    filtered_actions, off["actions"], active == "actions",
                    filter_label=self._filter.get("actions", ""),
                )
            )
            self.query_one("#agents").update(
                render_agents(
                    filtered_agents, state.scheduled, off["agents"], active == "agents",
                    filter_label=self._filter.get("agents", ""),
                )
            )
            # Graph mode renders into a scrollable viewport (#graph-scroll);
            # Notifications mode renders into the plain #notifications Static.
            # Only one is displayed at a time within #notif-region.
            self.query_one("#graph-scroll").styles.display = (
                "block" if self._graph_mode else "none"
            )
            self.query_one("#notifications").styles.display = (
                "none" if self._graph_mode else "block"
            )
            if self._graph_mode:
                self.query_one("#graph-body", Static).update(
                    self._render_graph_panel(state)
                )
            else:
                self.query_one("#notifications", Static).update(
                    render_notifications(
                        filtered_notifs, off["notifications"], active == "notifications",
                        filter_label=self._filter.get("notifications", ""),
                    )
                )

        except Exception as e:
            self.notify(str(e), severity="error")

    # ------------------------------------------------------------------
    # Scroll / pane-cycle actions (replace old on_key handler)
    # ------------------------------------------------------------------

    def action_scroll_down(self) -> None:
        self._scroll(+1)

    def action_scroll_up(self) -> None:
        self._scroll(-1)

    def action_page_down(self) -> None:
        self._scroll(+5)

    def action_page_up(self) -> None:
        self._scroll(-5)

    def action_cycle_pane(self) -> None:
        self._cycle_pane()

    def _scroll(self, delta: int) -> None:
        pane = self._active_pane
        self._offsets[pane] = max(0, self._offsets[pane] + delta)
        self._refresh()

    def _cycle_pane(self) -> None:
        idx = _SCROLL_PANES.index(self._active_pane) if self._active_pane in _SCROLL_PANES else 0
        self._active_pane = _SCROLL_PANES[(idx + 1) % len(_SCROLL_PANES)]
        self._refresh()

    def _cycle_pane_backward(self) -> None:
        idx = _SCROLL_PANES.index(self._active_pane) if self._active_pane in _SCROLL_PANES else 0
        self._active_pane = _SCROLL_PANES[(idx - 1) % len(_SCROLL_PANES)]
        self._refresh()

    # ------------------------------------------------------------------
    # Safe actions: switch thread, ack actions, help overlay
    # ------------------------------------------------------------------

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
        """T — prompt for a task id or label and show its detail in _GraphTaskModal."""
        from juggle_cockpit_model import snapshot as _snapshot
        from juggle_cockpit_modals import resolve_task_detail
        import dbops.db_graph as _g

        state = _snapshot(self._db)

        # Flatten all graph tasks across all projects, enriched with _label.
        # _label is the thread's user_label slug (e.g. "AI") for label-based lookup.
        label_by_thread: dict[str, str] = {t.id: t.label for t in state.topics}
        all_tasks: list[dict] = []
        try:
            import sqlite3
            with self._db._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, project_id, title, state, thread_id, verify_cmd, "
                    "prompt, handoff FROM graph_tasks"
                ).fetchall()
            for r in rows:
                d = dict(r)
                tid = d.get("thread_id")
                if tid and tid in label_by_thread:
                    d["_label"] = label_by_thread[tid]
                all_tasks.append(d)
        except Exception:
            pass

        def _on_query(q: str | None) -> None:
            if q is None:
                return
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
            self.push_screen(_GraphTaskModal(task, real_deps, tasks=all_tasks))

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

    def action_help(self) -> None:
        """? — show help overlay."""
        self.push_screen(_HelpModal())

    def action_projects(self) -> None:
        """p — show project arm/disarm overlay."""
        from juggle_cockpit_modals import _ProjectArmModal
        self.push_screen(_ProjectArmModal(self._db))

    def on_resize(self, event: events.Resize) -> None:
        from juggle_cockpit_view import pick_breakpoint
        bp = pick_breakpoint(event.size.width)
        try:
            if bp == "wide":
                if self._last_bp != "wide":
                    # Transitioning narrow/medium → wide: restore column widths from config.
                    # Heights (#upper / #notif-region) are preserved from HSplitter drag.
                    t, a, ag = _COL_RATIOS
                    topics_w = _clamp_col_pct(int(t * 100))
                    self.query_one("#topics").styles.display = "block"
                    self.query_one("#topics").styles.width = f"{topics_w}%"
                    self.query_one("#right").styles.width = f"{100 - topics_w}%"
                    inner_total = a + ag
                    actions_pct = int(a / inner_total * 100) if inner_total > 0 else 50
                    agents_pct = 100 - actions_pct
                    self.query_one("#actions").styles.width = f"{actions_pct}%"
                    self.query_one("#agents").styles.width = f"{agents_pct}%"
                # else: already wide — preserve user-dragged sizes unchanged
            else:
                # medium/narrow: collapse topics column
                self.query_one("#topics").styles.display = "none"
                self.query_one("#right").styles.width = "100%"
            self._last_bp = bp
        except Exception:
            pass

    _PANE_IDS = frozenset(("topics", "actions", "agents", "notifications"))

    def on_mouse_move(self, event: events.MouseMove) -> None:
        pane_id = getattr(event.widget, "id", None)
        if pane_id in self._PANE_IDS and self._active_pane != pane_id:
            self._active_pane = pane_id
            self._refresh()

    def on_mouse_scroll_up(self, event) -> None:
        pane_id = getattr(getattr(event, "widget", None), "id", None)
        if pane_id not in self._PANE_IDS:
            pane_id = self._active_pane
        self._active_pane = pane_id
        self._offsets[pane_id] = max(0, self._offsets.get(pane_id, 0) - 1)
        self._refresh()

    def on_mouse_scroll_down(self, event) -> None:
        pane_id = getattr(getattr(event, "widget", None), "id", None)
        if pane_id not in self._PANE_IDS:
            pane_id = self._active_pane
        self._active_pane = pane_id
        self._offsets[pane_id] = self._offsets.get(pane_id, 0) + 1
        self._refresh()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(db_path: str | None = None) -> None:
    try:
        app = CockpitApp(db_path=db_path)
        app.run()
    except Exception as exc:
        try:
            from juggle_selfheal import record_error
            record_error(exc, "juggle_cockpit.run")
        except Exception:
            pass
        raise




# ---------------------------------------------------------------------------
# Profile harness (re-exported from juggle_cockpit_profile)
# ---------------------------------------------------------------------------
from juggle_cockpit_profile import (  # noqa: F401
    _parse_psrecord_log,
    _profile_worker_loop,
    run_profile,
)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    parser = argparse.ArgumentParser(description="Juggle Cockpit (Textual)")
    parser.add_argument("--db", dest="db_path", default=None, help="Path to juggle.db")
    parser.add_argument(
        "--out",
        action="store_true",
        help="Render panes as plain text to stdout then exit (no TUI)",
    )
    parser.add_argument(
        "--profile",
        action="store_true",
        help="Run headless resource-usage profiling loop (no TUI)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        metavar="N",
        help="Duration in seconds for --profile (default: 60)",
    )
    parser.add_argument(
        "--profile-worker",
        action="store_true",
        dest="profile_worker",
        help=argparse.SUPPRESS,  # internal: child process spawned by run_profile
    )
    parser.add_argument("--screenshot", metavar="PATH", default=None, help="Save PNG/JPG/SVG screenshot to PATH")
    parser.add_argument("--graph", action="store_true", help="Render the lower-right panel in graph mode (screenshot)")
    args = parser.parse_args()
    if args.screenshot:
        from juggle_cockpit_screenshot import save_screenshot
        out = save_screenshot(
            args.screenshot, args.db_path, graph_mode=getattr(args, "graph", False)
        )
        print(out)
        sys.exit(0)
    if args.out:
        from juggle_cockpit_static import render_static
        sys.stdout.write(render_static(db_path=args.db_path))
        sys.exit(0)
    if args.profile_worker:
        _profile_worker_loop(args.duration, db_path=args.db_path)
        sys.exit(0)
    if args.profile:
        run_profile(duration=args.duration, db_path=args.db_path)
        sys.exit(0)
    run(db_path=args.db_path)
