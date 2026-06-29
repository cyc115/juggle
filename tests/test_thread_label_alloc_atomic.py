"""Atomic + graceful thread-label allocation (2026-06-21 incident pins).

Incident (sigs db8dfb62 / 6198bc24, observed live 2026-06-21):
  - IntegrityError "UNIQUE constraint failed: threads.user_label" (x2) and
    concurrent create-thread produced DUPLICATE / recycled labels, because the
    skip-live set and the partial unique index `idx_threads_live_label` only
    covered ('active','running') — they OMITTED the 'background' live state. A
    live background agent therefore shared a slug with a new active thread.
  - ValueError "All 702 user labels in use. Archive threads first." — the
    2-letter wheel (676 slots) crashed instead of degrading when full of live
    threads.

Fixes pinned here:
  (a) ATOMIC allocation — the live set / unique index cover ALL live states
      (active, running, background); concurrent creates never yield a duplicate
      live label.
  (b) GRACEFUL exhaustion — a full 2-char live space widens to a 3-char slug
      instead of raising.
  (c) The widen migration REPAIRS any pre-existing duplicate live labels before
      tightening the index (so doctor never crashes on an affected prod DB).

ALL tests use a TEMP DB; they never touch the production juggle.db.
"""

import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import dbops.threads as _threads  # noqa: E402
from dbops.schema import _wheel_index  # noqa: E402
from juggle_db import JuggleDB  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


@pytest.fixture(autouse=True)
def _no_cap(monkeypatch):
    """Lift MAX_THREADS so allocation tests can create freely."""
    monkeypatch.setattr(_threads, "MAX_THREADS", 1_000_000)


def _rewind_wheel_to(db, label):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO juggle_meta(key, value) VALUES ('label_seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(_wheel_index(label)),),
        )
        conn.commit()


def _live_labels(db):
    # P8 c4-write-cut: the live-label set lives on kind='conversation' nodes
    # (node vocab: open/running/background) — the sole conversation store.
    with db._connect() as conn:
        return [
            r["user_label"]
            for r in conn.execute(
                "SELECT user_label FROM nodes WHERE kind='conversation' "
                "AND state IN ('open','running','background')"
            ).fetchall()
        ]


# ---------------------------------------------------------------------------
# (a) 'background' is a LIVE state — its slug must not be recycled.
# ---------------------------------------------------------------------------
def test_background_slug_not_recycled_to_new_active(db):
    """2026-06-21: a live background agent shared a slug with a new active
    thread because skip-live omitted 'background'."""
    bg = db.create_thread("bg-agent", session_id="s")
    held = db.get_thread(bg)["user_label"]
    db.update_thread(bg, status="background")  # live background agent

    _rewind_wheel_to(db, held)  # next allocation would revisit the bg slug

    other = db.create_thread("new-active", session_id="s")
    assert db.get_thread(other)["user_label"] != held


def test_widened_index_rejects_duplicate_live_label(db):
    """The partial unique index must forbid an active conversation from taking a
    slug already held by a LIVE 'background' conversation (2026-06-21). P8
    c4-write-cut: the invariant now lives on idx_nodes_live_label (the node store);
    the legacy threads index is retired with the threads write."""
    bg = db.create_thread("bg", session_id="s")
    held = db.get_thread(bg)["user_label"]
    db.update_thread(bg, status="background")  # node state -> 'background' (live)
    other = db.create_thread("act", session_id="s")  # distinct slug

    with pytest.raises(sqlite3.IntegrityError):
        with db._connect() as conn:
            conn.execute(
                "UPDATE nodes SET user_label = ? WHERE id = ? AND kind='conversation'",
                (held, other),
            )
            conn.commit()


def test_concurrent_create_with_background_no_duplicates(db):
    """Concurrent create-thread never yields a duplicate live label, even when
    the wheel is rewound onto background-held slugs (2026-06-21 atomicity)."""
    for i in range(5):
        t = db.create_thread(f"bg{i}", session_id="s")
        db.update_thread(t, status="background")
    _rewind_wheel_to(db, "AA")  # force the wheel back over the bg slugs

    errors: list[str] = []

    def worker(i):
        try:
            db.create_thread(f"a{i}", session_id="s")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert errors == []
    labels = _live_labels(db)
    assert len(labels) == len(set(labels)), f"duplicate live labels: {labels}"


# ---------------------------------------------------------------------------
# (b) Graceful exhaustion — full 2-char live space widens to 3-char.
# ---------------------------------------------------------------------------
def test_full_two_char_space_degrades_to_three_char(db):
    """A full 676-slot live wheel must widen to a 3-letter slug, never crash
    (replaces the 'All 702 user labels in use' ValueError)."""
    for i in range(676):
        db.create_thread(f"t{i}", session_id="s")  # fills AA..ZZ, all active

    overflow = db.create_thread("overflow", session_id="s")  # must not raise
    label = db.get_thread(overflow)["user_label"]
    assert label is not None
    assert len(label) == 3 and label.isalpha()

    labels = _live_labels(db)
    assert len(labels) == len(set(labels))  # still globally unique among live


# ---------------------------------------------------------------------------
# (c) Widen migration repairs pre-existing duplicate live labels.
# ---------------------------------------------------------------------------
def test_widen_migration_repairs_existing_duplicate_live_labels(db):
    """doctor's slug-wheel migration must break pre-existing duplicate live
    labels before creating the widened unique index (2026-06-21 — an affected
    prod DB already holds active+background rows sharing a slug)."""
    from dbops.migrations_recent import run_migration_slug_wheel

    now = datetime.now(timezone.utc).isoformat()
    a_id, b_id = str(uuid.uuid4()), str(uuid.uuid4())
    with db._connect() as conn:
        # Simulate the legacy NARROW index, then plant a live duplicate that the
        # narrow index allowed (active + background both holding 'QQ').
        conn.execute("DROP INDEX IF EXISTS idx_threads_live_label")
        conn.execute(
            "CREATE UNIQUE INDEX idx_threads_live_label ON threads(user_label) "
            "WHERE user_label IS NOT NULL AND status IN ('active','running')"
        )
        for tid, status in ((a_id, "active"), (b_id, "background")):
            conn.execute(
                "INSERT INTO threads(id, user_label, session_id, topic, status, "
                "created_at, last_active, last_active_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (tid, "QQ", "s", "dup", status, now, now, now),
            )
        conn.commit()

    # The widen migration must not raise and must break the duplicate.
    with db._connect() as conn:
        run_migration_slug_wheel(conn)

    labels = _live_labels(db)
    assert len(labels) == len(set(labels)), f"duplicate survived: {labels}"
    # Widened index now covers 'background'.
    with db._connect() as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'idx_threads_live_label'"
        ).fetchone()["sql"]
    assert "background" in sql
