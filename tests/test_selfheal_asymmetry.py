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


def test_default_view_excludes_resolved_and_non_issue(tmp_path):
    """selfheal-v2 P1 (2026-06-21): default list excludes resolved + non_issue."""
    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()
    db.dedup_or_insert_error("s_open", "A", "E", "tb", "ep", "{}")
    r = db.dedup_or_insert_error("s_res", "A", "E", "tb", "ep", "{}")
    n = db.dedup_or_insert_error("s_ni", "A", "E", "tb", "ep", "{}")
    p = db.dedup_or_insert_error("s_prop", "A", "E", "tb", "ep", "{}")
    db.set_error_event_status(r, "resolved")
    db.set_error_event_status(n, "non_issue")
    db.set_error_event_status(p, "non_issue_proposed")
    sigs = {row["signature_hash"] for row in db.get_open_error_events()}
    assert sigs == {"s_open", "s_prop"}  # non_issue_proposed stays visible (greyed)


def test_all_flag_shows_everything(tmp_path):
    """selfheal-v2 P1 (2026-06-21): include_hidden returns resolved + non_issue too."""
    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()
    n = db.dedup_or_insert_error("s_ni", "A", "E", "tb", "ep", "{}")
    db.set_error_event_status(n, "non_issue")
    assert len(db.get_open_error_events(include_hidden=True)) == 1
    assert len(db.get_open_error_events()) == 0


def test_status_filter_returns_exact_status(tmp_path):
    """selfheal-v2 P1 (2026-06-21): --status filters to one status."""
    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()
    n = db.dedup_or_insert_error("s_ni", "A", "E", "tb", "ep", "{}")
    db.set_error_event_status(n, "non_issue")
    rows = db.get_open_error_events(status="non_issue")
    assert len(rows) == 1 and rows[0]["status"] == "non_issue"


def test_cmd_list_selfheal_status_filter_json(tmp_path, capsys):
    """selfheal-v2 P1 (2026-06-21): list-selfheal --status non_issue --json shows only that row."""
    import json
    from argparse import Namespace
    from juggle_cmd_misc import _cmd_list_selfheal
    db = JuggleDB(str(tmp_path / "t.db"))
    db.init_db()
    db.dedup_or_insert_error("s_open", "A", "E", "tb", "ep", "{}")
    n = db.dedup_or_insert_error("s_ni", "A", "E", "tb", "ep", "{}")
    db.set_error_event_status(n, "non_issue")
    _cmd_list_selfheal(Namespace(db_path=str(tmp_path / "t.db"), json=True,
                                 all=False, status="non_issue"))
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 1 and out[0]["signature_hash"] == "s_ni"
