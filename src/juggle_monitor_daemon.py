"""Agent-completion monitor daemon — library logic behind scripts/juggle-agent-monitor.

Owns the polling loop that streams completed-agent lines (one per closed
thread, `[LABEL] role: title`) to stdout for the orchestrator's Monitor tool,
plus the monitor's singleton-pidfile hygiene (thin shims over
``daemon_pidfile``). It must not own any tmux or recovery logic — that lives
in the watchdog modules. Entry point: ``main()``, invoked by the thin
``scripts/juggle-agent-monitor`` wrapper.
"""

import atexit
import os
import re
import signal
import sqlite3
import sys
import time
from pathlib import Path

import daemon_pidfile
from juggle_settings import get_settings

_JUGGLE_DIR = Path.home() / ".juggle"
# Defaults; main() reassigns these to the per-session paths (see _session_id).
SINGLETON_PID_FILE = _JUGGLE_DIR / "monitor.pid"
CURSOR_FILE = _JUGGLE_DIR / "monitor.cursor"


def _session_id() -> str:
    """Best-effort orchestrator/session key for the per-session pidfile + cursor.

    Multiple orchestrator instances share one juggle DB; a GLOBAL pidfile made
    them evict each other's monitor (kill-before-restart), and a GLOBAL cursor
    let one instance mark completions delivered that another never emitted
    (cross-instance starvation). Keying both by session id makes monitors from
    different sessions coexist while a same-session re-arm still dedups/resumes.

    Source priority: explicit JUGGLE_MONITOR_SESSION, else the Claude Code
    session id (CLAUDE_CODE_SESSION_ID, set by the launching orchestrator),
    else a stable fallback derived from the parent (launcher) PID. Sanitized to
    a filename-safe token.
    """
    raw = (
        os.environ.get("JUGGLE_MONITOR_SESSION")
        or os.environ.get("CLAUDE_CODE_SESSION_ID")
        or f"ppid{os.getppid()}"
    )
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:64] or "default"


def _pidfile_for(session_id: str) -> Path:
    return _JUGGLE_DIR / f"monitor-{session_id}.pid"


def _cursor_for(session_id: str) -> Path:
    return _JUGGLE_DIR / f"monitor-{session_id}.cursor"


def _db_path() -> Path:
    return Path(get_settings()["paths"]["data_dir"]) / "juggle.db"


def _is_monitor_process(pid: int) -> bool:
    """Return True if the process with given PID is a juggle-agent-monitor."""
    return daemon_pidfile.is_process(pid, "juggle-agent-monitor")


def _kill_existing_monitor_from_pidfile(pidfile_path: Path) -> None:
    """Kill the monitor recorded in pidfile_path — only if it really is a monitor.

    Thin shim over daemon_pidfile.kill_existing_from_pidfile (single source of
    truth): SIGTERM, wait up to 2s, escalate to SIGKILL — silent (no logging).
    Predicate resolved via module globals at call time so tests monkeypatching
    _is_monitor_process keep working.
    """
    daemon_pidfile.kill_existing_from_pidfile(
        pidfile_path,
        lambda pid: _is_monitor_process(pid),
    )


def _write_singleton_pid() -> None:
    daemon_pidfile.write_singleton_pid(SINGLETON_PID_FILE)


def _cleanup_singleton_pid() -> None:
    daemon_pidfile.cleanup_singleton_pid(SINGLETON_PID_FILE)


def _role_for_thread(conn: sqlite3.Connection, thread_id: str) -> str:
    row = conn.execute(
        "SELECT id FROM action_items WHERE thread_id = ? AND type = 'review' "
        "ORDER BY id DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    return "researcher" if row else "coder"


def _poll_once(
    conn: sqlite3.Connection, last_seen_id: int
) -> tuple[list[tuple[str, str]], int]:
    """Query new completions. Returns ([(thread_id, output_line)], new_last_seen_id)."""
    rows = conn.execute(
        """
        SELECT n.id, n.thread_id, t.user_label, t.title
        FROM notifications_v2 n
        JOIN threads t ON n.thread_id = t.id
        WHERE n.id > ? AND t.status = 'closed'
        ORDER BY n.id
        """,
        (last_seen_id,),
    ).fetchall()

    results = []
    for row in rows:
        role = _role_for_thread(conn, row["thread_id"])
        label = row["user_label"] or "?"
        title = row["title"] or "?"
        results.append((row["thread_id"], f"[{label}] {role}: {title}"))
        last_seen_id = row["id"]
    return results, last_seen_id


def _init_cursor(db_path: Path) -> int:
    """Return current max notifications_v2 id so we don't replay history."""
    try:
        from juggle_db_connect import open_connection
        conn = open_connection(db_path)
        row = conn.execute("SELECT MAX(id) AS m FROM notifications_v2").fetchone()
        conn.close()
        return row["m"] if row and row["m"] is not None else 0
    except sqlite3.OperationalError:
        return 0


def _save_cursor(cursor_path: Path, last_id: int) -> None:
    """Atomically persist the last delivered notification id (write tmp + rename)."""
    try:
        cursor_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cursor_path.with_suffix(".cursor.tmp")
        tmp.write_text(str(last_id))
        tmp.replace(cursor_path)
    except OSError:
        pass  # best-effort durability — a missed save just re-emits next restart


def _load_cursor(cursor_path: Path, db_path: Path) -> int:
    """Resume from the persisted cursor; on first run baseline at MAX(id).

    A persisted cursor survives a SIGTERM->relaunch boundary so the daemon
    re-emits exactly the unconsumed completions instead of skipping ahead to
    the current MAX(id) (which would drop completions seen while it was down).
    On the very first run (no cursor file) we baseline at MAX(id) so old
    history is not replayed, and persist that baseline.
    """
    try:
        return int(cursor_path.read_text().strip())
    except (ValueError, OSError):
        baseline = _init_cursor(db_path)
        _save_cursor(cursor_path, baseline)
        return baseline


def _handle_term(signum, frame) -> None:
    """Clean, idempotent shutdown for SIGTERM/SIGINT.

    atexit does NOT run on SIGTERM (Python terminates immediately, exit 143),
    so the harness's expected kill-and-relaunch lifecycle would otherwise leave
    a stale pidfile. Flush stdout, remove our pidfile (only if it still records
    our PID), then exit cleanly so atexit also runs as belt-and-suspenders.
    """
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    _cleanup_singleton_pid()
    sys.exit(0)


def main() -> None:
    # Resolve per-session paths so monitors from different orchestrator sessions
    # coexist (no kill-before-restart eviction) and each keeps its own delivery
    # cursor (no cross-instance starvation). Same-session re-arm reuses both.
    global SINGLETON_PID_FILE, CURSOR_FILE
    session_id = _session_id()
    SINGLETON_PID_FILE = _pidfile_for(session_id)
    CURSOR_FILE = _cursor_for(session_id)

    _kill_existing_monitor_from_pidfile(SINGLETON_PID_FILE)
    _write_singleton_pid()
    atexit.register(_cleanup_singleton_pid)
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    db_path = _db_path()
    last_seen_id = _load_cursor(CURSOR_FILE, db_path)
    emitted: set[str] = set()  # deduplicate by thread_id

    while True:
        try:
            from juggle_db_connect import open_connection
            conn = open_connection(db_path)
            results, new_last_seen_id = _poll_once(conn, last_seen_id)
            conn.close()
            for thread_id, line in results:
                if thread_id not in emitted:
                    emitted.add(thread_id)
                    print(line, flush=True)
            # Advance + persist the cursor ONLY after the lines are flushed, so a
            # SIGTERM->relaunch re-emits unconsumed completions rather than losing them.
            if new_last_seen_id != last_seen_id:
                last_seen_id = new_last_seen_id
                _save_cursor(CURSOR_FILE, last_seen_id)
        except sqlite3.OperationalError:
            pass  # DB locked — retry next tick

        time.sleep(1)
