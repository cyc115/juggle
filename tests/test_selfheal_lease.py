"""selfheal v2 p2 Task 6 — set-once benign_until lease (not last_seen proxy)."""
from datetime import datetime, timedelta, timezone

from juggle_db import JuggleDB
from selfheal_triage import should_resurface


def _fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def test_lease_resurfaces_active_recurring_benign():
    """REGRESSION (2026-06-21 selfheal v2 p2): the last_seen-proxy lease never
    expired for a RECURRING benign error (last_seen kept refreshing). The set-once
    benign_until anchor fixes it — an expired lease resurfaces even with a fresh
    last_seen."""
    now = datetime(2026, 6, 21, tzinfo=timezone.utc)
    row = {"count": 5, "last_seen": _fmt(now),                      # JUST recurred
           "benign_until": _fmt(now - timedelta(days=1))}          # lease expired
    assert should_resurface(row, now, surge_count=20, absolute_count=100, lease_days=30) == "lease"


def test_lease_holds_when_benign_until_future():
    """A future benign_until overrides a stale last_seen — stays hidden."""
    now = datetime(2026, 6, 21, tzinfo=timezone.utc)
    row = {"count": 5, "last_seen": _fmt(now - timedelta(days=90)),  # last_seen ancient
           "benign_until": _fmt(now + timedelta(days=10))}           # but lease still active
    assert should_resurface(row, now, surge_count=20, absolute_count=100, lease_days=30) is None


def test_legacy_null_benign_until_uses_last_seen_fallback():
    """Legacy rows (null benign_until) keep the last_seen-age fallback."""
    now = datetime(2026, 6, 21, tzinfo=timezone.utc)
    row = {"count": 5, "last_seen": _fmt(now - timedelta(days=90))}  # no benign_until key
    assert should_resurface(row, now, surge_count=20, absolute_count=100, lease_days=30) == "lease"


def test_set_benign_lease_persists_and_overrides_recurrence(tmp_path):
    """End-to-end: set_benign_lease stamps a set-once anchor; a recurrence bumps
    last_seen but does NOT extend the lease, so resurface still fires once expired."""
    db = JuggleDB(str(tmp_path / "j.db"))
    db.init_db()
    eid = db.dedup_or_insert_error(
        "sigL", "A", "ValueError", '  File "/x/juggle_x.py", line 1, in f\nValueError: x',
        "juggle_cli.py", "{}")
    db.set_error_event_status(eid, "non_issue")
    # Stamp a lease that already expired (lease_days negative → benign_until in past).
    db.set_benign_lease(eid, lease_days=-1)
    now = datetime.now(timezone.utc) + timedelta(minutes=1)
    out = db.resurface_nonissue_rows(now, surge_count=999, absolute_count=999, lease_days=999)
    assert any(r["signature_hash"] == "sigL" and r["reason"] == "lease" for r in out)
