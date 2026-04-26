#!/usr/bin/env python3
# /// script
# dependencies = [
#   "rich",
# ]
# ///
"""Juggle Cockpit — three-column live terminal dashboard.

Display-only. Never writes to DB. Never calls subprocess.

Run:  uv run src/juggle_cockpit.py
Exit: Ctrl-C
"""

import select
import shutil
import signal
import sys
import termios
import threading
import time
import tty


from juggle_db import JuggleDB
from juggle_settings import get_settings as _get_settings

REFRESH_INTERVAL: float = _get_settings()["cockpit"]["refresh_interval_secs"]

_last_reap_time = 0

# ---------------------------------------------------------------------------
# Scroll state — keyboard-driven viewport offsets per pane
# ---------------------------------------------------------------------------

_SCROLL_PANES = ("actions", "agents", "notifications")


class _ScrollState:
    """Thread-safe per-pane scroll offsets + active pane, driven by keyboard.

    Key bindings (when cockpit is in focus):
      ↑ / k    scroll active pane up
      ↓ / j    scroll active pane down
      Tab      cycle active pane
    """

    def __init__(self):
        self._offsets: dict[str, int] = {p: 0 for p in _SCROLL_PANES}
        self._active: str = "notifications"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="cockpit-keys")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> tuple[dict[str, int], str]:
        """Return (offsets_copy, active_pane) atomically."""
        with self._lock:
            return dict(self._offsets), self._active

    def clamp(self, pane: str, max_offset: int) -> None:
        """Clamp pane offset to [0, max_offset]."""
        with self._lock:
            self._offsets[pane] = min(self._offsets[pane], max(0, max_offset))

    def _adjust(self, delta: int) -> None:
        with self._lock:
            pane = self._active
            self._offsets[pane] = max(0, self._offsets[pane] + delta)

    def _cycle(self) -> None:
        with self._lock:
            idx = _SCROLL_PANES.index(self._active)
            self._active = _SCROLL_PANES[(idx + 1) % len(_SCROLL_PANES)]

    def _run(self) -> None:
        if not sys.stdin.isatty():
            return
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            # setcbreak: character-at-a-time, but keeps ISIG so Ctrl-C still works
            tty.setcbreak(fd)
            while not self._stop.is_set():
                ready, _, _ = select.select([sys.stdin], [], [], 0.1)
                if not ready:
                    continue
                ch = sys.stdin.read(1)
                if ch == "\x1b":
                    # Read the rest of the escape sequence (non-blocking)
                    r2, _, _ = select.select([sys.stdin], [], [], 0.05)
                    seq = sys.stdin.read(2) if r2 else ""
                    if seq == "[A":    # up arrow
                        self._adjust(-1)
                    elif seq == "[B":  # down arrow
                        self._adjust(+1)
                elif ch in ("k", "K"):
                    self._adjust(-1)
                elif ch in ("j", "J"):
                    self._adjust(+1)
                elif ch == "\t":
                    self._cycle()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---------------------------------------------------------------------------
# Rich-based tick — model/view layer (Tasks 13-14)
# ---------------------------------------------------------------------------

def tick(
    db,
    size,
    prev_layout,
    prev_bp,
    prev_topics_count=0,
    scroll_offsets=None,
    active_pane=None,
):
    """One cockpit tick: snapshot DB → pick breakpoint → render into layout.

    Returns (layout, bp, topics_count). Reuses prev_layout when breakpoint
    and topic count are both unchanged.
    scroll_offsets and active_pane are forwarded to render_into.
    """
    from juggle_cockpit_model import snapshot as _snapshot
    from juggle_cockpit_view import pick_breakpoint as _pick_bp, build_layout as _build_layout, render_into as _render_into

    bp = _pick_bp(size)
    state = _snapshot(db)
    topics_count = len(state.topics) if state is not None else 0
    if prev_layout is None or prev_bp != bp or prev_topics_count != topics_count:
        layout = _build_layout(bp, topics_count)
    else:
        layout = prev_layout

    _render_into(layout, state, bp, scroll_offsets=scroll_offsets, active_pane=active_pane)
    return layout, bp, topics_count


def _throttled_reaper(db, mgr, throttle_secs=60):
    """Reap agents, throttled to once per throttle_secs."""
    global _last_reap_time
    now = time.time()
    if now - _last_reap_time >= throttle_secs:
        from juggle_tmux import reap_stale_agents
        reap_stale_agents(db, mgr)
        _last_reap_time = now


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _make_cockpit_db(db_path: str | None = None) -> JuggleDB:
    """Create a JuggleDB with a persistent connection for cockpit use.

    Normal JuggleDB._connect() creates a new connection each call. In a 1s
    refresh loop that leaks file descriptors. We cache one connection and
    return it on every _connect() call.
    """
    import sqlite3 as _sqlite3

    db = JuggleDB(db_path=db_path)
    db.init_db()  # run migrations before monkey-patching the connection
    conn = _sqlite3.connect(str(db.db_path))
    conn.row_factory = _sqlite3.Row
    db._connect = lambda: conn  # noqa: E731 — intentional monkey-patch
    return db


def run(db_path: str | None = None) -> None:
    """Start the cockpit refresh loop using Rich Live."""
    from rich.live import Live
    from rich.console import Console

    db = _make_cockpit_db(db_path)
    if not db.is_active():
        print("Juggle inactive. Run /juggle:start first.")
        sys.exit(1)

    try:
        from juggle_tmux import JuggleTmuxManager
        _cockpit_mgr = JuggleTmuxManager()
    except Exception:
        _cockpit_mgr = None

    layout = None
    bp = None
    topics_count = 0
    scroll = _ScrollState()
    scroll.start()
    console = Console()

    try:
        with Live(console=console, screen=True, refresh_per_second=1) as live:
            while True:
                try:
                    size = console.size
                    if _cockpit_mgr is not None:
                        _throttled_reaper(db, _cockpit_mgr)
                    offsets, active = scroll.snapshot()
                    layout, bp, topics_count = tick(
                        db, size, layout, bp, topics_count,
                        scroll_offsets=offsets, active_pane=active,
                    )
                    live.update(layout)
                except Exception as e:
                    from rich.text import Text
                    live.update(Text(f"[error] {e}", style="red"))
                time.sleep(REFRESH_INTERVAL)
    finally:
        scroll.stop()


if __name__ == "__main__":
    import argparse

    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    parser = argparse.ArgumentParser(description="Juggle Cockpit dashboard")
    parser.add_argument("--db", dest="db_path", default=None,
                        help="Path to juggle.db file")
    args = parser.parse_args()
    run(db_path=args.db_path)
