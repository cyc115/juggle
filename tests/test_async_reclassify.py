"""Async LLM retroactive-reclassify layer (2026-07-22).

Spec: research/2026-07-22-async-reclassify-spec.md. Covers:
  - the move_messages primitive (MessagesMixin) — the spec's resolved BLOCKER
  - juggle_watchdog_reclassify.run_reclassify_sweep: cadence gate, settle
    window, watermark monotonicity, move/stay/new decisions, guarded new-topic
    creation, and fail-safe watermark handling on LLM failure.

All LLM calls are injected via `llm_fn` — no network in these tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


@pytest.fixture
def db(tmp_path):
    from juggle_db import JuggleDB

    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


def _mk_thread(db, topic="feature work"):
    return db.create_thread(topic, session_id="s1")


# ---------------------------------------------------------------------------
# move_messages primitive
# ---------------------------------------------------------------------------


def test_move_messages_reassigns_thread(db):
    src = _mk_thread(db, "src topic")
    dest = _mk_thread(db, "dest topic")
    db.add_message(src, "user", "message one")
    db.add_message(src, "user", "message two")

    src_msgs = db.get_messages(src, token_budget=10_000)
    ids = [m["id"] for m in src_msgs]

    moved = db.move_messages(ids, dest)

    assert moved == 2
    assert db.get_message_count(src, exclude_junk=False) == 0
    assert db.get_message_count(dest, exclude_junk=False) == 2
    dest_node = db.get_thread(dest)
    assert dest_node["last_active_at"] is not None
