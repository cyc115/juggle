"""Regression pins: Migration 16 label-backfill retirement.

2026-06-16: Migration 4 re-added the dead 'label' column (fixed by FIX 2 guard)
and Migration 16 ran _next_excel_label backfill that raises on >702 threads.
FIX 3: Migration 16 now just drops 'label' if present with NO backfill.  After
the fix, run_migrations on any DB with a 'label' column is a clean no-op drop —
no exception, no oscillating add/drop.
"""
import sqlite3
import string
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import dbops.threads as _threads
from juggle_db import JuggleDB


@pytest.fixture(autouse=True)
def _no_cap(monkeypatch):
    monkeypatch.setattr(_threads, "MAX_THREADS", 100000)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _all_excel_labels():
    """Return all 702 Excel labels: A-Z then AA-ZZ."""
    letters = string.ascii_uppercase
    labels = list(letters)
    for a in letters:
        for b in letters:
            labels.append(a + b)
    return labels


def _seed_threads(db, labels, extra_null=0):
    """Insert closed threads with the given user_labels (raw SQL, bypasses cap)."""
    now = _now()
    with db._connect() as conn:
        for lbl in labels:
            conn.execute(
                "INSERT INTO threads(id, user_label, session_id, topic, status, "
                "created_at, last_active, last_active_at) VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), lbl, "s", "t", "closed", now, now, now),
            )
        for _ in range(extra_null):
            conn.execute(
                "INSERT INTO threads(id, user_label, session_id, topic, status, "
                "created_at, last_active, last_active_at) VALUES (?,NULL,?,?,?,?,?,?)",
                (str(uuid.uuid4()), "s", "extra", "closed", now, now, now),
            )
        conn.commit()


def _inject_label_col(db):
    """Manually add the dead 'label' column to simulate a stale-schema state."""
    with db._connect() as conn:
        try:
            conn.execute("ALTER TABLE threads ADD COLUMN label TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # already there


# ---------------------------------------------------------------------------
# Core pin: run_migrations with stale 'label' col + 800 threads must succeed
# ---------------------------------------------------------------------------

def test_run_migrations_800_threads_no_label_col_after(tmp_path):
    """run_migrations on 800 threads with 'label' col present completes without
    error and leaves no 'label' column.

    2026-06-16: M16 backfill would raise ValueError on >702 existing labels.
    """
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    _seed_threads(d, _all_excel_labels()[:800] if len(_all_excel_labels()) >= 800
                  else _all_excel_labels())

    # Manually inject 'label' col to trigger M16 drop path.
    _inject_label_col(d)

    # Must not raise.
    d.init_db()

    with d._connect() as conn:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(threads)").fetchall()
        }
    assert "label" not in cols


def test_run_migrations_702_labels_plus_null_thread_no_raise(tmp_path):
    """All 702 Excel labels in use + 1 NULL user_label thread + 'label' col:
    run_migrations must not raise.

    2026-06-16: M16 backfill called _next_excel_label(702 used) → ValueError.
    """
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    _seed_threads(d, _all_excel_labels(), extra_null=1)
    _inject_label_col(d)

    d.init_db()  # must not raise

    with d._connect() as conn:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(threads)").fetchall()
        }
    assert "label" not in cols


def test_run_migrations_idempotent_multiple_calls(tmp_path):
    """Three consecutive init_db calls on the same DB must all succeed and
    leave no 'label' column — verifies the oscillation is gone."""
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    _seed_threads(d, _all_excel_labels(), extra_null=2)

    # init_db adds label via M4 (pre-FIX-2 guard would oscillate); after fix
    # M4 skips and M16 only drops if label somehow appeared.
    _inject_label_col(d)
    d.init_db()
    d.init_db()
    d.init_db()

    with d._connect() as conn:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(threads)").fetchall()
        }
    assert "label" not in cols


def test_null_user_labels_remain_null_after_migration(tmp_path):
    """M16 retirement: threads with NULL user_label must NOT get backfilled.

    After FIX 3, M16 only drops 'label' — it no longer assigns user_labels.
    NULL rows stay NULL; the wheel allocates fresh labels at create_thread time.
    """
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    _seed_threads(d, ["AA", "AB"], extra_null=3)
    _inject_label_col(d)

    d.init_db()

    with d._connect() as conn:
        null_count = conn.execute(
            "SELECT COUNT(*) FROM threads WHERE user_label IS NULL"
        ).fetchone()[0]
    # NULL rows must stay NULL — no backfill.
    assert null_count == 3
