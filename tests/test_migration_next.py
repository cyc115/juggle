import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))



def _conn(tmp_path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    conn.row_factory = sqlite3.Row
    return conn


# ── ensure_migration_seq / reserve_next ─────────────────────────────────────

def test_ensure_migration_seq_creates_table_bootstrapped_above_highest_known(tmp_path):
    from dbops.migration_seq import ensure_migration_seq
    conn = _conn(tmp_path)
    ensure_migration_seq(conn)
    row = conn.execute("SELECT next_number FROM migration_seq WHERE id = 1").fetchone()
    assert row is not None
    assert row["next_number"] >= 63  # highest known migration file is 62


def test_ensure_migration_seq_is_idempotent(tmp_path):
    from dbops.migration_seq import ensure_migration_seq
    conn = _conn(tmp_path)
    ensure_migration_seq(conn)
    conn.execute("UPDATE migration_seq SET next_number = 100 WHERE id = 1")
    conn.commit()
    ensure_migration_seq(conn)  # must NOT reset an already-advanced counter
    row = conn.execute("SELECT next_number FROM migration_seq WHERE id = 1").fetchone()
    assert row["next_number"] == 100


def test_reserve_next_increments_and_returns_previous_value(tmp_path):
    from dbops.migration_seq import ensure_migration_seq, reserve_next
    conn = _conn(tmp_path)
    ensure_migration_seq(conn)
    first = reserve_next(conn)
    second = reserve_next(conn)
    assert second == first + 1


def test_reserve_next_bootstraps_table_if_missing(tmp_path):
    """reserve_next works standalone — doesn't require a prior ensure_migration_seq call."""
    from dbops.migration_seq import reserve_next
    conn = _conn(tmp_path)
    n = reserve_next(conn)
    assert n >= 63


def test_reserve_next_concurrent_reservations_never_collide(tmp_path):
    """Pin (2026-07-02 duplicate-column cluster): N concurrent reservations
    against the SAME db file must all get distinct numbers."""
    from dbops.migration_seq import ensure_migration_seq, reserve_next

    db_path = str(tmp_path / "concurrent.db")
    setup = sqlite3.connect(db_path)
    ensure_migration_seq(setup)
    setup.close()

    results = []
    lock = threading.Lock()

    def worker():
        c = sqlite3.connect(db_path, timeout=30)
        n = reserve_next(c)
        with lock:
            results.append(n)
        c.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == len(set(results)), f"collision in {results}"


# ── CLI verb ─────────────────────────────────────────────────────────────

def test_cmd_migration_next_prints_reserved_number(tmp_path, capsys):
    from juggle_cmd_migration import cmd_migration_next
    from juggle_db import JuggleDB

    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()

    class Args:
        db_path = str(tmp_path / "j.db")

    cmd_migration_next(Args())
    out = capsys.readouterr().out.strip()
    assert out.isdigit()
    assert int(out) >= 63
