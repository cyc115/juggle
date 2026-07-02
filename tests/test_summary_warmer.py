"""TDD tests for juggle_summary_warmer — eager (i)-pane summary cache warming.

Proves the watchdog tick can populate topic_summary_cache rows WITHOUT the
modal ever running, so opening the modal for an active topic is a cache-only
read (summary-eager-gen).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_summary_warmer import topic_needs_warming, warm_stale_summaries

_SECTIONS = {"context": "c", "why": "w", "what": "h", "result": "r"}


# ---------------------------------------------------------------------------
# Pure decision — topic_needs_warming
# ---------------------------------------------------------------------------


def test_needs_warming_no_cached_row():
    assert topic_needs_warming(None, current_cursor=5, threshold=3) is True


def test_needs_warming_below_threshold_is_false():
    assert topic_needs_warming(cached_last_message_id=10, current_cursor=11, threshold=3) is False


def test_needs_warming_at_threshold_is_true():
    assert topic_needs_warming(cached_last_message_id=10, current_cursor=13, threshold=3) is True


def test_needs_warming_unchanged_cursor_is_false():
    assert topic_needs_warming(cached_last_message_id=10, current_cursor=10, threshold=3) is False


# ---------------------------------------------------------------------------
# warm_stale_summaries — seeded tmp_path DB, injectable llm_fn (no network)
# ---------------------------------------------------------------------------


def _juggle_db(tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(db_path=str(tmp_path / "j.db"))
    db.init_db()
    return db


def _mock_llm(prompt: str) -> str:
    return (
        "Context: c\nWhy: w\nWhat: h\nResult: r"
    )


def test_warm_populates_cache_for_never_cached_topic(tmp_path):
    """A topic with messages and no cache row gets a cache row after warming —
    proving the cache is populated WITHOUT the modal ever running."""
    db = _juggle_db(tmp_path)
    tid = db.create_thread("Topic one", session_id="")
    db.add_message(tid, "user", "please implement the feature")
    db.add_message(tid, "assistant", "done, implemented it")

    from juggle_topic_summary_cache import read_summary_cache

    with db._connect() as conn:
        assert read_summary_cache(conn, tid) is None

    n = warm_stale_summaries(db, llm_fn=_mock_llm, threshold=3)

    assert n == 1
    with db._connect() as conn:
        row = read_summary_cache(conn, tid)
    assert row is not None
    assert row["sections"]["context"] == "c"


def test_warm_skips_topic_within_threshold(tmp_path):
    """A topic whose cursor has advanced by LESS than threshold since its
    cached row is left alone — the debounce that keeps the tick cheap."""
    db = _juggle_db(tmp_path)
    tid = db.create_thread("Topic two", session_id="")
    db.add_message(tid, "user", "hi")

    from juggle_topic_summary_cache import current_cursor, store_summary

    with db._connect() as conn:
        cur = current_cursor(conn, tid)
    store_summary(db, tid, cur, _SECTIONS, {})

    calls = []
    n = warm_stale_summaries(db, llm_fn=lambda p: calls.append(p) or "x", threshold=3)

    assert n == 0
    assert calls == []


def test_warm_regenerates_topic_past_threshold(tmp_path):
    """Once the cursor has advanced by >= threshold since the cached row, the
    topic is regenerated exactly once."""
    db = _juggle_db(tmp_path)
    tid = db.create_thread("Topic three", session_id="")
    db.add_message(tid, "user", "hi")

    from juggle_topic_summary_cache import current_cursor, store_summary

    with db._connect() as conn:
        cur = current_cursor(conn, tid)
    store_summary(db, tid, cur, _SECTIONS, {})

    for i in range(3):
        db.add_message(tid, "user", f"followup {i}")

    n = warm_stale_summaries(db, llm_fn=_mock_llm, threshold=3)

    assert n == 1
    from juggle_topic_summary_cache import read_summary_cache
    with db._connect() as conn:
        row = read_summary_cache(conn, tid)
    assert row["sections"]["context"] == "c"


def test_warm_skips_archived_topics(tmp_path):
    """An archived topic is never warmed, even with zero cache and messages."""
    db = _juggle_db(tmp_path)
    tid = db.create_thread("Topic four", session_id="")
    db.add_message(tid, "user", "hi")
    db.archive_thread(tid) if hasattr(db, "archive_thread") else db.update_thread(tid, status="archived")

    calls = []
    n = warm_stale_summaries(db, llm_fn=lambda p: calls.append(p) or "x", threshold=3)

    assert n == 0
    assert calls == []


def test_warm_skips_topic_with_no_messages(tmp_path):
    """A brand-new topic with no messages yet has nothing to summarize."""
    db = _juggle_db(tmp_path)
    db.create_thread("Topic five", session_id="")

    n = warm_stale_summaries(db, llm_fn=lambda p: "x", threshold=3)

    assert n == 0


def test_warm_one_bad_topic_does_not_abort_sweep(tmp_path, monkeypatch):
    """A per-topic failure is logged and skipped — the sweep continues to the
    next topic instead of aborting entirely."""
    db = _juggle_db(tmp_path)
    tid_bad = db.create_thread("Topic bad", session_id="")
    db.add_message(tid_bad, "user", "hi")
    tid_good = db.create_thread("Topic good", session_id="")
    db.add_message(tid_good, "user", "hi")

    import juggle_topic_summary_cache as tsc
    real_current_cursor = tsc.current_cursor

    def flaky_current_cursor(conn, thread_id):
        if thread_id == tid_bad:
            raise RuntimeError("boom")
        return real_current_cursor(conn, thread_id)

    monkeypatch.setattr(tsc, "current_cursor", flaky_current_cursor)

    n = warm_stale_summaries(db, llm_fn=_mock_llm, threshold=3)

    assert n == 1
    from juggle_topic_summary_cache import read_summary_cache
    with db._connect() as conn:
        assert read_summary_cache(conn, tid_bad) is None
        assert read_summary_cache(conn, tid_good) is not None


def test_warm_returns_zero_for_none_db():
    assert warm_stale_summaries(None) == 0


def test_warm_caps_regens_per_sweep(tmp_path):
    """A burst of many stale topics (e.g. cold start) must not block a single
    tick for minutes — warm_stale_summaries caps regens per call so backlog
    drains gradually across ticks instead."""
    db = _juggle_db(tmp_path)
    for i in range(5):
        tid = db.create_thread(f"Topic {i}", session_id="")
        db.add_message(tid, "user", "hi")

    n = warm_stale_summaries(db, llm_fn=_mock_llm, threshold=3, max_regens=2)

    assert n == 2
