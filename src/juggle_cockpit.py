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
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, Label, Static

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
    """Vertical drag handle. Resizes the left/right widget pair on either side."""

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


class HSplitter(Static):
    """Horizontal drag handle. Resizes the top/bottom widget pair above and below."""

    DEFAULT_CSS = """
    HSplitter {
        width: 100%;
        height: 1;
        background: $panel-darken-1;
        color: $panel-lighten-2;
    }
    HSplitter:hover {
        background: $accent;
    }
    """

    def __init__(self, top_id: str, bottom_id: str) -> None:
        super().__init__("─")
        self._top_id = top_id
        self._bottom_id = bottom_id
        self._dragging = False
        self._drag_start_y: int = 0
        self._top_start_h: int = 0
        self._bottom_start_h: int = 0

    def on_mouse_down(self, event: events.MouseDown) -> None:
        top = self.app.query_one(f"#{self._top_id}")
        bottom = self.app.query_one(f"#{self._bottom_id}")
        self._dragging = True
        self._drag_start_y = event.screen_y
        self._top_start_h = top.size.height
        self._bottom_start_h = bottom.size.height
        self.capture_mouse()
        event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self._dragging:
            return
        delta = event.screen_y - self._drag_start_y
        top = self.app.query_one(f"#{self._top_id}")
        bottom = self.app.query_one(f"#{self._bottom_id}")
        total = self._top_start_h + self._bottom_start_h
        new_top = max(4, min(total - 4, self._top_start_h + delta))
        new_bottom = total - new_top
        top.styles.height = new_top
        bottom.styles.height = new_bottom
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        self.release_mouse()
        self._dragging = False
        event.stop()


# ---------------------------------------------------------------------------
# Pure helpers (module-level for unit testability)
# ---------------------------------------------------------------------------


def _resolve_thread_by_label(threads: list[dict], label: str) -> dict | None:
    """Return the first thread dict whose user_label matches label (case-insensitive)."""
    label_up = label.upper()
    return next(
        (t for t in threads if (t.get("user_label") or "").upper() == label_up),
        None,
    )


def _resolve_actions_by_thread_label(
    threads: list[dict], open_actions: list[dict], label: str
) -> list[dict]:
    """Return all open action dicts whose thread_id belongs to the named thread."""
    thread = _resolve_thread_by_label(threads, label)
    if thread is None:
        return []
    thread_id = thread.get("id")
    return [a for a in open_actions if a.get("thread_id") == thread_id]


# ---------------------------------------------------------------------------
# Modal screens
# ---------------------------------------------------------------------------


class _PromptModal(ModalScreen):
    """Generic one-line input modal. Dismisses with the stripped value or None."""

    DEFAULT_CSS = """
    _PromptModal {
        align: center middle;
    }
    _PromptModal > Vertical {
        width: 44;
        height: 6;
        border: round $accent;
        padding: 1 2;
    }
    """

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._prompt)
            yield Input(placeholder="…")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape":
            self.dismiss(None)


class _HelpModal(ModalScreen):
    """Help overlay listing all bindings, generated from CockpitApp.BINDINGS."""

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=False),
        Binding("q", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = """
    _HelpModal {
        align: center middle;
    }
    _HelpModal > Static {
        width: 50;
        border: round $accent;
        padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        # De-duplicate aliased scroll keys: show one row per unique action name.
        seen_actions: set[str] = set()
        lines: list[str] = ["Keyboard Shortcuts", "─" * 34]
        for b in CockpitApp.BINDINGS:
            if not b.description:
                continue
            if b.action in seen_actions:
                continue
            seen_actions.add(b.action)
            lines.append(f"  {b.key:<14} {b.description}")
        lines += ["", "Esc / q — close"]
        yield Static("\n".join(lines))


# ---------------------------------------------------------------------------
# CockpitApp
# ---------------------------------------------------------------------------


class CockpitApp(App):
    """Juggle Cockpit v2."""

    BINDINGS = [
        Binding("q",            "quit",          "Quit"),
        Binding("ctrl+c",       "quit",          "Quit",    show=False),
        Binding("question_mark","help",          "Help"),
        Binding("j",            "scroll_down",   "↓",       show=False),
        Binding("k",            "scroll_up",     "↑",       show=False),
        Binding("down",         "scroll_down",   "↓",       show=False),
        Binding("up",           "scroll_up",     "↑",       show=False),
        Binding("pagedown",     "page_down",     "PgDn",    show=False),
        Binding("pageup",       "page_up",       "PgUp",    show=False),
        Binding("tab",          "cycle_pane",    "Tab",     show=False),
        Binding("s",            "switch",        "Switch"),
        Binding("a",            "ack",           "Ack"),
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
                yield HSplitter("upper", "notifications")
                yield Static("", id="notifications")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Juggle"
        self.sub_title = "Cockpit v2"

        if not self._db.is_active():
            self.notify("Juggle inactive. Run /juggle:start first.", severity="error")
            self.exit(1)
            return

        # Apply settings-driven initial sizes.
        t, a, ag = _COL_RATIOS
        topics_w = int(t * 100)
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
        """a — ack all open action items on a thread by label."""
        def _on_label(label: str | None) -> None:
            if label is None:
                return
            label_up = label.strip().upper()
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

    def action_help(self) -> None:
        """? — show help overlay."""
        self.push_screen(_HelpModal())

    def on_resize(self, event: events.Resize) -> None:
        from juggle_cockpit_view import pick_breakpoint
        bp = pick_breakpoint(event.size)
        try:
            if bp == "wide":
                if self._last_bp != "wide":
                    # Transitioning narrow/medium → wide: restore column widths from config.
                    # Heights (#upper / #notifications) are preserved from HSplitter drag.
                    t, a, ag = _COL_RATIOS
                    topics_w = int(t * 100)
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(db_path: str | None = None) -> None:
    app = CockpitApp(db_path=db_path)
    app.run()


# ---------------------------------------------------------------------------
# Profile harness (--profile mode)
# ---------------------------------------------------------------------------


def _parse_psrecord_log(log_text: str) -> dict:
    """Parse a psrecord log and return summary stats.

    psrecord log format::

        # Elapsed time   CPU (%)     Real (MB)   Virtual (MB)
        0.000            5.0         100.0       500.0
        ...

    Returns a dict with keys: avg_cpu, peak_cpu, rss_start, rss_end,
    rss_growth, peak_rss.  Returns ``{}`` if no data rows are found.
    """
    cpu_vals: list[float] = []
    rss_vals: list[float] = []

    for line in log_text.splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        parts = line.split()
        if len(parts) >= 3:
            try:
                cpu_vals.append(float(parts[1]))
                rss_vals.append(float(parts[2]))
            except ValueError:
                continue

    if not cpu_vals:
        return {}

    return {
        "avg_cpu": sum(cpu_vals) / len(cpu_vals),
        "peak_cpu": max(cpu_vals),
        "rss_start": rss_vals[0],
        "rss_end": rss_vals[-1],
        "rss_growth": rss_vals[-1] - rss_vals[0],
        "peak_rss": max(rss_vals),
    }


def _profile_worker_loop(
    duration: int,
    db_path: str | None = None,
    _tick_fn=None,
) -> int:
    """Run a headless snapshot+render loop for *duration* seconds.

    Each iteration calls ``snapshot(db)`` + ``render_static_from_state`` — the
    same work as the live 1-second tick — without a TTY or Textual App.

    Parameters
    ----------
    duration:
        How many seconds to run.
    db_path:
        Optional path to juggle.db.
    _tick_fn:
        Replacement tick callable (injected by tests).  When ``None`` the
        real snapshot+render cycle is used.

    Returns
    -------
    int
        Number of completed iterations.
    """
    if _tick_fn is not None:
        tick_callable = _tick_fn
    else:
        from juggle_cockpit_model import snapshot as _snapshot
        from juggle_cockpit_view import render_static_from_state

        db = _make_cockpit_db(db_path)

        def _default_tick() -> None:
            state = _snapshot(db)
            render_static_from_state(state)

        tick_callable = _default_tick

    end = time.time() + duration
    iterations = 0
    while time.time() < end:
        tick_start = time.time()
        tick_callable()
        iterations += 1
        elapsed = time.time() - tick_start
        sleep_time = max(0.0, 1.0 - elapsed)
        if sleep_time > 0:
            time.sleep(sleep_time)
    return iterations


def run_profile(duration: int = 60, db_path: str | None = None) -> None:
    """Run the cockpit profiling harness.

    Spawns a headless worker child (``--profile-worker``) that mimics the live
    1-second cockpit tick for *duration* seconds.  Concurrently, ``psrecord``
    (via ``uvx``) samples the child's CPU and RSS every 0.5 s.  After both
    finish the log is parsed and a summary printed to stdout.

    Degrades gracefully if ``uvx``/``psrecord`` are unavailable — exits 0 with
    a clear message so CI is not broken.
    """
    log_path = Path("/tmp/cockpit_profile.log")
    plot_path = Path("/tmp/cockpit_profile.png")

    # --- spawn worker child ------------------------------------------------
    cockpit_script = str(Path(__file__).resolve())
    worker_cmd = [
        "uv", "run", cockpit_script,
        "--profile-worker",
        "--duration", str(duration),
    ]
    if db_path:
        worker_cmd += ["--db", db_path]

    print(f"[profile] Starting headless worker ({duration}s) …", flush=True)
    try:
        child = subprocess.Popen(
            worker_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        print("ERROR: 'uv' not found — cannot spawn worker process.", file=sys.stderr)
        sys.exit(1)

    pid = child.pid
    print(f"[profile] Worker PID: {pid}", flush=True)

    # --- start psrecord via uvx --------------------------------------------
    psrecord_cmd = [
        "uvx", "psrecord", str(pid),
        "--interval", "0.5",
        "--duration", str(duration + 2),
        "--plot", str(plot_path),
        "--log", str(log_path),
    ]
    print(f"[profile] Running: {' '.join(psrecord_cmd)}", flush=True)
    psrecord_ok = True
    psrecord_proc: subprocess.Popen | None = None
    try:
        psrecord_proc = subprocess.Popen(
            psrecord_cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        psrecord_ok = False
        print("[profile] WARNING: 'uvx' not found — skipping psrecord sampling.", flush=True)

    # --- wait for worker ---------------------------------------------------
    try:
        child.wait(timeout=duration + 15)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()

    # --- wait for psrecord -------------------------------------------------
    if psrecord_ok and psrecord_proc is not None:
        try:
            psrecord_proc.wait(timeout=duration + 15)
        except subprocess.TimeoutExpired:
            psrecord_proc.kill()
            psrecord_proc.wait()

    # --- print summary -----------------------------------------------------
    if not psrecord_ok or not log_path.exists():
        print(
            "\n[profile] psrecord log not available"
            " (uvx/psrecord not installed or failed).",
            flush=True,
        )
        print("[profile] Install: pip install psrecord  (no restart needed)", flush=True)
        print("[profile] Profiling run complete (no metrics collected).", flush=True)
        return

    try:
        log_text = log_path.read_text()
    except OSError as exc:
        print(f"[profile] ERROR reading log: {exc}", file=sys.stderr)
        sys.exit(1)

    stats = _parse_psrecord_log(log_text)
    if not stats:
        print("[profile] WARNING: psrecord log is empty or unparseable.", flush=True)
        return

    w = 52
    print(f"\n{'=' * w}")
    print("  Cockpit Profile Summary")
    print(f"{'=' * w}")
    print(f"  CPU avg:    {stats['avg_cpu']:.1f}%")
    print(f"  CPU peak:   {stats['peak_cpu']:.1f}%")
    print(f"  RSS start:  {stats['rss_start']:.1f} MB")
    print(f"  RSS end:    {stats['rss_end']:.1f} MB")
    print(f"  RSS growth: {stats['rss_growth']:+.1f} MB")
    print(f"  RSS peak:   {stats['peak_rss']:.1f} MB")
    print(f"{'=' * w}")

    if stats["rss_growth"] > 20.0:
        print(
            f"  ⚠  POSSIBLE LEAK: RSS grew {stats['rss_growth']:.1f} MB"
            f" (threshold: 20 MB)"
        )
    if stats["avg_cpu"] > 15.0:
        print(
            f"  ⚠  BATTERY CONCERN: avg CPU {stats['avg_cpu']:.1f}%"
            f" (threshold: 15%)"
        )

    print(f"\n  Plot: {plot_path}")


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
    args = parser.parse_args()
    if args.out:
        from juggle_cockpit_view import render_static
        sys.stdout.write(render_static(db_path=args.db_path))
        sys.exit(0)
    if args.profile_worker:
        _profile_worker_loop(args.duration, db_path=args.db_path)
        sys.exit(0)
    if args.profile:
        run_profile(duration=args.duration, db_path=args.db_path)
        sys.exit(0)
    run(db_path=args.db_path)
