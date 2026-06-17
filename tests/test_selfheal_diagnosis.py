"""Regression-pin tests for selfheal auto-diagnosis loop.

2026-06-17: _try_claim_diagnosis_slot() was dead code; rows sat open forever.
These tests pin the end-to-end gate without spawning a real agent.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# Task 1 — Config defaults
# ---------------------------------------------------------------------------

def test_selfheal_config_enabled_default_false():
    """selfheal.enabled must default to False — opt-in only.

    2026-06-17: ensure the feature can never auto-activate on existing installs.
    """
    from juggle_settings import get_settings
    settings = get_settings()
    assert settings["selfheal"]["enabled"] is False


def test_selfheal_config_min_count_default_3():
    from juggle_settings import get_settings
    assert get_settings()["selfheal"]["min_count"] == 3


def test_selfheal_config_retention_days_default_14():
    from juggle_settings import get_settings
    assert get_settings()["selfheal"]["retention_days"] == 14


# ---------------------------------------------------------------------------
# Task 2 — get_diagnosis_candidates (DB query)
# ---------------------------------------------------------------------------

def test_get_diagnosis_candidates_returns_open_class_a_above_min(tmp_path):
    """Only open class-A rows with count >= min_count are returned."""
    from juggle_db import JuggleDB
    from juggle_selfheal import get_diagnosis_candidates

    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()

    # Insert: open A count=5 → should appear
    db.dedup_or_insert_error("sig1", "A", "ValueError", "tb", "ep", "{}")
    with db._connect() as conn:
        conn.execute("UPDATE error_events SET count=5 WHERE signature_hash='sig1'")
        conn.commit()

    # Insert: open A count=2 → below min_count=3, should NOT appear
    db.dedup_or_insert_error("sig2", "A", "TypeError", "tb", "ep", "{}")
    with db._connect() as conn:
        conn.execute("UPDATE error_events SET count=2 WHERE signature_hash='sig2'")
        conn.commit()

    # Insert: open B count=5 → class B, should NOT appear
    db.dedup_or_insert_error("sig3", "B", None, "tb", "tool", "{}", juggle_ref="ref")
    with db._connect() as conn:
        conn.execute("UPDATE error_events SET count=5 WHERE signature_hash='sig3'")
        conn.commit()

    # Insert: diagnosing A count=5 → not 'open', should NOT appear
    db.dedup_or_insert_error("sig4", "A", "KeyError", "tb", "ep", "{}")
    with db._connect() as conn:
        conn.execute("UPDATE error_events SET count=5, status='diagnosing' WHERE signature_hash='sig4'")
        conn.commit()

    rows = get_diagnosis_candidates(db, min_count=3)
    assert len(rows) == 1
    assert rows[0]["signature_hash"] == "sig1"


def test_get_diagnosis_candidates_ordered_by_count_desc(tmp_path):
    from juggle_db import JuggleDB
    from juggle_selfheal import get_diagnosis_candidates

    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()

    for sig, cnt in [("s1", 3), ("s2", 10), ("s3", 5)]:
        db.dedup_or_insert_error(sig, "A", "E", "tb", "ep", "{}")
        with db._connect() as conn:
            conn.execute(f"UPDATE error_events SET count={cnt} WHERE signature_hash='{sig}'")
            conn.commit()

    rows = get_diagnosis_candidates(db, min_count=3)
    counts = [r["count"] for r in rows]
    assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------------------------
# Task 3 — select_diagnosis_candidate (pure gate)
# ---------------------------------------------------------------------------

def test_select_returns_none_when_disabled():
    from juggle_selfheal import select_diagnosis_candidate
    row = {"id": 1, "count": 10}
    assert select_diagnosis_candidate([], in_flight_exists=False, enabled=False) is None


def test_select_returns_none_when_in_flight():
    from juggle_selfheal import select_diagnosis_candidate
    row = {"id": 1, "count": 10}
    assert select_diagnosis_candidate([row], in_flight_exists=True, enabled=True) is None


def test_select_returns_none_when_no_rows():
    from juggle_selfheal import select_diagnosis_candidate
    assert select_diagnosis_candidate([], in_flight_exists=False, enabled=True) is None


def test_select_returns_first_row_when_eligible():
    from juggle_selfheal import select_diagnosis_candidate
    rows = [{"id": 1, "count": 10}, {"id": 2, "count": 5}]
    result = select_diagnosis_candidate(rows, in_flight_exists=False, enabled=True)
    assert result is rows[0]


# ---------------------------------------------------------------------------
# Task 4 — reset_stale_diagnosing_rows
# ---------------------------------------------------------------------------

def test_reset_stale_diagnosing_rows_resets_old_diagnosing(tmp_path):
    """Rows stuck in 'diagnosing' beyond staleness_secs are reset to 'open'.

    2026-06-17: without this, a crash during dispatch leaves rows permanently diagnosing.
    """
    from juggle_db import JuggleDB
    from juggle_selfheal import reset_stale_diagnosing_rows

    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()

    db.dedup_or_insert_error("sig_stale", "A", "E", "tb", "ep", "{}")
    old_time = (datetime.now(timezone.utc) - timedelta(seconds=400)).strftime("%Y-%m-%d %H:%M")
    with db._connect() as conn:
        conn.execute(
            "UPDATE error_events SET status='diagnosing', last_seen=? WHERE signature_hash='sig_stale'",
            (old_time,),
        )
        conn.commit()

    now = datetime.now(timezone.utc)
    count = reset_stale_diagnosing_rows(db, now, staleness_secs=270)
    assert count == 1

    with db._connect() as conn:
        row = conn.execute("SELECT status FROM error_events WHERE signature_hash='sig_stale'").fetchone()
    assert row["status"] == "open"


def test_reset_stale_diagnosing_rows_leaves_fresh_alone(tmp_path):
    from juggle_db import JuggleDB
    from juggle_selfheal import reset_stale_diagnosing_rows

    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()

    db.dedup_or_insert_error("sig_fresh", "A", "E", "tb", "ep", "{}")
    recent = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M")
    with db._connect() as conn:
        conn.execute(
            "UPDATE error_events SET status='diagnosing', last_seen=? WHERE signature_hash='sig_fresh'",
            (recent,),
        )
        conn.commit()

    now = datetime.now(timezone.utc)
    count = reset_stale_diagnosing_rows(db, now, staleness_secs=270)
    assert count == 0


# ---------------------------------------------------------------------------
# Task 5 — purge_expired_selfheal
# ---------------------------------------------------------------------------

def test_purge_expired_deletes_old_rows(tmp_path):
    """Rows with last_seen older than retention_days are deleted.

    2026-06-17: retention auto-purge bounds the error_events table indefinitely.
    Only open/resolved rows are purged — not active in-flight rows.
    """
    from juggle_db import JuggleDB
    from juggle_selfheal import purge_expired_selfheal

    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()

    db.dedup_or_insert_error("sig_old", "A", "E", "tb", "ep", "{}")
    old = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%d %H:%M")
    with db._connect() as conn:
        conn.execute("UPDATE error_events SET last_seen=? WHERE signature_hash='sig_old'", (old,))
        conn.commit()

    # diagnosing row should NOT be purged even if old
    db.dedup_or_insert_error("sig_inflight", "A", "E", "tb", "ep2", "{}")
    with db._connect() as conn:
        conn.execute(
            "UPDATE error_events SET last_seen=?, status='diagnosing' WHERE signature_hash='sig_inflight'",
            (old,),
        )
        conn.commit()

    now = datetime.now(timezone.utc)
    deleted = purge_expired_selfheal(db, now, retention_days=14)
    assert deleted == 1  # only sig_old purged

    with db._connect() as conn:
        row = conn.execute("SELECT id FROM error_events WHERE signature_hash='sig_old'").fetchone()
    assert row is None
    with db._connect() as conn:
        row = conn.execute("SELECT id FROM error_events WHERE signature_hash='sig_inflight'").fetchone()
    assert row is not None  # in-flight preserved


def test_purge_expired_keeps_recent_rows(tmp_path):
    from juggle_db import JuggleDB
    from juggle_selfheal import purge_expired_selfheal

    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()

    db.dedup_or_insert_error("sig_new", "A", "E", "tb", "ep", "{}")
    # 5 days old — within retention window
    recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
    with db._connect() as conn:
        conn.execute("UPDATE error_events SET last_seen=? WHERE signature_hash='sig_new'", (recent,))
        conn.commit()

    now = datetime.now(timezone.utc)
    deleted = purge_expired_selfheal(db, now, retention_days=14)
    assert deleted == 0


def test_purge_expired_returns_count(tmp_path):
    from juggle_db import JuggleDB
    from juggle_selfheal import purge_expired_selfheal

    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()

    old = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M")
    for sig in ["sa", "sb", "sc"]:
        db.dedup_or_insert_error(sig, "A", "E", "tb", "ep", "{}")
        with db._connect() as conn:
            conn.execute("UPDATE error_events SET last_seen=? WHERE signature_hash=?", (old, sig))
            conn.commit()

    now = datetime.now(timezone.utc)
    assert purge_expired_selfheal(db, now, retention_days=14) == 3


# ---------------------------------------------------------------------------
# Task 6 — build_diagnosis_prompt (pure)
# ---------------------------------------------------------------------------

def test_build_diagnosis_prompt_contains_key_fields():
    """Prompt must include sig, exc_type, entrypoint, traceback, count."""
    from juggle_selfheal import build_diagnosis_prompt

    row = {
        "id": 42,
        "signature_hash": "abcdef1234567890",
        "exc_type": "AttributeError",
        "entrypoint": "juggle_watchdog_daemon",
        "surface": None,
        "traceback": "Traceback (most recent call last):\n  File foo.py line 10",
        "command_args": '{"cmd": "start"}',
        "count": 7,
        "first_seen": "2026-06-10 12:00",
        "last_seen": "2026-06-17 08:00",
    }
    prompt = build_diagnosis_prompt(row)
    assert "abcdef1234567890" in prompt
    assert "AttributeError" in prompt
    assert "juggle_watchdog_daemon" in prompt
    assert "7" in prompt  # count
    assert "request-action" in prompt  # operator gate
    assert "do NOT auto-merge" in prompt.lower() or "do not auto-merge" in prompt.lower() or "not auto-merge" in prompt.lower() or "never auto-merge" in prompt.lower() or "operator" in prompt.lower()


def test_build_diagnosis_prompt_is_pure():
    """build_diagnosis_prompt must not raise and must be deterministic."""
    from juggle_selfheal import build_diagnosis_prompt

    row = {
        "id": 1, "signature_hash": "abc", "exc_type": "RuntimeError",
        "entrypoint": "ep", "surface": None, "traceback": "tb",
        "command_args": "{}", "count": 3,
        "first_seen": "2026-06-01 00:00", "last_seen": "2026-06-17 00:00",
    }
    p1 = build_diagnosis_prompt(row)
    p2 = build_diagnosis_prompt(row)
    assert p1 == p2
    assert isinstance(p1, str) and len(p1) > 50


# ---------------------------------------------------------------------------
# Task 7 — maybe_dispatch_selfheal_diagnosis (stub dispatch seam)
# ---------------------------------------------------------------------------

def test_maybe_dispatch_does_nothing_when_disabled(tmp_path):
    from juggle_db import JuggleDB
    from juggle_selfheal import maybe_dispatch_selfheal_diagnosis

    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()

    db.dedup_or_insert_error("sig1", "A", "E", "tb", "ep", "{}")
    with db._connect() as conn:
        conn.execute("UPDATE error_events SET count=10 WHERE signature_hash='sig1'")
        conn.commit()

    dispatched = []
    with patch("juggle_selfheal.get_settings", return_value={"selfheal": {"enabled": False, "min_count": 3, "retention_days": 14}}):
        maybe_dispatch_selfheal_diagnosis(db, dispatch_fn=lambda *a, **kw: dispatched.append(a))

    assert dispatched == []


def test_maybe_dispatch_dispatches_when_enabled(tmp_path):
    """Given enabled + qualifying row, dispatch_fn is called with right prompt."""
    from juggle_db import JuggleDB
    from juggle_selfheal import maybe_dispatch_selfheal_diagnosis

    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()

    db.dedup_or_insert_error("sigX", "A", "TypeError", "tb", "ep", "{}")
    with db._connect() as conn:
        conn.execute("UPDATE error_events SET count=5 WHERE signature_hash='sigX'")
        conn.commit()

    dispatched = []
    settings = {"selfheal": {"enabled": True, "min_count": 3, "retention_days": 14}}
    with patch("juggle_selfheal.get_settings", return_value=settings):
        maybe_dispatch_selfheal_diagnosis(db, dispatch_fn=lambda db, thread_id, prompt: dispatched.append((thread_id, prompt)))

    assert len(dispatched) == 1
    _, prompt = dispatched[0]
    assert "TypeError" in prompt


def test_maybe_dispatch_does_not_dispatch_when_in_flight(tmp_path):
    """If a row is already 'diagnosing', no new dispatch occurs."""
    from juggle_db import JuggleDB
    from juggle_selfheal import maybe_dispatch_selfheal_diagnosis

    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()

    db.dedup_or_insert_error("sig_open", "A", "E", "tb", "ep", "{}")
    with db._connect() as conn:
        conn.execute("UPDATE error_events SET count=5 WHERE signature_hash='sig_open'")
        conn.commit()
    db.dedup_or_insert_error("sig_flying", "A", "E", "tb", "ep2", "{}")
    with db._connect() as conn:
        conn.execute("UPDATE error_events SET count=5, status='diagnosing' WHERE signature_hash='sig_flying'")
        conn.commit()

    dispatched = []
    settings = {"selfheal": {"enabled": True, "min_count": 3, "retention_days": 14}}
    with patch("juggle_selfheal.get_settings", return_value=settings):
        maybe_dispatch_selfheal_diagnosis(db, dispatch_fn=lambda *a: dispatched.append(a))

    assert dispatched == []


def test_maybe_dispatch_sets_row_to_awaiting_approval_after_dispatch(tmp_path):
    """After dispatch, the claimed row status should be 'awaiting_approval'."""
    from juggle_db import JuggleDB
    from juggle_selfheal import maybe_dispatch_selfheal_diagnosis

    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()

    db.dedup_or_insert_error("sigY", "A", "E", "tb", "ep", "{}")
    with db._connect() as conn:
        conn.execute("UPDATE error_events SET count=5 WHERE signature_hash='sigY'")
        conn.commit()

    settings = {"selfheal": {"enabled": True, "min_count": 3, "retention_days": 14}}
    with patch("juggle_selfheal.get_settings", return_value=settings):
        maybe_dispatch_selfheal_diagnosis(db, dispatch_fn=lambda db, tid, prompt: None)

    with db._connect() as conn:
        row = conn.execute("SELECT status FROM error_events WHERE signature_hash='sigY'").fetchone()
    assert row["status"] == "awaiting_approval"


# ---------------------------------------------------------------------------
# Task 8 — Reentrancy guard on dispatch path
# ---------------------------------------------------------------------------

def test_reentrancy_guard_env_var_set_during_dispatch(tmp_path):
    """JUGGLE_SELFHEAL_OP must be set when dispatch_fn is called."""
    from juggle_db import JuggleDB
    from juggle_selfheal import maybe_dispatch_selfheal_diagnosis, _SELFHEAL_ENV

    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()

    db.dedup_or_insert_error("sigZ", "A", "E", "tb", "ep", "{}")
    with db._connect() as conn:
        conn.execute("UPDATE error_events SET count=5 WHERE signature_hash='sigZ'")
        conn.commit()

    env_during_dispatch = []

    def capturing_dispatch(db, thread_id, prompt):
        env_during_dispatch.append(os.environ.get(_SELFHEAL_ENV))

    settings = {"selfheal": {"enabled": True, "min_count": 3, "retention_days": 14}}
    with patch("juggle_selfheal.get_settings", return_value=settings):
        maybe_dispatch_selfheal_diagnosis(db, dispatch_fn=capturing_dispatch)

    assert env_during_dispatch == ["1"]
    # env var must be cleared after dispatch
    assert os.environ.get(_SELFHEAL_ENV) is None


# ---------------------------------------------------------------------------
# Task 9 — list-selfheal --json
# ---------------------------------------------------------------------------

def test_list_selfheal_json_output(tmp_path, capsys):
    import json
    from juggle_db import JuggleDB
    from juggle_cmd_misc import _cmd_list_selfheal
    from argparse import Namespace

    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()
    db.dedup_or_insert_error("sjson", "A", "ValueError", "tb", "ep", "{}")

    args = Namespace(db_path=str(tmp_path / "t.db"), json=True)
    _cmd_list_selfheal(args)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["signature_hash"] == "sjson"
