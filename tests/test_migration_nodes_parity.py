"""Pin (2026-06-22): nodes lacked user_label/assigned_by/last_active_at + the
kind-scoped unique slug index, blocking the P8 read-collapse (no ALTER TABLE nodes existed)."""
import sqlite3
import pytest
from juggle_db import JuggleDB


def _fresh(tmp_path):
    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    return db


def test_parity_columns_present(tmp_path):
    db = _fresh(tmp_path)
    with db._connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    assert {"user_label", "assigned_by", "last_active_at"} <= cols


def test_assigned_by_defaults_auto(tmp_path):
    db = _fresh(tmp_path)
    with db._connect() as conn:
        conn.execute("INSERT INTO nodes (id,kind,title,state,created_at,updated_at) "
                     "VALUES ('n1','task','t','open','x','x')")
        conn.commit()
        assert conn.execute("SELECT assigned_by FROM nodes WHERE id='n1'").fetchone()[0] == "auto"


def test_user_label_unique_per_conversation(tmp_path):
    db = _fresh(tmp_path)
    with db._connect() as conn:
        conn.execute("INSERT INTO nodes (id,kind,title,state,user_label,created_at,updated_at) "
                     "VALUES ('a','conversation','t','open','foo','x','x')")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO nodes (id,kind,title,state,user_label,created_at,updated_at) "
                         "VALUES ('b','conversation','t','open','foo','x','x')")
            conn.commit()


def test_user_label_not_unique_across_kinds(tmp_path):
    db = _fresh(tmp_path)
    with db._connect() as conn:
        conn.execute("INSERT INTO nodes (id,kind,title,state,user_label,created_at,updated_at) "
                     "VALUES ('a','conversation','t','open','foo','x','x')")
        # a task node may reuse the same label string (index filters kind='conversation')
        conn.execute("INSERT INTO nodes (id,kind,title,state,user_label,created_at,updated_at) "
                     "VALUES ('c','task','t','open','foo','x','x')")
        conn.commit()  # must NOT raise


def test_migration_50_idempotent(tmp_path):
    db = _fresh(tmp_path)
    db.init_db()  # second pass — additive ALTER must not crash


def test_backfill_survives_recycled_slug(tmp_path):
    """Regression (2026-06-22): Migration 50 backfill crashed with
    'UNIQUE constraint failed: nodes.user_label' on any DB where the slug wheel
    recycled a user_label across a LIVE + an ARCHIVED conversation node (legacy
    idx_threads_live_label is live-scoped, so live+archived dup slugs are valid
    and BOTH threads mirror to conversation nodes via Migration 44). The parity
    index must be live-scoped too (state IN ('open','running')) — matching the
    legacy index — so the backfill does not abort the whole `juggle doctor` pass."""
    db = _fresh(tmp_path)
    from dbops.migration_nodes_parity import backfill_nodes_parity
    with db._connect() as conn:
        # archived thread keeps slug 'foo'; the wheel recycled 'foo' to a new live thread
        conn.execute("INSERT INTO threads (id,session_id,topic,status,user_label,created_at,last_active) "
                     "VALUES ('arch','','x','archived','foo','c','la')")
        conn.execute("INSERT INTO threads (id,session_id,topic,status,user_label,created_at,last_active) "
                     "VALUES ('live','','x','active','foo','c','la')")
        # Migration 44 mirrors ALL threads to conversation nodes (no state filter)
        conn.execute("INSERT INTO nodes (id,kind,title,state,created_at,updated_at) "
                     "VALUES ('arch','conversation','x','archived','c','la')")
        conn.execute("INSERT INTO nodes (id,kind,title,state,created_at,updated_at) "
                     "VALUES ('live','conversation','x','open','c','la')")
        conn.commit()
        backfill_nodes_parity(conn)   # must NOT raise IntegrityError
        # the live node carries the slug; the archived one is outside the live index
        live = conn.execute("SELECT user_label FROM nodes WHERE id='live'").fetchone()[0]
    assert live == "foo"


def test_backfill_populates_parity(tmp_path):
    db = _fresh(tmp_path)
    from dbops.migration_nodes_parity import backfill_nodes_parity
    with db._connect() as conn:
        conn.execute("INSERT INTO threads (id,session_id,topic,status,user_label,assigned_by,"
                     "created_at,last_active,last_active_at) "
                     "VALUES ('t1','','x','active','slug-1','human','c','old-la','new-la')")
        conn.execute("INSERT INTO nodes (id,kind,title,state,created_at,updated_at) "
                     "VALUES ('t1','conversation','x','open','c','old-la')")
        conn.commit()
        backfill_nodes_parity(conn)
        row = conn.execute("SELECT user_label, assigned_by, last_active_at, updated_at "
                           "FROM nodes WHERE id='t1'").fetchone()
    # _connect uses sqlite3.Row; compare as a plain tuple.
    assert tuple(row) == ("slug-1", "human", "new-la", "new-la")   # staleness fixed -> updated_at=new-la
