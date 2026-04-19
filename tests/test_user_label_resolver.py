"""Tests for Task 4 user label resolver + Excel-style allocation."""
import pytest
from juggle_db import JuggleDB, _next_excel_label
from juggle_cli_common import _resolve_thread


@pytest.fixture
def db(tmp_path):
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    return d


def test_next_excel_label_skips_used_letters():
    assert _next_excel_label(set()) == "A"
    assert _next_excel_label({"A"}) == "B"
    used = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    assert _next_excel_label(used) == "AA"


def test_next_excel_label_two_letter_sequence():
    used = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ") | {"AA", "AB"}
    assert _next_excel_label(used) == "AC"


def test_labels_are_not_reassigned_after_archive(db):
    ids = [db.create_thread(f"t{i}", session_id="s") for i in range(3)]
    labels_initial = [db.get_thread(i)["user_label"] for i in ids]
    assert labels_initial == ["A", "B", "C"]
    # Archive B
    db.archive_thread(ids[1])
    # Next create should get D, not B
    new_id = db.create_thread("new", session_id="s")
    assert db.get_thread(new_id)["user_label"] == "D"


def test_resolve_thread_accepts_label(db):
    tid = db.create_thread("t", session_id="s")
    resolved = _resolve_thread(db, "A")
    assert resolved == tid


def test_resolve_thread_accepts_6char_hex_prefix(db):
    tid = db.create_thread("t", session_id="s")
    prefix = tid[:6]
    resolved = _resolve_thread(db, prefix)
    assert resolved == tid


def test_resolve_thread_case_insensitive_label(db):
    tid = db.create_thread("t", session_id="s")
    assert _resolve_thread(db, "a") == tid


def test_resolve_thread_accepts_full_uuid(db):
    tid = db.create_thread("t", session_id="s")
    assert _resolve_thread(db, tid) == tid
