"""JuggleDB tests: session active flag, thread CRUD, labels, current thread, UUID + label schema (split from test_juggle_db.py, 2026-06-10)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from juggle_context import get_thread_state
from juggle_db import JuggleDB


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(str(tmp_path / "test.db"))
    d.init_db()
    return d


def test_is_active_default_false(db):
    assert db.is_active() is False


def test_set_active(db):
    db.set_active(True)
    assert db.is_active() is True
    db.set_active(False)
    assert db.is_active() is False


def test_set_active_sets_started_at_once(db):
    db.set_active(True)
    with db._connect() as conn:
        row1 = conn.execute(
            "SELECT value FROM session WHERE key='started_at'"
        ).fetchone()
    db.set_active(True)
    with db._connect() as conn:
        row2 = conn.execute(
            "SELECT value FROM session WHERE key='started_at'"
        ).fetchone()
    assert row1["value"] == row2["value"]  # not overwritten on second call


def test_create_thread_returns_a(db):
    """create_thread returns a UUID (not a letter)."""
    import re

    tid = db.create_thread("My topic", session_id="s1")
    assert re.match(r"^[0-9a-f-]{36}$", tid), f"Expected UUID, got: {tid}"


def test_create_thread_sequential(db):
    """Sequential threads get sequential wheel slugs AA, AB (T-slug-wheel)."""
    a = db.create_thread("Topic A", session_id="s1")
    b = db.create_thread("Topic B", session_id="s1")
    assert db.get_thread(a)["user_label"] == "AA"
    assert db.get_thread(b)["user_label"] == "AB"


def test_create_thread_max_10(db):
    from juggle_db import MAX_THREADS

    for i in range(MAX_THREADS):
        db.create_thread(f"Topic {i}", session_id="s1")
    with pytest.raises(ValueError, match=f"Maximum of {MAX_THREADS}"):
        db.create_thread("Topic overflow", session_id="s1")


def test_get_thread(db):
    tid = db.create_thread("My topic", session_id="s1")
    t = db.get_thread(tid)
    assert t is not None
    assert t["topic"] == "My topic"
    assert t["status"] == "active"


def test_get_thread_missing(db):
    assert db.get_thread("not-a-real-uuid") is None


def test_get_all_threads(db):
    ta = db.create_thread("A topic", session_id="s1")
    tb = db.create_thread("B topic", session_id="s1")
    threads = db.get_all_threads()
    assert len(threads) == 2
    assert threads[0]["id"] == ta
    assert threads[1]["id"] == tb


def test_update_thread(db):
    tid = db.create_thread("My topic", session_id="s1")
    db.update_thread(tid, status="background")
    t = db.get_thread(tid)
    assert t["status"] == "background"


def test_update_thread_list_serialized(db):
    tid = db.create_thread("My topic", session_id="s1")
    db.update_thread(tid, key_decisions=["decision 1", "decision 2"])
    t = db.get_thread(tid)
    # Should be stored as JSON string
    parsed = json.loads(t["key_decisions"])
    assert parsed == ["decision 1", "decision 2"]


def test_set_get_current_thread(db):
    db.create_thread("My topic", session_id="s1")
    assert db.get_current_thread() is None
    db.set_current_thread("A")
    assert db.get_current_thread() == "A"


# UUID + label schema tests
# ------------------------------------------------------------------


def test_create_thread_returns_uuid(db):
    """create_thread() returns a UUID string, not a letter."""
    import re

    tid = db.create_thread("My topic", session_id="s1")
    assert re.match(r"^[0-9a-f-]{36}$", tid), f"Expected UUID, got: {tid}"


def test_create_thread_first_user_label_is_aa(db):
    """First thread created gets wheel slug 'AA' (T-slug-wheel)."""
    tid = db.create_thread("My topic", session_id="s1")
    thread = db.get_thread(tid)
    assert thread is not None
    assert thread["user_label"] == "AA"


def test_create_thread_second_user_label_is_ab(db):
    """Second thread gets wheel slug 'AB' (T-slug-wheel)."""
    db.create_thread("First", session_id="s1")
    tid2 = db.create_thread("Second", session_id="s1")
    thread = db.get_thread(tid2)
    assert thread["user_label"] == "AB"


def test_schema_has_id_and_user_label_not_label(db):
    """threads table has 'id' and 'user_label'; 'label' and 'thread_id' are absent."""
    with db._connect() as conn:
        cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(threads)").fetchall()
        }
    assert "id" in cols
    assert "user_label" in cols
    assert "label" not in cols
    assert "thread_id" not in cols



# ------------------------------------------------------------------
# UUID + label Task 2 tests
# ------------------------------------------------------------------


def test_get_thread_by_uuid(db):
    """get_thread() accepts UUID and returns dict with 'id' and 'user_label'."""
    tid = db.create_thread("My topic", session_id="s1")
    thread = db.get_thread(tid)
    assert thread is not None
    assert thread["id"] == tid
    assert thread["user_label"] == "AA"
    assert thread["topic"] == "My topic"


def test_get_all_threads_includes_id_and_user_label(db):
    """get_all_threads() returns dicts with both 'id' and 'user_label'."""
    tid = db.create_thread("Topic A", session_id="s1")
    threads = db.get_all_threads()
    assert len(threads) == 1
    assert threads[0]["id"] == tid
    assert threads[0]["user_label"] == "AA"


def test_archive_thread_keeps_user_label(db):
    """archive_thread sets status='archived', KEEPS user_label (T-slug-wheel).

    The slug is a permanent historical handle; reuse is handled by the wheel's
    skip-live rule, not by erasing the row.
    """
    tid = db.create_thread("Topic A", session_id="s1")
    slug = db.get_thread(tid)["user_label"]
    db.archive_thread(tid)
    thread = db.get_thread(tid)
    assert thread["status"] == "archived"
    assert thread["user_label"] == slug  # persists, not recycled-by-erasure
    assert thread["show_in_list"] == 0


def test_new_thread_advances_wheel_not_recycle_after_archive(db):
    """T-slug-wheel: archiving does NOT free a slug for immediate reuse — the
    wheel advances to the next slot, and the archived row keeps its slug."""
    tid_a = db.create_thread("First", session_id="s1")
    assert db.get_thread(tid_a)["user_label"] == "AA"

    db.archive_thread(tid_a)

    tid_b = db.create_thread("Second", session_id="s1")
    assert db.get_thread(tid_b)["user_label"] == "AB"  # wheel advanced
    assert db.get_thread(tid_a)["user_label"] == "AA"  # archived slug persists


def test_new_thread_advances_wheel_not_recycle_after_close(db):
    """T-slug-wheel: same as archive — closing keeps the slug, wheel advances."""
    tid_a = db.create_thread("First", session_id="s1")
    assert db.get_thread(tid_a)["user_label"] == "AA"

    db.update_thread(tid_a, status="closed")

    tid_b = db.create_thread("Second", session_id="s1")
    assert db.get_thread(tid_b)["user_label"] == "AB"  # wheel advanced
    assert db.get_thread(tid_a)["user_label"] == "AA"  # closed slug persists


def test_two_active_threads_never_share_label(db):
    """Two non-archived/non-closed threads always get distinct labels.

    Regression pin: 2026-06-15 — recycling must not assign the same label to
    two simultaneously active threads.
    """
    tid_a = db.create_thread("First", session_id="s1")
    tid_b = db.create_thread("Second", session_id="s1")
    label_a = db.get_thread(tid_a)["user_label"]
    label_b = db.get_thread(tid_b)["user_label"]
    assert label_a != label_b, f"both got label {label_a!r}"


def test_get_thread_by_user_label_newest_wins_after_reuse(db):
    """get_thread_by_user_label resolves a reused slug to the NEWEST holder
    (T-slug-wheel) — the live thread, not the archived original.
    """
    tid_a = db.create_thread("Original", session_id="s1")
    assert db.get_thread(tid_a)["user_label"] == "AA"
    db.archive_thread(tid_a)  # keeps slug 'AA', but is terminal (not live)

    # A newer thread reuses slug 'AA' (forced onto the same slot).
    tid_b = db.create_thread("New holder", session_id="s1")  # 'AB'
    with db._connect() as conn:
        conn.execute("UPDATE threads SET user_label = 'AA' WHERE id = ?", (tid_b,))
        conn.commit()

    resolved = db.get_thread_by_user_label("AA")
    assert resolved is not None
    assert resolved["id"] == tid_b, (
        f"expected live newest holder {tid_b[:8]}, got {resolved['id'][:8]}"
    )


# ------------------------------------------------------------------
