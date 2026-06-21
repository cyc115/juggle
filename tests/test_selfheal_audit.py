"""selfheal v2 p2 Task 4 — durable hide-on-arrival audit log."""
from juggle_db import JuggleDB


def _db(tmp_path):
    db = JuggleDB(str(tmp_path / "j.db"))
    db.init_db()
    return db


def test_record_and_get_audit_roundtrip(tmp_path):
    db = _db(tmp_path)
    db.record_selfheal_audit(1, "sigA", "grpA", "allowlist_hide", "tmp_path_gone", detail="v1")
    rows = db.get_selfheal_audit(action="allowlist_hide")
    assert len(rows) == 1
    assert rows[0]["signature_hash"] == "sigA" and rows[0]["reason"] == "tmp_path_gone"
    assert rows[0]["group_key"] == "grpA" and rows[0]["detail"] == "v1"


def test_allowlist_sweep_writes_audit_via_dispatch(tmp_path):
    """The dispatch tick's allowlist sweep records a durable audit row per hide."""
    db = _db(tmp_path)
    db.dedup_or_insert_error(
        "sigtmp", "A", "OSError",
        "OSError: no such file or directory: /tmp/juggle-juggle-XX/y", "juggle_cli.py", "{}")
    from juggle_selfheal_diagnosis import maybe_dispatch_selfheal_diagnosis
    maybe_dispatch_selfheal_diagnosis(db)  # runs sweep+audit; no dispatch (disabled)
    audit = db.get_selfheal_audit(action="allowlist_hide")
    assert len(audit) == 1 and audit[0]["signature_hash"] == "sigtmp"
    assert audit[0]["reason"] == "tmp_path_gone"
    rows = {r["signature_hash"]: r["status"] for r in db.get_open_error_events(include_hidden=True)}
    assert rows["sigtmp"] == "non_issue"   # hidden


def test_new_variant_writes_durable_audit_row(tmp_path):
    """REGRESSION (2026-06-21 selfheal v2 p2, DA fix a): the new-variant
    over-aggregation signal must be DURABLE + visible (a selfheal_audit row),
    not merely a transient attr / log line that vanishes with the process."""
    db = _db(tmp_path)
    tb1 = '  File "/x/juggle_cmd_thread.py", line 42, in cmd_send\nKeyError: a\n'
    tb2 = tb1.replace("line 42", "line 99")  # same group, NEW sig
    db.dedup_or_insert_error("sigv1", "A", "KeyError", tb1, "juggle_cli.py", "{}")
    db.dedup_or_insert_error("sigv2", "A", "KeyError", tb2, "juggle_cli.py", "{}")
    audit = db.get_selfheal_audit(action="new_variant")
    assert len(audit) == 1 and audit[0]["signature_hash"] == "sigv2"
    assert audit[0]["group_key"]  # carries the group it joined
