"""selfheal v2 p2 Task 5 — audited, gated silent auto-hide for benign verdicts."""
import sqlite3

import pytest

from juggle_db import JuggleDB


def _db(tmp_path):
    db = JuggleDB(str(tmp_path / "j.db"))
    db.init_db()
    return db


_TB = '  File "/x/juggle_x.py", line 1, in f\nValueError: x'


def test_benign_verdict_visible_by_default(tmp_path):
    """LOCKED never-silent-terminal: default keeps benign verdicts VISIBLE."""
    db = _db(tmp_path)
    eid = db.dedup_or_insert_error("sigv", "A", "ValueError", _TB, "juggle_cli.py", "{}")
    from juggle_selfheal_diagnosis import apply_benign_verdict
    apply_benign_verdict(db, eid, cfg={"silent_autohide_enabled": False})
    assert db.get_open_error_events(include_hidden=True)[0]["status"] == "non_issue_proposed"
    assert db.get_selfheal_audit() == []


def test_benign_verdict_silent_when_enabled_writes_audit_and_lease(tmp_path):
    db = _db(tmp_path)
    eid = db.dedup_or_insert_error("sigv", "A", "ValueError", _TB, "juggle_cli.py", "{}")
    from juggle_selfheal_diagnosis import apply_benign_verdict
    apply_benign_verdict(db, eid, cfg={"silent_autohide_enabled": True, "resurface_lease_days": 30})
    row = db.get_open_error_events(include_hidden=True)[0]
    assert row["status"] == "non_issue" and row["benign_until"]   # lease set
    assert db.get_selfheal_audit(action="silent_autohide")[0]["signature_hash"] == "sigv"


def test_silent_autohide_audit_failure_does_not_flip_status(tmp_path, monkeypatch):
    """REGRESSION (2026-06-21 selfheal v2 p2, DA fix 🔴): a FAILED audit write must
    NEVER produce a silent terminal hide. The audit row is written BEFORE the
    status flip in ONE transaction, so an audit failure rolls back the flip and
    the row stays non-terminal. (RED on flip-then-audit ordering.)"""
    db = _db(tmp_path)
    eid = db.dedup_or_insert_error("sigv", "A", "ValueError", _TB, "juggle_cli.py", "{}")

    def _boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(db, "_audit_insert", _boom)
    with pytest.raises(sqlite3.OperationalError):
        db.silent_autohide(eid, reason="diagnoser_benign", lease_days=30)
    row = db.get_open_error_events(include_hidden=True)[0]
    assert row["status"] != "non_issue"        # NOT silently hidden
    assert row["status"] == "open"             # unchanged
    assert row["benign_until"] is None         # no lease leaked


def test_silent_autohide_refused_without_audit_table_falls_back_to_proposal(tmp_path):
    """Audit table is a HARD precondition: gate ON but table absent → visible proposal."""
    db = _db(tmp_path)
    eid = db.dedup_or_insert_error("sigv", "A", "ValueError", _TB, "juggle_cli.py", "{}")
    with db._connect() as conn:
        conn.execute("DROP TABLE selfheal_audit")
        conn.commit()
    from juggle_selfheal_diagnosis import apply_benign_verdict
    status = apply_benign_verdict(db, eid, cfg={"silent_autohide_enabled": True})
    assert status == "non_issue_proposed"
    assert db.get_open_error_events(include_hidden=True)[0]["status"] == "non_issue_proposed"
