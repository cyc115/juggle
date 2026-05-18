import subprocess
import uuid
import datetime
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
from juggle_db import JuggleDB

FIXTURE_DIR = Path(__file__).parent / "fixtures"
TEST_SESSION = "juggle-watchdog-test"


@pytest.fixture(scope="session", autouse=True)
def ensure_tmux_session():
    r = subprocess.run(["tmux", "has-session", "-t", TEST_SESSION], capture_output=True)
    session_existed = r.returncode == 0
    if not session_existed:
        subprocess.run(["tmux", "new-session", "-d", "-s", TEST_SESSION], check=True)
    yield
    # Only destroy the session if we created it — don't kill pre-existing sessions.
    if not session_existed:
        subprocess.run(["tmux", "kill-session", "-t", TEST_SESSION], capture_output=True)


@pytest.fixture
def tmux_pane(ensure_tmux_session):  # noqa: ARG001 — fixture dep, not used in body
    r = subprocess.run(
        ["tmux", "new-window", "-t", TEST_SESSION, "-P", "-F", "#{pane_id}"],
        capture_output=True, text=True, check=True,
    )
    pane_id = r.stdout.strip()
    subprocess.run(
        ["tmux", "resize-pane", "-t", pane_id, "-x", "80", "-y", "24"],
        capture_output=True,
    )
    yield pane_id
    subprocess.run(["tmux", "kill-pane", "-t", pane_id], capture_output=True)


@pytest.fixture
def test_db(tmp_path):
    db = JuggleDB(str(tmp_path / "juggle.db"))
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
            "context_threads, created_at, last_active) VALUES (?,?,?,?,?,?,?,?)",
            (agent_id, "coder", tmux_pane, tid, "busy", "[]", now, now),
        )
    yield {"agent_id": agent_id, "thread_id": tid, "pane_id": tmux_pane}
    with test_db._connect() as conn:
        conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        conn.execute("DELETE FROM action_items WHERE thread_id = ?", (tid,))
        conn.execute("DELETE FROM notifications_v2 WHERE thread_id = ?", (tid,))
        conn.execute("DELETE FROM messages WHERE thread_id = ?", (tid,))
        conn.execute("DELETE FROM threads WHERE id = ?", (tid,))
