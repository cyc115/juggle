"""juggle_integrate_selfrepo — self-repo daemon restart after integrate.

Owns: restarting juggle's own watchdog/talkback daemons (and killing the
stale monitor) after a ff-merge into juggle's own repo.
Must not own: the integration pipeline (juggle_cmd_integrate) or lock
handling (juggle_integrate_lock).

Extracted verbatim from juggle_cmd_integrate (2026-06-10, autopilot Phase 3
mechanical split). Tests keep patching
``juggle_cmd_integrate._restart_juggle_daemons`` — the call site resolves the
name through that module's globals.
"""

from __future__ import annotations

import os
import signal
import sys
from pathlib import Path


def _restart_juggle_daemons() -> None:
    """Restart watchdog + talkback after a ff-merge of juggle's own repo.

    Also kills the stale monitor process (VZ singleton fix) — next /juggle:start
    re-spawns it via Claude Code's Monitor tool.
    """
    try:
        from juggle_cmd_threads import _start_watchdog, _maybe_start_talkback
        _start_watchdog()
        _maybe_start_talkback()
    except Exception as e:
        print(
            f"[juggle] WARNING: watchdog restart after self-integrate failed: {e}",
            file=sys.stderr,
        )
    # Kill stale monitor (VZ singleton hygiene) — next /juggle:start re-spawns it
    try:
        monitor_pidfile = Path.home() / ".juggle" / "monitor.pid"
        if monitor_pidfile.exists():
            parts = monitor_pidfile.read_text().strip().splitlines()
            if parts:
                old_pid = int(parts[0])
                if old_pid != os.getpid():
                    try:
                        os.kill(old_pid, 0)  # alive check
                        os.kill(old_pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass
    except Exception:
        pass
