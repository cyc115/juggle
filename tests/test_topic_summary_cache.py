"""Persist-only cockpit topic-summary cache (migration 46) — v1.

Covers the PURE decision seam, L2 persistence (restart survival on a fresh DB
connection with the LLM NEVER called), idempotent last-writer-wins UPSERT, and
the R7 / B2-Q5 write-gate fix: a partial-but-displayable summary IS cached
(matches the real `any_content` display gate), while an empty/LLM-failed
summary is NEVER cached (so the next view retries). LLM is mocked throughout.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dbops.migration_topic_summary_cache import migrate_46_topic_summary_cache
from juggle_topic_summary_cache import (
    current_cursor,
    decide_summary_action,
    has_displayable_content,
    invalidate_summary_cache,
    load_cached_sections,
    read_summary_cache,
    store_summary,
    upsert_summary_cache,
)

_SECTIONS = {"context": "c", "why": "w", "what": "h", "result": "r"}


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    migrate_46_topic_summary_cache(conn)
    return conn


# ── decision purity (no DB, no LLM) ──────────────────────────────────────────

def test_decide_full_when_no_cached_row():
    assert decide_summary_action(None, 10) == "FULL"


def test_decide_exact_when_cursor_unchanged():
    assert decide_summary_action(10, 10) == "EXACT"


def test_decide_full_when_cursor_advanced():
    assert decide_summary_action(10, 17) == "FULL"


def test_decide_full_when_cursor_backwards_corruption():
    assert decide_summary_action(20, 10) == "FULL"


# ── displayable gate (mirrors the real _apply_summary any_content gate) ───────

def test_has_displayable_content_any_section():
    assert has_displayable_content({"context": "x", "why": "", "what": "", "result": ""})


def test_has_displayable_content_false_when_all_empty():
    assert not has_displayable_content({"context": "", "why": "", "what": "", "result": ""})
    assert not has_displayable_content({})


# ── migration + cursor ───────────────────────────────────────────────────────

def test_migration_creates_table_idempotently():
    conn = _conn()
    migrate_46_topic_summary_cache(conn)  # second run must not raise
    cols = {r[1] for r in conn.execute("PRAGMA table_info(topic_summary_cache)")}
    assert cols == {"thread_id", "last_message_id", "summary_json", "generated_at", "node_signature"}


def test_current_cursor_is_max_message_id_or_zero():
    conn = _conn()
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " thread_id TEXT, role TEXT, content TEXT)"
    )
    assert current_cursor(conn, "t1") == 0  # empty thread
    conn.executemany(
        "INSERT INTO messages (thread_id, role, content) VALUES (?,?,?)",
        [("t1", "user", "a"), ("t1", "assistant", "b"), ("t2", "user", "c")],
    )
    conn.commit()
    assert current_cursor(conn, "t1") == 2
    assert current_cursor(conn, "t2") == 3


# ── persistence + restart survival ───────────────────────────────────────────

def test_upsert_then_read_roundtrip():
    conn = _conn()
    upsert_summary_cache(conn, "t1", 12, _SECTIONS)
    row = read_summary_cache(conn, "t1")
    assert row is not None
    assert row["last_message_id"] == 12
    assert row["sections"] == _SECTIONS


def test_upsert_is_idempotent_last_writer_wins():
    conn = _conn()
    upsert_summary_cache(conn, "t1", 5, {"context": "old", "why": "", "what": "", "result": ""})
    upsert_summary_cache(conn, "t1", 9, {"context": "new", "why": "", "what": "", "result": ""})
    n = conn.execute("SELECT COUNT(*) FROM topic_summary_cache WHERE thread_id='t1'").fetchone()[0]
    assert n == 1  # exactly one row per thread
    row = read_summary_cache(conn, "t1")
    assert row["last_message_id"] == 9 and row["sections"]["context"] == "new"


def test_summary_survives_a_fresh_db_connection(tmp_path):
    """Restart survival: write on one connection, read on a brand-new one."""
    db = str(tmp_path / "cache.db")
    c1 = sqlite3.connect(db)
    c1.row_factory = sqlite3.Row
    migrate_46_topic_summary_cache(c1)
    upsert_summary_cache(c1, "t1", 7, _SECTIONS)
    c1.close()

    c2 = sqlite3.connect(db)  # fresh connection == cockpit restart
    c2.row_factory = sqlite3.Row
    row = read_summary_cache(c2, "t1")
    assert row is not None
    assert decide_summary_action(row["last_message_id"], 7) == "EXACT"
    assert row["sections"] == _SECTIONS
    c2.close()


def test_read_malformed_json_is_a_miss():
    conn = _conn()
    conn.execute(
        "INSERT INTO topic_summary_cache (thread_id, last_message_id, summary_json, generated_at)"
        " VALUES ('t1', 3, 'not-json{', '2026-06-21T00:00:00Z')"
    )
    conn.commit()
    assert read_summary_cache(conn, "t1") is None  # self-healing → FULL next view


# ── modal-facing wrappers (JuggleDB) + restart with zero LLM calls ───────────

def _juggle_db(tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    return db


def test_load_cached_sections_exact_hit_no_llm_after_restart(tmp_path, monkeypatch):
    """After a 'restart' (fresh JuggleDB on the same file), an EXACT hit returns
    the stored summary and the LLM is NOT consulted (AC3)."""
    import juggle_topic_summary as ts
    calls = {"n": 0}
    monkeypatch.setattr(ts, "summarize_topic", lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1) or _SECTIONS))

    db1 = _juggle_db(tmp_path)
    tid = db1.create_thread("Topic one", session_id="")
    db1.add_message(tid, "user", "hi")
    with db1._connect() as conn:
        cur = current_cursor(conn, tid)
    assert cur > 0
    store_summary(db1, tid, cur, _SECTIONS, {})

    # Fresh DB handle on the same file = cockpit restart; cold L1.
    db2 = _juggle_db(tmp_path)
    sections, resolved = load_cached_sections(db2, tid, 1, {})
    assert sections == _SECTIONS
    assert resolved == cur
    assert calls["n"] == 0  # zero LLM invocations on an exact hit


# ── invalidate (r regen) ──────────────────────────────────────────────────

def test_invalidate_summary_cache_drops_l2_row(tmp_path):
    db = _juggle_db(tmp_path)
    store_summary(db, "t1", 5, _SECTIONS, {})
    with db._connect() as conn:
        assert read_summary_cache(conn, "t1") is not None

    invalidate_summary_cache(db, "t1", {})

    with db._connect() as conn:
        assert read_summary_cache(conn, "t1") is None


def test_invalidate_summary_cache_drops_matching_l1_entries():
    l1 = {("t1", 5, ""): _SECTIONS, ("t1", 9, "sig"): _SECTIONS, ("t2", 5, ""): _SECTIONS}
    invalidate_summary_cache(None, "t1", l1)
    assert ("t1", 5, "") not in l1
    assert ("t1", 9, "sig") not in l1
    assert ("t2", 5, "") in l1  # other threads untouched


def test_invalidate_summary_cache_no_db_no_thread_id_is_noop():
    l1 = {("", 5, ""): _SECTIONS}
    invalidate_summary_cache(None, "", l1)  # must not raise
    assert l1 == {("", 5, ""): _SECTIONS}  # key not for this thread_id ("") — untouched by design


def test_store_summary_skips_empty_and_failed_summaries(tmp_path):
    """R7 / B2-Q5: an empty (LLM-failed) summary is NEVER written to L1 or L2;
    a partial-but-displayable one IS (mirrors the any_content display gate).
    (2026-06-21 R7: _fetch_summary cached empty/partial/failed unconditionally.)"""
    db = _juggle_db(tmp_path)
    l1: dict = {}

    # Empty (LLM failed) → no write to either layer.
    store_summary(db, "t1", 5, {"context": "", "why": "", "what": "", "result": ""}, l1)
    with db._connect() as conn:
        assert read_summary_cache(conn, "t1") is None
    assert l1 == {}

    # Partial-but-displayable (1 section) → cached (would display, so cache it).
    partial = {"context": "usable", "why": "", "what": "", "result": ""}
    store_summary(db, "t2", 9, partial, l1)
    with db._connect() as conn:
        row = read_summary_cache(conn, "t2")
    assert row is not None and row["sections"] == partial
    assert l1.get(("t2", 9, "")) == partial
