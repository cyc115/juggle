"""apply_event Namespace-shape contract for spool replay (T-fix-spool-apply-
agent-complete-shape).

Regression scope — 2026-07-02 ~04:39 incident: the first LIVE spool drain
dead-lettered agent_complete events with

    AttributeError during replay of agent_complete:
    'Namespace' object has no attribute 'result_summary'

Root cause: juggle_spool_apply._NS_DEFAULTS did not carry every attribute the
replayed cmd_* handlers read, so an event whose args omitted an optional key
(e.g. the empty-args junk fixtures) raised AttributeError BEFORE the handler's
own validation could dead-letter it cleanly. Same defect class as f0dbe11 but on
the agent_complete/agent_fail path.

These tests pin: (a) the real captured dead event replays cleanly, (b) empty-args
junk dead-letters WITHOUT AttributeError, (c) a shared shape contract — every
writer's emitted arg keys are provided by the applier Namespace — so future
drift fails in CI, not in production dead-letters.

Spool isolation: the `spool` fixture monkeypatches juggle_spool_apply.spool_dir
to a tmp dir — these tests NEVER touch the real ~/.juggle/spool.
"""
import os

import pytest

from dbops.spool import write_event, read_pending
from juggle_db import JuggleDB
from juggle_spool_apply import _NS_DEFAULTS, apply_event, drain_spool


@pytest.fixture
def db():
    d = JuggleDB(os.environ["JUGGLE_DB_PATH"])
    d.init_db()
    return d


@pytest.fixture
def spool(tmp_path, monkeypatch):
    s = tmp_path / "spool"
    s.mkdir()
    monkeypatch.setattr("juggle_spool_apply.spool_dir", lambda: s)
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    monkeypatch.setenv("JUGGLE_ORCHESTRATOR", "1")
    return s


# --- Ground-truth writer arg shapes (read verbatim from the committed writers) --
# Keys = the args dict each writer emits on its spool early-return. Update this
# ONLY alongside the corresponding writer; the contract test below asserts the
# applier Namespace supplies every one of these keys.
WRITER_ARG_KEYS = {
    # cmd_complete_agent (juggle_cmd_agents_complete.py)
    "agent_complete": {"thread_id", "result_summary", "retain_text",
                       "open_questions", "handoff", "role"},
    # cmd_fail_agent (juggle_cmd_agents_complete.py)
    "agent_fail": {"thread_id", "error", "failure_type", "max_retries",
                   "recovery_dispatched"},
    # cmd_request_action (juggle_cmd_agents.py) — thread_id rides the event top level
    "action_create": {"thread_id", "message", "type", "priority"},
    # cmd_ack_action (juggle_cmd_agents.py)
    "action_ack": {"action_id"},
    # cmd_notify (juggle_cmd_agents.py) — thread_id rides the event top level
    "action_notify": {"thread_id", "message"},
    # cmd_graph_mark_task (juggle_cmd_graph.py)
    "graph_mark_task": {"task_id", "fail", "handoff"},
}


@pytest.mark.parametrize("event_type,writer_keys", sorted(WRITER_ARG_KEYS.items()))
def test_applier_namespace_covers_every_writer_arg_key(event_type, writer_keys):
    """Shared shape contract: for EVERY spool event type, the applier's
    reconstructed Namespace must expose every key the writer emits — even for an
    empty-args event. The applier provides `_NS_DEFAULTS ∪ {thread_id} ∪ args`;
    with empty args that is `_NS_DEFAULTS ∪ {thread_id}`, so writer keys must be
    a subset of the defaults (thread_id is always seeded from the top level).
    A writer that grows a new arg without a matching default fails HERE, loudly,
    instead of dead-lettering in production (2026-07-02)."""
    provided = set(_NS_DEFAULTS) | {"thread_id"}
    missing = writer_keys - provided
    assert not missing, (
        f"{event_type}: applier Namespace missing writer keys {missing} — "
        f"add them to _NS_DEFAULTS in juggle_spool_apply.py"
    )


def test_empty_args_agent_complete_dead_letters_without_attributeerror(db, spool):
    """Regression pin — the 2026-07-02 junk fixtures
    (~/.juggle/spool/dead/…-a-6abecaa0.json / …-a-13a80a09.json) verbatim:
    type=agent_complete, args={}. Before the fix this raised
    'AttributeError … no attribute result_summary'. After, the empty Namespace
    is fully seeded and the event degrades to the handler's own thread-not-found
    validation (SystemExit) → a CLEAN dead-letter, never an AttributeError."""
    write_event(spool, "agent_complete", "a", "t", {})
    event = read_pending(spool)[0]
    ok, msg = apply_event(db, event)
    assert ok is False
    assert "has no attribute" not in msg
    assert "AttributeError" not in msg


def test_real_captured_agent_complete_dead_event_applies(db, spool):
    """Regression pin — BK's real completion for T-spool-09
    (~/.juggle/spool/dead/20260702T091916.706770-noagent-40f249d3.json). Args
    copied verbatim EXCEPT thread_id, which is rebound to a thread created in
    this tmp DB (the captured uuid belonged to BK's worktree, absent here).
    Pins that the FULL agent_complete writer shape replays cleanly."""
    tid = db.create_thread("T-spool-09 topic", session_id="s")
    args = {
        "thread_id": tid,
        "result_summary": (
            "T-spool-09: wired drain_spool into watchdog tick (_poll_once) and "
            "startup (main, drain-on-start). Full suite green (3187 passed, 20 "
            "skipped, 2 deselected), doctor --dry-run clean."
        ),
        "retain_text": (
            "juggle_watchdog_daemon.py imports drain_spool from juggle_spool_apply; "
            "called (guarded try/except) at top of _poll_once every tick and once in "
            "main() after db.init_db()/cleanup_watchdog_events() before the while loop."
        ),
        "open_questions": None,
        "handoff": None,
        "role": None,
    }
    write_event(spool, "agent_complete", "", tid, args)
    stats = drain_spool(db)
    assert stats["applied"] == 1
    assert stats["dead"] == 0


def test_agent_fail_empty_args_no_attributeerror(db, spool):
    """Same defect class on the agent_fail path: an empty-args agent_fail must
    not AttributeError on args.error before its own validation runs."""
    write_event(spool, "agent_fail", "a", "t", {})
    event = read_pending(spool)[0]
    ok, msg = apply_event(db, event)
    assert "has no attribute" not in msg


def test_dead_letter_action_items_are_capped_not_one_per_junk_event(db, spool):
    """A burst of junk events must NOT file one HIGH action item each — the
    overflow collapses into a single grouped alert (2026-07-02: the empty-args
    fixtures would otherwise flood the cockpit)."""
    n = 6  # > _DEAD_ACTION_ITEM_CAP
    for _ in range(n):
        write_event(spool, "action_notify", "a", "t", {})  # missing 'message' → dead
    stats = drain_spool(db)
    assert stats["dead"] == n
    spool_items = [
        i for i in db.get_open_action_items()
        if "spool" in (i.get("message") or "").lower()
    ]
    assert len(spool_items) == 1
    assert str(n) in spool_items[0]["message"]
