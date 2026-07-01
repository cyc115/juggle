"""Node-aware topic summary (pins '2026-06-30 node-aware summary').

Two-part feature:
(1) build_summary_ctx loads the topic's child task-nodes and the summarizer
    prompt gains a 'Sub-tasks:' block so the summary reflects node progress.
(2) The topic-summary cache staleness fingerprint = f(message cursor,
    child_node_signature). ANY node development (state/updated_at change,
    add/remove) OR a new message invalidates the cached summary and forces
    regeneration on next access. Pure/deterministic — no event wiring.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_topic_summary import build_summarize_prompt
from juggle_topic_summary_cache import (
    child_node_signature,
    current_cursor,
    decide_summary_action,
    load_cached_sections,
    store_summary,
)

_SECTIONS = {"context": "c", "why": "w", "what": "h", "result": "r"}


# ── child_node_signature (pure) ──────────────────────────────────────────────

def test_signature_empty_when_no_children():
    assert child_node_signature([]) == ""
    assert child_node_signature(None) == ""


def test_signature_is_order_independent_and_deterministic():
    a = [
        {"id": "T1", "state": "open", "updated_at": "2026-06-30T00:00:00Z"},
        {"id": "T2", "state": "verified", "updated_at": "2026-06-30T01:00:00Z"},
    ]
    b = list(reversed(a))
    assert child_node_signature(a) == child_node_signature(b)
    assert child_node_signature(a) == child_node_signature(a)


def test_signature_changes_on_state_change():
    before = [{"id": "T1", "state": "open", "updated_at": "t0"}]
    after = [{"id": "T1", "state": "verified", "updated_at": "t0"}]
    assert child_node_signature(before) != child_node_signature(after)


def test_signature_changes_on_updated_at_change():
    before = [{"id": "T1", "state": "open", "updated_at": "t0"}]
    after = [{"id": "T1", "state": "open", "updated_at": "t1"}]
    assert child_node_signature(before) != child_node_signature(after)


def test_signature_changes_on_node_added():
    one = [{"id": "T1", "state": "open", "updated_at": "t0"}]
    two = one + [{"id": "T2", "state": "open", "updated_at": "t0"}]
    assert child_node_signature(one) != child_node_signature(two)


# ── decide_summary_action is node-aware ──────────────────────────────────────

def test_decide_exact_when_cursor_and_signature_match():
    assert decide_summary_action(10, 10, "sigA", "sigA") == "EXACT"


def test_decide_full_when_signature_differs_even_if_cursor_same():
    assert decide_summary_action(10, 10, "sigA", "sigB") == "FULL"


def test_decide_full_when_cursor_advances_even_if_signature_same():
    assert decide_summary_action(10, 17, "sigA", "sigA") == "FULL"


def test_decide_backward_compatible_without_signatures():
    # Legacy 2-arg call (message-only) still behaves as before.
    assert decide_summary_action(10, 10) == "EXACT"
    assert decide_summary_action(10, 17) == "FULL"


# ── prompt gains a Sub-tasks block ───────────────────────────────────────────

def test_prompt_includes_subtasks_block():
    meta = {
        "label": "GU",
        "title": "Topic",
        "status": "open",
        "child_nodes": [
            {"id": "T1", "title": "Do thing", "state": "verified"},
            {"id": "T2", "title": "Other", "state": "open"},
        ],
    }
    prompt = build_summarize_prompt("in", "out", [], meta)
    assert "Sub-tasks:" in prompt
    assert "[T1 — Do thing — verified]" in prompt
    assert "[T2 — Other — open]" in prompt


def test_prompt_omits_subtasks_block_when_no_children():
    prompt = build_summarize_prompt("in", "out", [], {"label": "GU"})
    assert "Sub-tasks:" not in prompt


# ── build_summary_ctx loads child task-nodes ─────────────────────────────────

def _juggle_db(tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    return db


def _insert_task_node(db, node_id, parent_id, state, title="t", updated_at="2026-06-30T00:00:00Z"):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO nodes (id, kind, title, state, parent_id, agent_result, "
            "created_at, updated_at) VALUES (?, 'task', ?, ?, ?, ?, ?, ?)",
            (node_id, title, state, parent_id, "res-" + node_id, "2026-06-30T00:00:00Z", updated_at),
        )
        conn.commit()


def test_build_summary_ctx_includes_child_nodes(tmp_path):
    from juggle_cockpit_modals import build_summary_ctx
    db = _juggle_db(tmp_path)
    tid = db.create_thread("Topic", session_id="")
    db.add_message(tid, "user", "hi")
    _insert_task_node(db, "T1", tid, "verified", title="First")
    _insert_task_node(db, "T2", tid, "open", title="Second")

    ctx = build_summary_ctx(db, tid)
    children = ctx.get("child_nodes")
    assert children is not None and len(children) == 2
    ids = {c["id"] for c in children}
    assert ids == {"T1", "T2"}
    by_id = {c["id"]: c for c in children}
    assert by_id["T1"]["state"] == "verified"
    assert by_id["T1"]["title"] == "First"


# ── node-aware cache invalidation (L1 + L2 round-trip) ───────────────────────

def test_node_change_invalidates_cached_summary(tmp_path):
    db = _juggle_db(tmp_path)
    tid = db.create_thread("Topic", session_id="")
    db.add_message(tid, "user", "hi")
    with db._connect() as conn:
        cur = current_cursor(conn, tid)

    sig_a = child_node_signature([{"id": "T1", "state": "open", "updated_at": "t0"}])
    store_summary(db, tid, cur, _SECTIONS, {}, sig_a)

    # Same signature → EXACT hit (fresh L1 dict, cold cache) reads from L2.
    sections, _ = load_cached_sections(db, tid, cur, {}, sig_a)
    assert sections == _SECTIONS

    # Node developed → new signature → miss → regeneration required.
    sig_b = child_node_signature([{"id": "T1", "state": "verified", "updated_at": "t1"}])
    miss, _ = load_cached_sections(db, tid, cur, {}, sig_b)
    assert miss is None


def test_l1_key_includes_signature(tmp_path):
    db = _juggle_db(tmp_path)
    l1: dict = {}
    store_summary(db, "t9", 4, _SECTIONS, l1, "sigX")
    assert l1.get(("t9", 4, "sigX")) == _SECTIONS
