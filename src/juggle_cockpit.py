#!/usr/bin/env python3
"""Juggle Cockpit — three-column live terminal dashboard.

Display-only. Never writes to DB. Never calls subprocess.

Run:  python3 src/juggle_cockpit.py
Exit: Ctrl-C
"""

import shutil
import signal
import sys
import time

from juggle_db import JuggleDB
from juggle_settings import get_settings as _get_settings

REFRESH_INTERVAL: float = _get_settings()["cockpit"]["refresh_interval_secs"]

_last_reap_time = 0


# ---------------------------------------------------------------------------
# Rich-based tick — model/view layer (Tasks 13-14)
# ---------------------------------------------------------------------------

def tick(db, size, prev_layout, prev_bp):
    """One cockpit tick: snapshot DB → pick breakpoint → render into layout.

    Returns (layout, bp). Reuses prev_layout when breakpoint is unchanged.
    """
    from juggle_cockpit_model import snapshot as _snapshot
    from juggle_cockpit_view import pick_breakpoint as _pick_bp, build_layout as _build_layout, render_into as _render_into

    bp = _pick_bp(size)
    if prev_layout is None or prev_bp != bp:
        layout = _build_layout(bp)
    else:
        layout = prev_layout

    state = _snapshot(db)
    _render_into(layout, state, bp)
    return layout, bp


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
    console = Console()

    with Live(console=console, screen=True, refresh_per_second=1) as live:
        while True:
            try:
                size = console.size
                if _cockpit_mgr is not None:
                    _throttled_reaper(db, _cockpit_mgr)
                layout, bp = tick(db, size, layout, bp)
                live.update(layout)
            except Exception as e:
                from rich.text import Text
                live.update(Text(f"[error] {e}", style="red"))
            time.sleep(REFRESH_INTERVAL)


if __name__ == "__main__":
    import argparse

    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    parser = argparse.ArgumentParser(description="Juggle Cockpit dashboard")
    parser.add_argument("--db", dest="db_path", default=None,
                        help="Path to juggle.db file")
    args = parser.parse_args()
    run(db_path=args.db_path)
