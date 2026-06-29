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
    # P8 c4-write-cut: the live-set scan resolves from kind='conversation' nodes,
    # so the skip-live setup forces the label on the NODE (the sole store).
    live = db.create_thread("live", session_id="s")  # gets 'AA', seq -> 1
    with db._connect() as conn:
        conn.execute(
            "UPDATE nodes SET user_label = 'AB', state = 'open' "
            "WHERE id = ? AND kind='conversation'",
            (live,),
        )
        conn.commit()
    # Point the wheel back at index 1 ('AB'); a live node holds it -> skip to 'AC'.
    _set_seq(db, 1)
    nid = db.create_thread("next", session_id="s")
    assert _label(db, nid) == "AC"
    # Exactly one live 'AB' — index intact.
    with db._connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE kind='conversation' "
            "AND user_label = 'AB' AND state IN ('open','running')"
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
    # P8 c4-write-cut: the no-shared-live-slug invariant is enforced by the partial
    # unique idx_nodes_live_label on the node store (the threads index is retired).
    a = db.create_thread("a", session_id="s")  # 'AA'
    b = db.create_thread("b", session_id="s")  # 'AB'
    assert _label(db, a) == "AA"
    with pytest.raises(sqlite3.IntegrityError):
        with db._connect() as conn:
            conn.execute(
                "UPDATE nodes SET user_label = 'AA' "
                "WHERE id = ? AND kind='conversation'",
                (b,),
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


# ---------------------------------------------------------------------------
# 7. Allocation succeeds with >676 closed threads holding labels.
#    (Regression pin: _next_excel_label raised once 702 persisted labels
#     existed; _next_wheel_slug must only skip LIVE holders.)
# ---------------------------------------------------------------------------
def test_allocation_succeeds_with_many_closed_threads(db):
    """Wheel allocation works even when >676 closed threads hold labels.

    2026-06-16: add-task / init_db path triggered _next_excel_label which
    raised 'All 702 user labels in use' once 702 persisted labels existed.
    _next_wheel_slug skips ONLY live ('active'/'running') slugs, so it
    tolerates arbitrarily many closed-thread labels.
    """
    # Seed 700 closed threads — fills most two-letter wheel positions.
    for i in range(700):
        tid = db.create_thread(f"seed-{i}", session_id="s")
        db.set_thread_status(tid, "closed")

    # Allocation must succeed: the wheel finds a free live slot.
    new_tid = db.create_thread("live-new", session_id="s")
    label = db.get_thread(new_tid)["user_label"]
    assert label is not None
    assert len(label) == 2 and label.isalpha()

    # A second new thread must also succeed.
    new_tid2 = db.create_thread("live-new2", session_id="s")
    label2 = db.get_thread(new_tid2)["user_label"]
    assert label2 is not None
    assert label2 != label  # distinct slugs for distinct live threads


def test_init_db_idempotent_with_all_702_labels_used(tmp_path, monkeypatch):
    """A second init_db must not raise when all 702 Excel labels are in use.

    2026-06-16: Migration 4 re-added the dead 'label' column after Migration 16
    dropped it.  On the next init_db Migration 16 re-ran _next_excel_label
    against 702 persisted labels (A-Z + AA-ZZ), raising 'All 702 user labels
    in use' on a thread with NULL user_label.

    Fixed by guarding Migration 4 so it skips when user_label already exists.
    """
    import string
    import uuid
    from datetime import datetime, timezone

    import dbops.threads as _t
    monkeypatch.setattr(_t, "MAX_THREADS", 100000)

    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()

    # Build the complete 702-slot Excel label set: A-Z + AA-ZZ.
    letters = string.ascii_uppercase
    excel_labels = list(letters)
    for a in letters:
        for b in letters:
            excel_labels.append(a + b)

    now = datetime.now(timezone.utc).isoformat()
    with d._connect() as conn:
        for lbl in excel_labels:
            conn.execute(
                "INSERT INTO threads(id, user_label, session_id, topic, status, "
                "created_at, last_active, last_active_at) VALUES (?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), lbl, "s", "t", "closed", now, now, now),
            )
        # One thread with NULL user_label — the victim that triggers the backfill.
        conn.execute(
            "INSERT INTO threads(id, user_label, session_id, topic, status, "
            "created_at, last_active, last_active_at) VALUES (?,NULL,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "s", "extra", "closed", now, now, now),
        )
        conn.commit()

    # Second init_db (what juggle graph add-task calls): M4 adds 'label' back,
    # M16 sees it and tries to backfill the NULL row — must not raise.
    d.init_db()  # was: ValueError('All 702 user labels in use')

    # 'label' column must be gone.
    with d._connect() as conn:
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(threads)").fetchall()
        }
    assert "label" not in cols
