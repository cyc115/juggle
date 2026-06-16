"""Topic Slug Wheel: reusable AA-ZZ rotation + newest-wins resolution.

Pins the slug-wheel contract (T-slug-wheel):
  - rotating two-letter slugs AA..ZZ (676 slots), wrap ZZ->AA
  - skip-live allocation (never collide with a live holder)
  - persistence: slug stays on the row after close/archive (no recycling-by-erasure)
  - newest-wins resolution via the single chokepoint get_thread_by_user_label
  - DB-enforced "no two live topics share a slug" (partial unique index)
  - monotonic label_seq counter that never decreases across deletes/closes

ALL tests use a TEMP DB. They never touch the production juggle.db.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import dbops.threads as _threads  # noqa: E402
from juggle_db import JuggleDB  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


@pytest.fixture(autouse=True)
def _no_cap(monkeypatch):
    """Lift MAX_THREADS so wheel tests can create freely."""
    monkeypatch.setattr(_threads, "MAX_THREADS", 100000)


def _set_seq(db, seq):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO juggle_meta(key, value) VALUES ('label_seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(seq),),
        )
        conn.commit()


def _get_seq(db):
    with db._connect() as conn:
        row = conn.execute(
            "SELECT value FROM juggle_meta WHERE key = 'label_seq'"
        ).fetchone()
    return int(row["value"]) if row and row["value"] is not None else None


def _label(db, tid):
    return db.get_thread(tid)["user_label"]


# ---------------------------------------------------------------------------
# 1. Rotation: sequential creates produce AA, AB, AC ... and wrap ZZ -> AA.
# ---------------------------------------------------------------------------
def test_rotation_sequential(db):
    _set_seq(db, 0)
    labels = [_label(db, db.create_thread(f"t{i}", session_id="s")) for i in range(3)]
    assert labels == ["AA", "AB", "AC"]


def test_rotation_wraps_zz_to_aa(db):
    # Drive label_seq to the boundary: i=675 -> 'ZZ', then wrap to 'AA'.
    _set_seq(db, 675)
    assert _label(db, db.create_thread("zz", session_id="s")) == "ZZ"
    # No live holder of 'AA' in this DB, so the wheel wraps cleanly to 'AA'.
    assert _label(db, db.create_thread("wrap", session_id="s")) == "AA"


# ---------------------------------------------------------------------------
# 2. Skip-live: when a live thread holds the next wheel slug, allocation skips
#    it; the partial unique index is never violated.
# ---------------------------------------------------------------------------
def test_skip_live_slug(db):
    live = db.create_thread("live", session_id="s")  # gets 'AA', seq -> 1
    with db._connect() as conn:
        conn.execute(
            "UPDATE threads SET user_label = 'AB', status = 'active' WHERE id = ?",
            (live,),
        )
        conn.commit()
    # Point the wheel back at index 1 ('AB'); a live thread holds it -> skip to 'AC'.
    _set_seq(db, 1)
    nid = db.create_thread("next", session_id="s")
    assert _label(db, nid) == "AC"
    # Exactly one live 'AB' — index intact.
    with db._connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM threads WHERE user_label = 'AB' "
            "AND status IN ('active','running')"
        ).fetchone()[0]
    assert n == 1


# ---------------------------------------------------------------------------
# 3. Persistence: closing/archiving a thread KEEPS its user_label.
# ---------------------------------------------------------------------------
def test_label_persists_on_close(db):
    tid = db.create_thread("t1", session_id="s")
    lbl = _label(db, tid)
    db.set_thread_status(tid, "closed")
    assert _label(db, tid) == lbl


def test_label_persists_on_archive(db):
    tid = db.create_thread("t2", session_id="s")
    lbl = _label(db, tid)
    db.archive_thread(tid)
    assert _label(db, tid) == lbl


def test_label_persists_on_update_thread_close(db):
    tid = db.create_thread("t3", session_id="s")
    lbl = _label(db, tid)
    db.update_thread(tid, status="closed")
    assert _label(db, tid) == lbl


# ---------------------------------------------------------------------------
# 4. Newest-wins: a reused slug always resolves to the NEWEST holder.
# ---------------------------------------------------------------------------
def test_newest_wins_resolution(db):
    older = db.create_thread("old", session_id="s")  # 'AA'
    assert _label(db, older) == "AA"
    db.set_thread_status(older, "closed")  # frees the slug for reuse (not live)

    # Reuse the wheel position so a NEW thread also lands on 'AA'.
    _set_seq(db, 0)
    newer = db.create_thread("new", session_id="s")
    assert _label(db, newer) == "AA"

    resolved = db.get_thread_by_user_label("AA")
    assert resolved is not None
    assert resolved["id"] == newer  # newest live holder wins

    # Older closed holder is never returned while a newer holder exists,
    # even once the newer holder is also terminal (newest created_at wins).
    db.set_thread_status(newer, "closed")
    resolved2 = db.get_thread_by_user_label("AA")
    assert resolved2["id"] == newer


def test_newest_wins_is_case_insensitive(db):
    tid = db.create_thread("x", session_id="s")  # 'AA'
    assert db.get_thread_by_user_label("aa")["id"] == tid


# ---------------------------------------------------------------------------
# 5. No two live threads may share a slug — DB index enforces it.
# ---------------------------------------------------------------------------
def test_two_live_cannot_share_slug(db):
    a = db.create_thread("a", session_id="s")  # 'AA'
    b = db.create_thread("b", session_id="s")  # 'AB'
    assert _label(db, a) == "AA"
    with pytest.raises(sqlite3.IntegrityError):
        with db._connect() as conn:
            conn.execute(
                "UPDATE threads SET user_label = 'AA' WHERE id = ?", (b,)
            )
            conn.commit()


# ---------------------------------------------------------------------------
# 6. Counter monotonic across deletes/closes — never lowers label_seq.
# ---------------------------------------------------------------------------
def test_counter_monotonic_across_deletes(db):
    _set_seq(db, 0)
    a = db.create_thread("a", session_id="s")  # 'AA', seq -> 1
    b = db.create_thread("b", session_id="s")  # 'AB', seq -> 2
    assert _get_seq(db) == 2

    db.set_thread_status(a, "closed")
    with db._connect() as conn:
        conn.execute("DELETE FROM threads WHERE id = ?", (b,))
        conn.commit()

    c = db.create_thread("c", session_id="s")  # seq=2 -> 'AC', seq -> 3
    assert _get_seq(db) == 3
    assert _label(db, c) == "AC"
