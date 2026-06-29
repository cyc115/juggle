"""Migration-17/18/19 domain-drop test.

The one-shot juggle_migrate_lifecycle backfill (and its tests) were retired by the
P8 terminal-drop (Migration 55) — it operated on the now-dropped ``threads`` table.
This file keeps the unrelated migrations-17/18/19 domain-drop pin.
"""


def test_migration_17_18_19_drops_domain(tmp_path):
    """Migrations 17–19 drop domain columns and tables on an old-schema DB."""
    import sqlite3

    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, domain TEXT)")
    conn.execute("CREATE TABLE agents (id TEXT PRIMARY KEY, domain TEXT)")
    conn.execute("CREATE TABLE domains (name TEXT PRIMARY KEY)")
    conn.execute(
        "CREATE TABLE domain_paths (path_fragment TEXT PRIMARY KEY, domain TEXT)"
    )
    conn.commit()
    conn.close()

    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "src"))
    from juggle_db import JuggleDB

    JuggleDB(db_path).init_db()

    conn2 = sqlite3.connect(db_path)
    cols_agents = {
        row[1] for row in conn2.execute("PRAGMA table_info(agents)").fetchall()
    }
    tables = {
        row[0]
        for row in conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn2.close()

    assert "domain" not in cols_agents
    assert "domains" not in tables
    assert "domain_paths" not in tables
    # P8 Migration 55 terminal-drop: the legacy threads table is gone post-init.
    assert "threads" not in tables
