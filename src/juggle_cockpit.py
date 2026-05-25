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
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static

from juggle_db import JuggleDB
from juggle_settings import get_settings as _get_settings


def _compute_ratios(topics_cells: float, actions_cells: float, agents_cells: float) -> list[float]:
    """Normalize actual rendered cell widths to [topics, actions, agents] ratios summing to 1.0.

    Uses size.width (absolute cells) so the result is correct regardless of whether
    styles were set as percent (initial mount) or as cell integers (post-drag).
    The last element absorbs floating-point rounding to ensure exact sum of 1.0.
    """
    total = topics_cells + actions_cells + agents_cells
    if total <= 0:
        return []
    t = round(topics_cells / total, 2)
    a = round(actions_cells / total, 2)
    ag = round(1.0 - t - a, 2)
    return [t, a, ag]


def _write_ratios(config_path: Path, ratios: list[float]) -> None:
    """Atomically write column_ratios to config.json.

    No-op if config file is missing or the cockpit key is absent — avoids
    corrupting a partially-edited config on first run. Atomic via tmp + os.replace.
    """
    if not config_path.exists():
        return
    try:
        cfg = json.loads(config_path.read_text())
    except (json.JSONDecodeError, OSError):
        return
    if "cockpit" not in cfg:
        return
    cfg["cockpit"]["column_ratios"] = ratios
    tmp = config_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.replace(tmp, config_path)


_SETTINGS = _get_settings()
REFRESH_INTERVAL: float = _SETTINGS["cockpit"]["refresh_interval_secs"]
_COL_RATIOS: list[float] = _SETTINGS["cockpit"]["column_ratios"]  # [topics, actions, agents]
_NOTIF_RATIO: int = _SETTINGS["cockpit"]["notification_ratio"]  # % height for notifications

_SCROLL_PANES = ("actions", "agents", "notifications")


# ---------------------------------------------------------------------------
# Persistent DB (same monkey-patch as v1)
# ---------------------------------------------------------------------------


def _make_cockpit_db(db_path: str | None = None) -> JuggleDB:
    """Create a JuggleDB with a cached connection — avoids file-descriptor leak in 1s loop."""
    import sqlite3 as _sqlite3

    db = JuggleDB(db_path=db_path)
    db.init_db()
    conn = _sqlite3.connect(str(db.db_path))
    conn.row_factory = _sqlite3.Row
    db._connect = lambda: conn  # noqa: E731 — intentional monkey-patch
    return db


# ---------------------------------------------------------------------------
# Splitter — drag handle between two sibling panels
# ---------------------------------------------------------------------------


class Splitter(Static):
    """Vertical drag handle. Resizes the widget pair on either side."""

    DEFAULT_CSS = """
    Splitter {
        width: 1;
        height: 100%;
        background: $panel-darken-1;
        color: $panel-lighten-2;
    }
    Splitter:hover {
        background: $accent;
    }
    """

    def __init__(self, left_id: str, right_id: str) -> None:
        super().__init__("│")
        self._left_id = left_id
        self._right_id = right_id
        self._dragging = False
        self._drag_start_x: int = 0
        self._left_start_w: int = 0
        self._right_start_w: int = 0

    def on_mouse_down(self, event: events.MouseDown) -> None:
        left = self.app.query_one(f"#{self._left_id}")
        right = self.app.query_one(f"#{self._right_id}")
        self._dragging = True
        self._drag_start_x = event.screen_x
        self._left_start_w = left.size.width
        self._right_start_w = right.size.width
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        delta = event.screen_x - self._drag_start_x
        left = self.app.query_one(f"#{self._left_id}")
        right = self.app.query_one(f"#{self._right_id}")
        total = self._left_start_w + self._right_start_w
        new_left = max(8, min(total - 8, self._left_start_w + delta))
        new_right = total - new_left
        left.styles.width = new_left
        right.styles.width = new_right
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self.release_mouse()
        self._dragging = False
        event.stop()


# ---------------------------------------------------------------------------
# CockpitApp
# ---------------------------------------------------------------------------


class CockpitApp(App):
    """Juggle Cockpit v2."""

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    CSS = """
    #layout {
        layout: horizontal;
        height: 1fr;
    }
    #topics {
        height: 100%;
    }
    #right {
        height: 100%;
        layout: vertical;
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
    """

    def __init__(self, db_path: str | None = None) -> None:
        super().__init__()
        self._db = _make_cockpit_db(db_path)
        self._offsets: dict[str, int] = {p: 0 for p in _SCROLL_PANES}
        self._active_pane: str = "notifications"
        self._last_reap: float = 0.0
        self._last_bp: str = "wide"  # tracks previous breakpoint for resize transitions
        self._cockpit_mgr = None
        try:
            from juggle_tmux import JuggleTmuxManager
            self._cockpit_mgr = JuggleTmuxManager()
        except Exception:
            pass

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
            yield Splitter("topics", "right")
            with Vertical(id="right"):
                with Horizontal(id="upper"):
                    yield Static("", id="actions")
                    yield Splitter("actions", "agents")
                    yield Static("", id="agents")
                yield Static("", id="notifications")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Juggle"
        self.sub_title = "Cockpit v2"

        if not self._db.is_active():
            self.notify("Juggle inactive. Run /juggle:start first.", severity="error")
            self.exit(1)
            return

        # Apply settings-driven initial widths
        t, a, ag = _COL_RATIOS
        topics_w = int(t * 100)
        right_w = 100 - topics_w
        actions_pct = int(a / (a + ag) * 100)
        agents_pct = 100 - actions_pct
        notif_pct = _NOTIF_RATIO
        upper_pct = 100 - notif_pct

        self.query_one("#topics").styles.width = f"{topics_w}%"
        self.query_one("#right").styles.width = f"{right_w}%"
        self.query_one("#actions").styles.width = f"{actions_pct}%"
        self.query_one("#agents").styles.width = f"{agents_pct}%"
        self.query_one("#upper").styles.height = f"{upper_pct}%"
        self.query_one("#notifications").styles.height = f"{notif_pct}%"

        self._check_tmux_mouse()
        self.set_interval(REFRESH_INTERVAL, self._refresh)

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
            # Throttled reaper (60s)
            now = time.time()
            if now - self._last_reap >= 60 and self._cockpit_mgr is not None:
                try:
                    from juggle_tmux import reap_stale_agents
                    reap_stale_agents(self._db, self._cockpit_mgr)
                    self._last_reap = now
                except Exception:
                    pass

            state = _snapshot(self._db)

            # Clamp offsets to content length
            self._offsets["actions"] = min(self._offsets["actions"], max(0, len(state.actions) - 3))
            self._offsets["agents"] = min(self._offsets["agents"], max(0, len(state.agents) - 3))
            self._offsets["notifications"] = min(
                self._offsets["notifications"], max(0, len(state.notifications) - 3)
            )

            size = self.size
            bp = pick_breakpoint(size)
            off = self._offsets
            active = self._active_pane

            self.query_one("#topics").update(render_topics(state.topics, bp))
            self.query_one("#actions").update(
                render_actions(state.actions, off["actions"], active == "actions")
            )
            self.query_one("#agents").update(
                render_agents(state.agents, state.scheduled, off["agents"], active == "agents")
            )
            self.query_one("#notifications").update(
                render_notifications(state.notifications, off["notifications"], active == "notifications")
            )
        except Exception as e:
            self.notify(str(e), severity="error")

    def on_key(self, event: events.Key) -> None:
        if event.key in ("up", "k"):
            self._scroll(-1)
            event.stop()
        elif event.key in ("down", "j"):
            self._scroll(+1)
            event.stop()
        elif event.key == "tab":
            self._cycle_pane()
            event.stop()

    def _scroll(self, delta: int) -> None:
        pane = self._active_pane
        self._offsets[pane] = max(0, self._offsets[pane] + delta)
        self._refresh()

    def _cycle_pane(self) -> None:
        idx = _SCROLL_PANES.index(self._active_pane) if self._active_pane in _SCROLL_PANES else 0
        self._active_pane = _SCROLL_PANES[(idx + 1) % len(_SCROLL_PANES)]
        self._refresh()

    def on_resize(self, event: events.Resize) -> None:
        from juggle_cockpit_view import pick_breakpoint
        bp = pick_breakpoint(event.size)
        try:
            if bp == "wide":
                if self._last_bp != "wide":
                    # Transitioning narrow/medium → wide: restore all columns from config.
                    # Also resets #actions/#agents so they fit the restored #right width.
                    t, a, ag = _COL_RATIOS
                    topics_w = int(t * 100)
                    self.query_one("#topics").styles.display = "block"
                    self.query_one("#topics").styles.width = f"{topics_w}%"
                    self.query_one("#right").styles.width = f"{100 - topics_w}%"
                    actions_pct = int(a / (a + ag) * 100)
                    self.query_one("#actions").styles.width = f"{actions_pct}%"
                    self.query_one("#agents").styles.width = f"{100 - actions_pct}%"
                # else: already wide — preserve user-dragged widths unchanged
            else:
                # medium/narrow: collapse topics column into notifications area
                self.query_one("#topics").styles.display = "none"
                self.query_one("#right").styles.width = "100%"
            self._last_bp = bp
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(db_path: str | None = None) -> None:
    app = CockpitApp(db_path=db_path)
    app.run()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    parser = argparse.ArgumentParser(description="Juggle Cockpit v2 (Textual)")
    parser.add_argument("--db", dest="db_path", default=None, help="Path to juggle.db")
    args = parser.parse_args()
    run(db_path=args.db_path)
