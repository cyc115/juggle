"""Lexical dedup guard at the create_thread chokepoint (2026-06-15).

Incident: the SAME work spawned two threads — [A] "slug wheel" (a conversational
brainstorm topic) and [C] "[T-slug-wheel] Topic Slug Wheel: reusable AA-ZZ
rotation…" (the graph tick's execution thread). Semantically identical, born of
two different code paths (manual create-thread vs graph-tick dispatch).

ALL thread creation funnels through JuggleDB.create_thread, so one lexical guard
there covers both origins: before inserting a new row, compare the candidate
title against OPEN same-project threads; on a strong match reuse the existing
thread instead of spawning a twin.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from dbops import db_topics as tp  # noqa: E402
from dbops.threads import (  # noqa: E402
    THREAD_DEDUP_THRESHOLD,
    _normalize_title_tokens,
    _title_similarity,
)
import juggle_graph_dispatch as gd  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    return d


# ---------------------------------------------------------------------------
# 6. Pure-function unit tests for the lexical scorer
# ---------------------------------------------------------------------------


def test_normalize_strips_graph_topic_prefix():
    assert _normalize_title_tokens("[T-slug-wheel] Slug Wheel") == {"slug", "wheel"}


def test_normalize_drops_stopwords_and_punctuation():
    assert _normalize_title_tokens("The slug, of a Wheel!") == {"slug", "wheel"}


def test_normalize_splits_hyphenated_tokens():
    assert "aa" in _normalize_title_tokens("AA-ZZ rotation")
    assert "zz" in _normalize_title_tokens("AA-ZZ rotation")


def test_similarity_prefers_containment_over_jaccard():
    # A is a subset of B: containment = 1.0, jaccard = 2/5 = 0.4 → max picks 1.0
    score = _title_similarity("slug wheel", "slug wheel rotation panel system")
    assert score == pytest.approx(1.0)


def test_similarity_disjoint_is_zero():
    assert _title_similarity("slug wheel", "ticker panel multi instance") == 0.0


def test_similarity_threshold_boundary():
    # 4 of 5 tokens shared → containment 0.8 == threshold (a dup);
    # 3 of 5 shared → 0.6 < threshold (not a dup).
    dup = _title_similarity(
        "alpha beta gamma delta epsilon", "alpha beta gamma delta zeta"
    )
    not_dup = _title_similarity(
        "alpha beta gamma delta epsilon", "alpha beta gamma sigma zeta"
    )
    assert dup >= THREAD_DEDUP_THRESHOLD
    assert not_dup < THREAD_DEDUP_THRESHOLD


# ---------------------------------------------------------------------------
# 1. Manual create-thread reuses an OPEN semantic twin (no new row)
# ---------------------------------------------------------------------------


def test_create_thread_reuses_open_semantic_twin(db):
    existing = db.create_thread("slug wheel", session_id="s")
    before = len(db.get_all_threads())

    got = db.create_thread(
        "[T-slug-wheel] Topic Slug Wheel: reusable AA-ZZ rotation", session_id="s"
    )

    assert got == existing
    assert len(db.get_all_threads()) == before  # no new row inserted


# ---------------------------------------------------------------------------
# 3. Genuinely different titles are NOT merged
# ---------------------------------------------------------------------------


def test_create_thread_distinct_titles_not_merged(db):
    a = db.create_thread("slug wheel", session_id="s")
    b = db.create_thread("ticker panel multi-instance", session_id="s")

    assert a != b
    assert len(db.get_all_threads()) == 2


# ---------------------------------------------------------------------------
# 4. A CLOSED/ARCHIVED twin is NOT a reuse target
# ---------------------------------------------------------------------------


def test_create_thread_ignores_closed_twin(db):
    old = db.create_thread("slug wheel", session_id="s")
    db.set_thread_status(old, "closed")

    fresh = db.create_thread("slug wheel rotation", session_id="s")

    assert fresh != old
    assert db.get_thread(fresh)["state"] == "open"


def test_create_thread_ignores_archived_twin(db):
    old = db.create_thread("slug wheel", session_id="s")
    db.archive_thread(old)

    fresh = db.create_thread("slug wheel rotation", session_id="s")

    assert fresh != old


# ---------------------------------------------------------------------------
# 5. Two threads that already OWN distinct graph topics are never collapsed
# ---------------------------------------------------------------------------


def test_topic_owning_threads_are_not_reuse_targets(db):
    # Two open threads, each bound to its own distinct graph topic, share a
    # title. A new create_thread must NOT collapse onto either — they are real
    # in-flight topics, not free conversational threads.
    t1 = db.create_thread("alpha feature work", session_id="s")
    t2 = db.create_thread("alpha feature work too", session_id="s")
    tp.create_topic(db, topic_id="T-a", project_id="INBOX", title="topic a")
    tp.create_topic(db, topic_id="T-b", project_id="INBOX", title="topic b")
    tp.set_topic_thread(db, "T-a", t1)
    tp.set_topic_thread(db, "T-b", t2)

    fresh = db.create_thread("alpha feature work", session_id="s")

    assert fresh != t1
    assert fresh != t2


# ---------------------------------------------------------------------------
# 2. Graph-tick dispatch path reuses an existing matching OPEN thread
#    (binds the topic via set_topic_thread instead of spawning a twin)
# ---------------------------------------------------------------------------


class _FakeDispatch:
    def __init__(self):
        self.calls = []

    def __call__(self, db, thread_id, prompt, task):
        self.calls.append((thread_id, task["id"]))


def test_dispatch_reuses_existing_open_thread(db):
    # A conversational thread about the same work already exists (unbound).
    convo = db.create_thread("slug wheel", session_id="s")

    # The graph tick is about to dispatch a semantically-identical topic.
    tid = "T-slug-wheel"
    tp.create_topic(
        db,
        topic_id=tid,
        project_id="INBOX",
        title="Topic Slug Wheel: reusable AA-ZZ rotation",
    )
    nid = f"{tid}-k0"
    g.create_task(db, task_id=nid, project_id="INBOX", title=nid, prompt="p")
    g.set_task_topic(db, nid, tid)  # dual-writes nodes.parent_id (P8 Task 4.2)
    tp.recompute_topic_ready(db, "INBOX")
    db.set_setting(gd.ARMED_PROJECT_KEY, "INBOX")

    fake = _FakeDispatch()
    gd.graph_tick(db, dispatch_fn=fake)

    # No twin thread spawned: only the conversational thread exists.
    assert len(db.get_all_threads()) == 1
    # The topic is bound to the reused conversational thread.
    assert tp.get_topic(db, tid)["thread_id"] == convo
    # And the dispatch actually targeted that thread (topic-level dispatch unit).
    assert fake.calls == [(convo, tid)]
