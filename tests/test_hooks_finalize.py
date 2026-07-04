"""Tests for juggle_hooks_finalize — the AgentStop finalization-enforcement hook.

Regression pin for T-enforce-finalize-stop-hook: a coder agent that finishes
work and idles at the prompt WITHOUT running `agent complete`/`agent fail` must
have its turn-end BLOCKED with a nudge. The hook fires in agent panes only,
does a single spool+DB read, and fails open on any error so a hook exception
can never wedge an agent.
"""

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import juggle_hooks_finalize as jhf
from dbops.spool import write_event
from juggle_spool_paths import spool_dir


class _FakeDB:
    """Minimal stand-in exposing the two reads the hook performs."""

    def __init__(self, agents, threads):
        self._agents = agents
        self._threads = threads

    def get_all_agents(self):
        return self._agents

    def get_thread(self, thread_id):
        return self._threads.get(thread_id)


def _use_db(monkeypatch, db):
    import juggle_hooks_config as cfg

    monkeypatch.setattr(cfg, "get_db", lambda: db)


def _run(data):
    """Invoke handle_agent_stop, capturing (exit_code, stdout)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            jhf.handle_agent_stop(data)
            code = 0
        except SystemExit as exc:
            code = exc.code or 0
    return code, buf.getvalue()


CWD = "/tmp/juggle-juggle-RU"
TID = "11111111-1111-1111-1111-111111111111"


def _busy_agent_db():
    agents = [{"id": "a1", "status": "busy", "assigned_thread": TID}]
    threads = {TID: {"id": TID, "worktree_path": CWD}}
    return _FakeDB(agents, threads)


# ── blocks when in-flight and unfinalized ────────────────────────────────────

def test_blocks_when_thread_in_flight_and_not_finalized(monkeypatch):
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    _use_db(monkeypatch, _busy_agent_db())
    code, out = _run({"cwd": CWD})
    assert code == 0
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "agent complete" in payload["reason"]


# ── does NOT block once finalization was spooled ─────────────────────────────

def test_no_block_when_completion_spooled(monkeypatch):
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    _use_db(monkeypatch, _busy_agent_db())
    write_event(spool_dir(), "agent_complete", "", "", {"thread_id": TID})
    code, out = _run({"cwd": CWD})
    assert code == 0
    assert out.strip() == ""  # allowed through, no block


def test_no_block_when_fail_spooled(monkeypatch):
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    _use_db(monkeypatch, _busy_agent_db())
    write_event(spool_dir(), "agent_fail", "", "", {"thread_id": TID})
    code, out = _run({"cwd": CWD})
    assert out.strip() == ""


# ── agent panes only ─────────────────────────────────────────────────────────

def test_never_blocks_outside_agent_session(monkeypatch):
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    _use_db(monkeypatch, _busy_agent_db())
    code, out = _run({"cwd": CWD})
    assert out.strip() == ""  # orchestrator/main never blocked


# ── nothing in-flight for this cwd ───────────────────────────────────────────

def test_no_block_when_no_busy_agent_for_cwd(monkeypatch):
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    # agent busy on a DIFFERENT worktree
    db = _FakeDB(
        [{"id": "a1", "status": "busy", "assigned_thread": TID}],
        {TID: {"id": TID, "worktree_path": "/tmp/juggle-juggle-OTHER"}},
    )
    _use_db(monkeypatch, db)
    code, out = _run({"cwd": CWD})
    assert out.strip() == ""


def test_no_block_when_agent_already_idle(monkeypatch):
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    db = _FakeDB(
        [{"id": "a1", "status": "idle", "assigned_thread": None}],
        {TID: {"id": TID, "worktree_path": CWD}},
    )
    _use_db(monkeypatch, db)
    code, out = _run({"cwd": CWD})
    assert out.strip() == ""


# ── loop guard ───────────────────────────────────────────────────────────────

def test_stop_hook_active_breaks_loop(monkeypatch):
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")
    _use_db(monkeypatch, _busy_agent_db())
    code, out = _run({"cwd": CWD, "stop_hook_active": True})
    assert out.strip() == ""  # never an infinite block loop


# ── fail-open on any error ───────────────────────────────────────────────────

def test_fail_open_on_db_error(monkeypatch):
    monkeypatch.setenv("JUGGLE_IS_AGENT", "1")

    class _BoomDB:
        def get_all_agents(self):
            raise RuntimeError("db locked")

    _use_db(monkeypatch, _BoomDB())
    code, out = _run({"cwd": CWD})
    assert code == 0
    assert out.strip() == ""  # exception must not wedge the agent
