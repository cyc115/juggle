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
from dbops.event_kinds import AGENT_COMPLETE, LEGACY
from juggle_settings import get_settings

_COALESCE_THRESHOLD = 3  # >N same-kind rows in one poll cycle -> one summary line

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
) -> tuple[list[tuple[str | None, str]], int]:
    """Query new pushable events (T1a: handled_by != 'watchdog').

    Returns ([(thread_id_or_None, output_line)], new_last_seen_id). Kind
    AGENT_COMPLETE/LEGACY on a 'done' conversation thread keeps the exact
    back-compat `[LABEL] role: title` line (matches the pre-T1b behavior);
    every other pushable kind prints its notification message as-is. More
    than _COALESCE_THRESHOLD same-kind rows in one cycle collapse into a
    single summary line (thread_id=None, so it bypasses per-thread dedup).
    """
    rows = conn.execute(
        """
        SELECT n.id, n.thread_id, n.message, n.kind, n.handled_by,
               t.user_label, t.title, t.state
        FROM notifications_v2 n
        LEFT JOIN nodes t ON n.thread_id = t.id AND t.kind='conversation'
        WHERE n.id > ?
        ORDER BY n.id
        """,
        (last_seen_id,),
    ).fetchall()

    from collections import Counter

    counts = Counter(row["kind"] for row in rows if row["handled_by"] != "watchdog")
    coalesced_kinds = {k for k, n in counts.items() if n > _COALESCE_THRESHOLD}
    emitted_coalesce_line: set[str] = set()
    results: list[tuple[str | None, str]] = []

    for row in rows:
        if row["handled_by"] == "watchdog":
            last_seen_id = row["id"]
            continue
        kind = row["kind"]
        if kind in coalesced_kinds:
            if kind not in emitted_coalesce_line:
                emitted_coalesce_line.add(kind)
                results.append((None, f"⬢ {counts[kind]} × {kind} events"))
        elif kind in (AGENT_COMPLETE, LEGACY) and row["state"] == "done":
            role = _role_for_thread(conn, row["thread_id"])
            label = row["user_label"] or "?"
            title = row["title"] or "?"
            results.append((row["thread_id"], f"[{label}] {role}: {title}"))
        elif kind in (AGENT_COMPLETE, LEGACY):
            pass  # not a done conversation thread — matches pre-T1b behavior (skip)
        else:
            results.append((None, row["message"]))
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


def run_once(db_path: Path | None = None, cursor_path: Path | None = None) -> None:
    """One-shot poll: print unconsumed events since the shared cursor, then return.

    Cron fallback for machines where the Monitor tool is unavailable (telemetry
    disabled). Reuses the exact ``_poll_once`` event surface and the SAME
    per-session cursor file as the streaming daemon (``_cursor_for``/
    ``_load_cursor``/``_save_cursor``), so a --once poll and the streaming
    daemon never double-emit one event. Silent (no output) when nothing new.
    db_path/cursor_path are injectable for tests; default to the real
    per-session paths for CLI use.
    """
    if db_path is None:
        db_path = _db_path()
    if cursor_path is None:
        cursor_path = _cursor_for(_session_id())

    last_seen_id = _load_cursor(cursor_path, db_path)
    try:
        from juggle_db_connect import open_connection
        conn = open_connection(db_path)
    except sqlite3.OperationalError:
        return

    results, new_last_seen_id = _poll_once(conn, last_seen_id)
    conn.close()

    for _thread_id, line in results:
        print(line, flush=True)

    if new_last_seen_id != last_seen_id:
        _save_cursor(cursor_path, new_last_seen_id)


def _handle_term(_signum, frame) -> None:
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
                # thread_id=None covers threadless/coalesced events — always
                # print (nothing to dedupe against); completion lines dedupe
                # by thread_id as before.
                if thread_id is None or thread_id not in emitted:
                    if thread_id is not None:
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
