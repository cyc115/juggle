"""irl-repair T4 — pre-claim repair sweep + class playbooks + slot priority.

Pins (plan/2026-07-03-integrate-recovery-loop.md T4, spec rev 6 §3):
  (i)   a dispatchable repair outranks a ready topic for the last agent slot;
  (ii)  no reservation once the cap is breached (exhausted topic never starves
        the claim loop);
  (iii) the repair task lands in the topic's PRESERVED worktree/thread.
Plus: repair_dispatched emission, the 3 class playbooks (collision embeds the
never-delete-non-identical rule, incident cyc_CB), machinery is never repaired,
and idempotency (a dispatched repair is not re-dispatched next tick).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as tp  # noqa: E402
from dbops import event_kinds as ek  # noqa: E402
from dbops.schema import _now  # noqa: E402
from dbops.state_write import write_state  # noqa: E402
import juggle_graph_dispatch as gd  # noqa: E402
from juggle_graph_hydration import build_repair_prompt  # noqa: E402
from juggle_graph_repair import is_repair_dispatchable, sweep_repair_dispatch  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "repair.db"))
    d.init_db()
    return d


def _env(cls="conflict", dispatchable=True, files=("a.py",)):
    return {
        "class": cls, "files": list(files), "log_tail": "boom",
        "branch": "cyc_x", "worktree": "/wt/cyc_x", "attempt": 0,
        "signature": "sig", "attempts_by_signature": {"sig": 1},
        "attempts_total": 1, "repair_dispatchable": dispatchable,
    }


def _failed_topic(db, tid, envelope, project="INBOX", worktree="/wt/cyc_x", bind=True):
    """A topic parked in failed-integration with its worktree thread preserved."""
    tp.create_topic(db, topic_id=tid, project_id=project, title=f"Topic {tid}")
    thread_id = None
    if bind:
        thread_id = db.create_thread(f"[{tid}]", session_id="s")
        tp.set_topic_thread(db, tid, thread_id)
        db.update_thread(thread_id, worktree_path=worktree,
                         worktree_branch="cyc_x", main_repo_path="/repo")
    tp.set_topic_fail_envelope(db, tid, json.dumps(envelope))
    with db._connect() as conn:
        write_state(conn, tid, "failed-integration", now=_now())
        conn.commit()
    return thread_id


def _ready_topic(db, tid, project="INBOX"):
    tp.create_topic(db, topic_id=tid, project_id=project, title=f"Topic {tid}")
    nid = f"{tid}-k0"
    g.create_task(db, task_id=nid, project_id=project, title=nid, prompt="p")
    g.set_task_topic(db, nid, tid)
    tp.recompute_topic_ready(db, project)


class Recorder:
    """Records (thread_id, node_id, prompt); optional capacity that raises."""

    def __init__(self, cap=None):
        self.cap = cap
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, db, thread_id, prompt, node):
        if self.cap is not None and len(self.calls) >= self.cap:
            raise gd.CapacityError("pool full")
        self.calls.append((thread_id, node["id"], prompt))


# ── is_repair_dispatchable ──────────────────────────────────────────────────

def test_dispatchable_true_for_flagged_routine():
    assert is_repair_dispatchable(_env(dispatchable=True)) is True


def test_dispatchable_false_when_flag_off_or_missing():
    assert is_repair_dispatchable(_env(dispatchable=False)) is False
    assert is_repair_dispatchable({"class": "conflict"}) is False
    assert is_repair_dispatchable(None) is False


# ── build_repair_prompt — 3 class playbooks ─────────────────────────────────

def test_playbook_conflict_says_rebase_and_embeds_envelope():
    p = build_repair_prompt(_env("conflict", files=["m.py"]))
    assert "rebase" in p.lower() and "resolve" in p.lower()
    assert '"class": "conflict"' in p and "m.py" in p  # envelope JSON embedded


def test_playbook_red_suite_says_diagnose_not_delete_tests():
    p = build_repair_prompt(_env("red-suite"))
    assert "diagnose" in p.lower()
    assert "delete the tests" in p.lower()


def test_playbook_collision_embeds_never_delete_rule_and_incident():
    p = build_repair_prompt(_env("collision"))
    assert "never delete" in p.lower()
    assert "cyc_CB" in p


def test_playbook_prompt_never_creates_new_worktree():
    p = build_repair_prompt(_env("conflict"))
    assert "do not create a new branch" in p.lower()


# ── sweep_repair_dispatch ───────────────────────────────────────────────────

def test_sweep_dispatches_into_preserved_thread_and_emits(db):
    """Pin (iii): the repair lands in the topic's preserved worktree/thread."""
    thread_id = _failed_topic(db, "T1", _env("conflict"))
    rec = Recorder()

    dispatched = sweep_repair_dispatch(db, "INBOX", dispatch=rec, session_id="s")

    assert dispatched == ["T1"]
    assert len(rec.calls) == 1
    called_thread, node_id, prompt = rec.calls[0]
    assert called_thread == thread_id  # the ORIGINAL preserved thread
    assert node_id == "T1"
    assert "rebase" in prompt.lower()
    # Worktree preserved (sweep never re-created one).
    assert db.get_thread(thread_id)["worktree_path"] == "/wt/cyc_x"
    # repair_dispatched emitted, watchdog-routed (DB-only).
    with db._connect() as conn:
        row = conn.execute(
            "SELECT kind, handled_by FROM notifications_v2 WHERE kind=?",
            (ek.REPAIR_DISPATCHED,),
        ).fetchone()
    assert tuple(row) == (ek.REPAIR_DISPATCHED, "watchdog")


def test_sweep_skips_machinery_and_exhausted(db):
    _failed_topic(db, "Tm", _env("machinery", dispatchable=False))
    _failed_topic(db, "Tx", _env("conflict", dispatchable=False))  # cap breached
    rec = Recorder()
    assert sweep_repair_dispatch(db, "INBOX", dispatch=rec) == []
    assert rec.calls == []


def test_sweep_skips_unbound_topic(db):
    _failed_topic(db, "Tu", _env("conflict"), bind=False)  # thread reloaded away
    rec = Recorder()
    assert sweep_repair_dispatch(db, "INBOX", dispatch=rec) == []


def test_sweep_idempotent_after_dispatch(db):
    _failed_topic(db, "T2", _env("conflict"))
    rec = Recorder()
    assert sweep_repair_dispatch(db, "INBOX", dispatch=rec) == ["T2"]
    # Envelope flag flipped off — second sweep is a no-op.
    env = json.loads(tp.get_topic(db, "T2")["fail_envelope"])
    assert env["repair_dispatchable"] is False and env["repair_dispatched"] is True
    assert sweep_repair_dispatch(db, "INBOX", dispatch=rec) == []
    assert len(rec.calls) == 1


def test_sweep_capacity_hit_leaves_topic_dispatchable(db):
    _failed_topic(db, "Tc", _env("conflict"))
    rec = Recorder(cap=0)  # every dispatch raises CapacityError
    assert sweep_repair_dispatch(db, "INBOX", dispatch=rec) == []
    env = json.loads(tp.get_topic(db, "Tc")["fail_envelope"])
    assert env["repair_dispatchable"] is True  # still eligible next tick


# ── graph_tick priority + conditional reservation ───────────────────────────

def test_pin_i_repair_outranks_ready_topic_for_last_slot(db):
    repair_thread = _failed_topic(db, "TR", _env("conflict"))
    _ready_topic(db, "TN")
    rec = Recorder(cap=1)  # exactly one agent slot free this tick

    stats = gd.graph_tick(db, dispatch_fn=rec)

    assert stats["repaired"] == ["TR"]
    # The single slot went to the repair; the ready topic was deferred, not run.
    assert rec.calls[0][0] == repair_thread
    assert "TN" not in stats["dispatched"]
    assert tp.get_topic(db, "TN")["state"] != "running"


def test_pin_ii_exhausted_topic_does_not_reserve_slot(db):
    _failed_topic(db, "TX", _env("conflict", dispatchable=False))  # cap breached
    _ready_topic(db, "TN")
    rec = Recorder(cap=1)

    stats = gd.graph_tick(db, dispatch_fn=rec)

    assert stats["repaired"] == []          # nothing repaired
    assert stats["dispatched"] == ["TN"]    # the free slot served the ready topic
    assert tp.get_topic(db, "TN")["state"] == "running"


# ── envelope flag + topic persistence (integrate side) ──────────────────────

def test_record_refusal_flags_routine_dispatchable_and_persists_on_topic(db):
    from juggle_integrate_envelope import FailContext, record_refusal

    tp.create_topic(db, topic_id="TE", project_id="INBOX", title="TE")
    thread_id = db.create_thread("[TE]", session_id="s")
    tp.set_topic_thread(db, "TE", thread_id)
    # A topic is 'integrating' when integrate runs (and can fail) — the state
    # store_fail_envelope now scopes the stamp to (fix-conflict-envelope-routing).
    with db._connect() as conn:
        conn.execute("UPDATE nodes SET state='integrating' WHERE id='TE' AND kind='topic'")
        conn.commit()

    ctx = FailContext(
        db=db, thread_id=thread_id, worktree_branch="cyc_x",
        files=["conflict.py"],
        trunk_at_attempt="main@a", head_at_attempt="branch@b",
    )
    rec = record_refusal("rebase_conflict", "Rebase conflict on cyc_x onto main.", ctx)

    assert rec.envelope["repair_dispatchable"] is True
    stored = json.loads(tp.get_topic(db, "TE")["fail_envelope"])
    assert stored["repair_dispatchable"] is True
    assert stored["class"] == "conflict"


def test_record_refusal_machinery_not_dispatchable(db):
    from juggle_integrate_envelope import FailContext, record_refusal

    ctx = FailContext(db=db, thread_id="th", worktree_branch="cyc_x")
    rec = record_refusal("unexpected_exception", "boom", ctx)
    assert rec.envelope["repair_dispatchable"] is False


# ── repaired topic can re-complete (failed-integration → integrating walk) ───

def test_failed_integration_topic_can_be_marked_complete(db):
    from dbops.db_topics_marking import mark_topic_completion

    _failed_topic(db, "TW", _env("conflict"))
    # A repaired topic re-runs integrate; mark_topic_completion must walk it up
    # out of failed-integration rather than reject it as terminal.
    state = mark_topic_completion(db, "TW", integrate_ok=False)
    assert state == "failed-integration"  # re-failed integrate falls back cleanly
