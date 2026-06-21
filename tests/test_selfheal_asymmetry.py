"""selfheal-triage-v2 P1 — status vocabulary, dedup asymmetry, default-view pins."""
import pytest

from juggle_db import JuggleDB
from dbops.schema import VALID_ERROR_STATUSES


def test_valid_error_statuses_contains_new_states():
    """selfheal-v2 P1 (2026-06-21): non_issue + non_issue_proposed must exist."""
    assert "non_issue" in VALID_ERROR_STATUSES
    assert "non_issue_proposed" in VALID_ERROR_STATUSES
    assert "resolved" in VALID_ERROR_STATUSES


def test_set_status_rejects_unknown_status(tmp_path):
    """selfheal-v2 P1 (2026-06-21): app-level validation replaces dropped DB CHECK."""
    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()
    rid = db.dedup_or_insert_error("sig1", "A", "ValueError", "tb", "ep", "{}")
    with pytest.raises(ValueError):
        db.set_error_event_status(rid, "bogus_status")


def test_set_status_accepts_non_issue(tmp_path):
    """selfheal-v2 P1 (2026-06-21): non_issue is a settable status."""
    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()
    rid = db.dedup_or_insert_error("sig1", "A", "ValueError", "tb", "ep", "{}")
    assert db.set_error_event_status(rid, "non_issue") is True


def _count_rows(db, sig):
    with db._connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM error_events WHERE signature_hash=?", (sig,)
        ).fetchone()[0]


def test_resolved_recurrence_creates_new_open_row(tmp_path):
    """selfheal-v2 P1 (2026-06-21): resolved is non-sticky — recurrence re-alerts as a fresh open row."""
    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()
    rid = db.dedup_or_insert_error("sigR", "A", "ValueError", "tb", "ep", "{}")
    db.set_error_event_status(rid, "resolved")
    db.dedup_or_insert_error("sigR", "A", "ValueError", "tb", "ep", "{}")  # recurrence
    assert _count_rows(db, "sigR") == 2  # new open row created
    with db._connect() as conn:
        statuses = {
            r[0] for r in conn.execute(
                "SELECT status FROM error_events WHERE signature_hash='sigR'"
            ).fetchall()
        }
    assert statuses == {"resolved", "open"}


def test_non_issue_recurrence_bumps_count_no_new_row(tmp_path):
    """selfheal-v2 P1 (2026-06-21): non_issue is sticky — recurrence bumps count, stays hidden."""
    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()
    rid = db.dedup_or_insert_error("sigN", "B", None, "tb", "ep", "{}")
    db.set_error_event_status(rid, "non_issue")
    res = db.dedup_or_insert_error("sigN", "B", None, "tb", "ep", "{}")  # recurrence
    assert res is None  # dedup, no insert
    assert _count_rows(db, "sigN") == 1
    with db._connect() as conn:
        row = conn.execute(
            "SELECT status, count FROM error_events WHERE signature_hash='sigN'"
        ).fetchone()
    assert row[0] == "non_issue" and row[1] == 2  # still hidden, count bumped
