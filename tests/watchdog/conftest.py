import subprocess
import time
import uuid
import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
# tests/ on sys.path (added when the top-level tests/conftest.py is imported) so
# the worker-scoped session helper is importable from here too.
sys.path.insert(0, str(Path(__file__).parent.parent))
from juggle_db import JuggleDB
from juggle_watchdog_singleton import PROD_DB_PATH, find_watchdog_pids

from _xdist_isolation import watchdog_session_name

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def test_session_name() -> str:
    """Per-xdist-worker tmux session so parallel workers never collide
    (speedup-tier, 2026-06-21). Resolved at fixture-setup time so a worker that
    imports this module before PYTEST_XDIST_WORKER is set still gets the right
    name."""
    return watchdog_session_name()


TEST_SESSION = test_session_name()  # module-load default; fixtures call the fn


def _wait_pane(pane_id: str, marker: str, timeout: float = 5.0, interval: float = 0.05) -> str:
    """Poll tmux pane content until `marker` appears or timeout. Returns final content."""
    deadline = time.monotonic() + timeout
    content = ""
    while time.monotonic() < deadline:
        r = subprocess.run(
            ["tmux", "capture-pane", "-pt", pane_id],
            capture_output=True, text=True,
        )
        content = r.stdout
        if marker in content:
            return content
        time.sleep(interval)
    return content  # return whatever we got; caller asserts


def _ensure_session(session: str) -> None:
    """Create `session` if it isn't live. Idempotent + tolerant of the
    already-exists race: two callers may both see has-session fail under the
    SHARED tmux server, so a losing new-session ('duplicate session') is fine
    (deflake 2026-07-04)."""
    if subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0:
        return
    subprocess.run(["tmux", "new-session", "-d", "-s", session], capture_output=True)


@pytest.fixture(scope="session", autouse=True)
def ensure_tmux_session():
    session = test_session_name()
    session_existed = (
        subprocess.run(["tmux", "has-session", "-t", session], capture_output=True).returncode == 0
    )
    if not session_existed:
        _ensure_session(session)
    yield
    # Only destroy the session if we created it — don't kill pre-existing sessions.
    if not session_existed:
        subprocess.run(
            ["tmux", "kill-session", "-t", session], capture_output=True
        )


def _new_window(session: str, attempts: int = 6, interval: float = 0.25) -> str:
    """Open a fresh window and return its pane id, resilient to shared-server
    contention (deflake 2026-07-04).

    Under `make test` -n auto every xdist worker hammers the ONE default tmux
    server, so a `new-window` can transiently fail (server busy) or the
    per-worker session can momentarily be missing. The old `check=True` turned
    either transient into a fixture-SETUP ERROR that fail-closed integrate gates
    read as a real failure. We instead re-ensure the session and retry with
    bounded backoff, then fail loud only if EVERY attempt failed."""
    last_err = ""
    for _ in range(attempts):
        _ensure_session(session)
        r = subprocess.run(
            ["tmux", "new-window", "-t", session, "-P", "-F", "#{pane_id}"],
            capture_output=True,
            text=True,
        )
        pane_id = r.stdout.strip()
        if r.returncode == 0 and pane_id:
            return pane_id
        last_err = r.stderr.strip() or f"rc={r.returncode}"
        time.sleep(interval)
    raise RuntimeError(
        f"tmux new-window failed after {attempts} attempts on session "
        f"{session!r}: {last_err}"
    )


@pytest.fixture
def tmux_pane(ensure_tmux_session):  # noqa: ARG001 — fixture dep, not used in body
    session = test_session_name()
    pane_id = _new_window(session)
    subprocess.run(
        ["tmux", "resize-pane", "-t", pane_id, "-x", "80", "-y", "24"],
        capture_output=True,
    )
    yield pane_id
    subprocess.run(["tmux", "kill-pane", "-t", pane_id], capture_output=True)


@pytest.fixture
def test_db(tmp_path):
    db_path = tmp_path / "juggle.db"
    # Sanction guard: never allow tests to target the production DB.
    assert db_path.resolve() != PROD_DB_PATH, (
        f"test_db must use a temp path, not the prod DB {PROD_DB_PATH}"
    )
    db = JuggleDB(str(db_path))
    db.init_db()
    return db


@pytest.fixture
def fake_agent(tmux_pane, test_db):
    agent_id = str(uuid.uuid4())
    tid = test_db.create_thread("watchdog-test-topic", session_id="test-session")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with test_db._connect() as conn:
        conn.execute(
            "INSERT INTO agents (id, role, pane_id, assigned_thread, status, "
            "context_threads, created_at, last_active, last_send_task_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (agent_id, "coder", tmux_pane, tid, "busy", "[]", now, now, now),
        )
    yield {"agent_id": agent_id, "thread_id": tid, "pane_id": tmux_pane}
    with test_db._connect() as conn:
        conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        conn.execute("DELETE FROM action_items WHERE thread_id = ?", (tid,))
        conn.execute("DELETE FROM notifications_v2 WHERE thread_id = ?", (tid,))
        conn.execute("DELETE FROM messages WHERE thread_id = ?", (tid,))
        # P8 terminal: the conversation is a kind='conversation' node (legacy
        # threads table dropped, Migration 55).
        conn.execute("DELETE FROM nodes WHERE id = ?", (tid,))


