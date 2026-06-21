"""selfheal v2 p2 Task 2 — group_key column + backfill + dedup-stores-key +
new-variant signal + grouped view with REAL breadth re-split (DA fix b).
"""
from juggle_db import JuggleDB


def _db(tmp_path):
    db = JuggleDB(str(tmp_path / "j.db"))
    db.init_db()
    return db


def test_migration_adds_group_key_and_dedup_stores_it(tmp_path):
    db = _db(tmp_path)
    tb = '  File "/x/src/juggle_cmd_thread.py", line 42, in cmd_send\nKeyError: a\n'
    db.dedup_or_insert_error("sigAAA", "A", "KeyError", tb, "juggle_cli.py", "{}")
    with db._connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(error_events)")}
        assert "group_key" in cols
        gk = conn.execute(
            "SELECT group_key FROM error_events WHERE signature_hash='sigAAA'"
        ).fetchone()[0]
        assert gk and len(gk) == 16   # stored on insert


def test_backfill_populates_null_group_key(tmp_path):
    """REGRESSION (2026-06-21 selfheal v2 p2): migration must backfill group_key
    on pre-existing rows that predate the column (NULL group_key)."""
    db = _db(tmp_path)
    tb = '  File "/x/src/juggle_cmd_thread.py", line 7, in cmd_send\nKeyError: a\n'
    # Simulate a legacy row: insert then NULL out group_key.
    db.dedup_or_insert_error("siglegacy", "A", "KeyError", tb, "juggle_cli.py", "{}")
    with db._connect() as conn:
        conn.execute("UPDATE error_events SET group_key=NULL WHERE signature_hash='siglegacy'")
        conn.commit()
    # Re-run the migration directly — it must backfill the NULL.
    from dbops.migration_selfheal_group_key import migrate_group_key
    with db._connect() as conn:
        migrate_group_key(conn)
        gk = conn.execute(
            "SELECT group_key FROM error_events WHERE signature_hash='siglegacy'"
        ).fetchone()[0]
    assert gk and len(gk) == 16


def test_dedup_emits_new_variant_for_same_group_new_sig(tmp_path):
    db = _db(tmp_path)
    tb1 = '  File "/x/juggle_cmd_thread.py", line 42, in cmd_send\nKeyError: a\n'
    tb2 = tb1.replace("line 42", "line 99")  # same group, NEW sig
    db.dedup_or_insert_error("sig1", "A", "KeyError", tb1, "juggle_cli.py", "{}")
    db.dedup_or_insert_error("sig2", "A", "KeyError", tb2, "juggle_cli.py", "{}")
    assert db._last_new_variant and db._last_new_variant["signature_hash"] == "sig2"


def test_first_sig_in_group_is_not_a_new_variant(tmp_path):
    db = _db(tmp_path)
    tb = '  File "/x/juggle_cmd_thread.py", line 1, in cmd_send\nKeyError: a\n'
    db.dedup_or_insert_error("sigfirst", "A", "KeyError", tb, "juggle_cli.py", "{}")
    assert db._last_new_variant is None  # first sig founds the group, no signal


def test_grouped_view_aggregates_and_resplits_broad(tmp_path):
    """DA fix b: a group that absorbs >broad_cap distinct sigs is REALLY re-split
    — its member signatures are surfaced (members populated), not hidden behind
    one collapsed blob that could swallow a new bug."""
    db = _db(tmp_path)
    tb = '  File "/x/juggle_cmd_thread.py", line {n}, in cmd_send\nKeyError: a\n'
    for n in range(12):  # 12 distinct sigs, one group → broad (cap 10)
        db.dedup_or_insert_error(f"sig{n}", "A", "KeyError", tb.format(n=n), "juggle_cli.py", "{}")
    groups = db.get_grouped_error_events(broad_cap=10)
    assert len(groups) == 1
    g = groups[0]
    assert g["distinct_signatures"] == 12 and g["total_count"] == 12 and g["broad"] is True
    # REAL re-split: every member signature is exposed, not collapsed away.
    assert g["members"] is not None and len(g["members"]) == 12
    assert {m["signature_hash"] for m in g["members"]} == {f"sig{n}" for n in range(12)}


def test_grouped_view_narrow_group_stays_collapsed(tmp_path):
    db = _db(tmp_path)
    tb = '  File "/x/juggle_cmd_thread.py", line {n}, in cmd_send\nKeyError: a\n'
    for n in range(3):  # 3 distinct sigs, one group → narrow (cap 10)
        db.dedup_or_insert_error(f"sig{n}", "A", "KeyError", tb.format(n=n), "juggle_cli.py", "{}")
    groups = db.get_grouped_error_events(broad_cap=10)
    assert len(groups) == 1
    g = groups[0]
    assert g["broad"] is False and g["members"] is None and g["distinct_signatures"] == 3
