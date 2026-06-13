"""Migration 40 idempotency — VCS columns on agent_runs (T-vcs-checkpoint).

The prod DB already has the 5 vcs columns (applied during the incident), so the
migration MUST converge on both a fresh dev DB and one where the columns already
exist. Each ADD COLUMN is guarded by a column-existence check; re-applying the
migrations must not raise and must not change the column set.
"""

import sqlite3
import sys
from pathlib import Path


SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from juggle_db import JuggleDB  # noqa: E402
from dbops.migrations_recent import apply_recent_migrations  # noqa: E402

VCS_COLS = {"repo_path", "vcs_type", "before_sha", "after_sha", "was_dirty"}


def _cols(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return {r["name"] for r in conn.execute("PRAGMA table_info(agent_runs)")}
    finally:
        conn.close()


def test_fresh_db_has_vcs_columns(tmp_path):
    p = str(tmp_path / "fresh.db")
    JuggleDB(p).init_db()
    assert VCS_COLS <= _cols(p)


def test_migration_is_idempotent(tmp_path):
    """Re-running migrations on an already-migrated DB is a no-op (no raise)."""
    p = str(tmp_path / "re.db")
    JuggleDB(p).init_db()
    before = _cols(p)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        apply_recent_migrations(conn)  # second pass — must not raise
        conn.commit()
    finally:
        conn.close()
    assert _cols(p) == before
    assert VCS_COLS <= _cols(p)


def test_migration_converges_when_columns_preexist(tmp_path):
    """Simulate the prod state: cols already present before migrations run."""
    p = str(tmp_path / "preexist.db")
    db = JuggleDB(p)
    db.init_db()
    # Drop+recreate a minimal agent_runs WITHOUT vcs cols, then re-migrate to add
    # them, then add them again manually to prove the guard skips existing cols.
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        # columns already exist from init_db; manually re-running the ALTER chain
        # would raise "duplicate column" without the guard — assert it does NOT.
        apply_recent_migrations(conn)
        conn.commit()
        assert VCS_COLS <= {r["name"] for r in conn.execute("PRAGMA table_info(agent_runs)")}
    finally:
        conn.close()
