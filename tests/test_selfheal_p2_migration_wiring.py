"""Regression pins for the 2026-06-21 deployment-blocking incident: self-heal
Phase-2 migrations 47-49 not wired into run_migrations.

migrations_recent.py had a bare comment ("# Migrations 47-49 ... group_key +
audit + lease") with NO apply_selfheal_p2_migrations(conn) call. On the
run_migrations / doctor / init-db / upgrade path the P2 schema
(error_events.group_key (47), selfheal_audit (48), error_events.benign_until
(49)) was therefore never created on an existing DB, yet the Phase-2 code QUERIES
group_key — so every error_events command + doctor crashed
"no such column: group_key" on an unmigrated DB.

It shipped because DH's tests applied the migration via a fixture
(migrate_group_key directly), not via run_migrations — a test-vs-prod path gap.
These pins exercise the run_migrations / JuggleDB path end to end so the wiring
cannot silently regress again.
"""
from juggle_db import JuggleDB


def _p2_schema(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(error_events)").fetchall()}
    tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    return cols, tables


def test_fresh_db_via_init_has_p2_schema_and_group_key_query_works(tmp_path):
    """A fresh DB built through JuggleDB.init_db()/run_migrations exposes the full
    P2 schema, and the group_key read path + list-selfheal run without raising
    'no such column: group_key' (2026-06-21 incident)."""
    db = JuggleDB(str(tmp_path / "j.db"))
    db.init_db()
    with db._connect() as conn:
        cols, tables = _p2_schema(conn)
    assert "group_key" in cols          # migration 47
    assert "benign_until" in cols       # migration 49
    assert "selfheal_audit" in tables   # migration 48

    db.dedup_or_insert_error(
        "sigDQ", "A", "KeyError",
        '  File "/x/juggle_cli.py", line 1, in cmd\nKeyError: a\n',
        "juggle_cli.py", "{}",
    )
    # The group_key query (GROUP BY group_key) is the call that crashed pre-fix.
    groups = db.get_grouped_error_events(broad_cap=10)
    assert groups and groups[0]["total_count"] == 1

    # list-selfheal CLI handler must run without crashing on the P2 schema.
    from juggle_cmd_misc import _cmd_list_selfheal

    class _Args:
        db_path = str(tmp_path / "j.db")
        json = True
        all = False
        status = None

    _cmd_list_selfheal(_Args())


def test_run_migrations_rewires_p2_schema_on_upgraded_db(tmp_path):
    """The WIRING guard: a legacy DB stripped of the P2 schema must regain
    group_key / benign_until / selfheal_audit after run_migrations — i.e.
    apply_selfheal_p2_migrations(conn) is actually CALLED from run_migrations.

    RED when the wiring is a bare comment (the 2026-06-21 shipped gap). Also
    exercises _backfill on the run_migrations (row_factory=Row) connection so a
    plain-connection TypeError on r["error_class"] would surface here too.
    """
    from dbops.migrations import run_migrations

    db = JuggleDB(str(tmp_path / "j.db"))
    db.init_db()
    # Insert a row so migration 47's backfill has work (validates _backfill on the
    # Row connection); its group_key is dropped below with the column.
    db.dedup_or_insert_error(
        "sigLEG", "A", "KeyError",
        '  File "/x/juggle_cli.py", line 7, in cmd\nKeyError: a\n',
        "juggle_cli.py", "{}",
    )

    # Simulate a pre-P2 (legacy) DB: drop the P2 columns/table that the CREATE
    # path now seeds, so ONLY the run_migrations wiring can restore them.
    with db._connect() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_error_events_group_key")
        conn.execute("ALTER TABLE error_events DROP COLUMN group_key")
        conn.execute("ALTER TABLE error_events DROP COLUMN benign_until")
        conn.execute("DROP TABLE IF EXISTS selfheal_audit")
        conn.commit()
        cols, tables = _p2_schema(conn)
        assert "group_key" not in cols and "benign_until" not in cols
        assert "selfheal_audit" not in tables

    # run_migrations must re-add all three via the wired P2 chain (47/48/49).
    # P8 terminal: run_migrations ALTERs the legacy threads table (dropped on the
    # prior init_db), so re-create it first — exactly as init_db does before each
    # migration pass — then the wired P2 chain restores the dropped P2 schema.
    from helpers.node_seed import make_legacy_tables
    with db._connect() as conn:
        make_legacy_tables(conn, "threads")
        run_migrations(conn)
        conn.commit()

    with db._connect() as conn:
        cols, tables = _p2_schema(conn)
        gk = conn.execute(
            "SELECT group_key FROM error_events WHERE signature_hash='sigLEG'"
        ).fetchone()[0]
    assert "group_key" in cols          # migration 47 wired
    assert "benign_until" in cols       # migration 49 wired
    assert "selfheal_audit" in tables   # migration 48 wired
    # _backfill recomputed the legacy row's group_key on the Row connection.
    assert gk and len(gk) == 16
