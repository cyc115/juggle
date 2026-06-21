"""selfheal v2 p2 Task 3 — batched fix resolves ONLY touched signatures."""
from juggle_db import JuggleDB


def _db(tmp_path):
    db = JuggleDB(str(tmp_path / "j.db"))
    db.init_db()
    return db


def test_resolve_signatures_leaves_siblings_open(tmp_path):
    """REGRESSION (2026-06-21 selfheal v2 p2): a batched fix must resolve ONLY the
    signatures it touched, not every variant sharing the group_key."""
    db = _db(tmp_path)
    tb = '  File "/x/juggle_cmd_thread.py", line {n}, in cmd_send\nKeyError: a\n'
    for n in (1, 2, 3):
        db.dedup_or_insert_error(f"sig{n}", "A", "KeyError", tb.format(n=n), "juggle_cli.py", "{}")
    n = db.resolve_signatures(["sig1", "sig2"], action_item_id=None)
    assert n == 2
    rows = {r["signature_hash"]: r["status"] for r in db.get_open_error_events(include_hidden=True)}
    assert rows["sig1"] == "resolved" and rows["sig2"] == "resolved"
    assert rows["sig3"] == "open"   # sibling variant untouched


def test_resolve_signatures_empty_list_is_noop(tmp_path):
    """REGRESSION (2026-06-21 selfheal v2 p2, DA fix c): resolve_signatures([])
    must be a 0-row no-op, never emit a malformed ``IN ()`` (SQLite syntax error)."""
    db = _db(tmp_path)
    db.dedup_or_insert_error("sigX", "A", "KeyError",
                             '  File "/x/juggle_x.py", line 1, in f\nKeyError: a\n',
                             "juggle_cli.py", "{}")
    assert db.resolve_signatures([]) == 0
    rows = {r["signature_hash"]: r["status"] for r in db.get_open_error_events(include_hidden=True)}
    assert rows["sigX"] == "open"  # nothing resolved


def test_resolve_signatures_sets_action_item_id(tmp_path):
    db = _db(tmp_path)
    db.dedup_or_insert_error("sigY", "A", "KeyError",
                             '  File "/x/juggle_x.py", line 1, in f\nKeyError: a\n',
                             "juggle_cli.py", "{}")
    assert db.resolve_signatures(["sigY"], action_item_id=42) == 1
    row = db.get_open_error_events(include_hidden=True)[0]
    assert row["status"] == "resolved" and row["action_item_id"] == 42
