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
