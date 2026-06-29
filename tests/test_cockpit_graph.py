"""Tests for cockpit graph-task visibility (autopilot Phase 4, DA m2).

Project rows show aggregate task progress ('3/14 done, 1 failed, 2 ready')
and task-bound topics get their glyph from graph_tasks.state — NOT from
thread status or TTL, so the un-instantiated tail and done tasks stay
visible regardless of thread lifecycle.
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from juggle_db import JuggleDB  # noqa: E402
from dbops import db_graph as g  # noqa: E402
from juggle_cockpit_model import Topic, snapshot  # noqa: E402
from juggle_cockpit_view import TASK_STATE_GLYPHS, render_topics  # noqa: E402


@pytest.fixture
def db(tmp_path: Path) -> JuggleDB:
    d = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    d.init_db()
    return d


def _render_text(panel, width: int = 100) -> str:
    from rich.console import Console

    buf = io.StringIO()
    Console(width=width, file=buf, no_color=True, highlight=False).print(panel)
    return buf.getvalue()


def _topic(label="T", status="running", task_state=None, project_id="INBOX"):
    return Topic(
        id=f"id-{label}",
        label=label,
        status=status,
        age_secs=10,
        is_current=False,
        title=f"Topic {label}",
        project_id=project_id,
        project_name="Inbox",
        task_state=task_state,
    )


# ── snapshot plumbing ─────────────────────────────────────────────────────────


def test_snapshot_exposes_graph_by_project(db):
    # P8: snapshot reads graph_by_project from nodes; seed both nodes and
    # legacy graph_tasks (dual-write kept in P8).
    g.create_task(db, task_id="a", project_id="INBOX", title="A", prompt="p")
    g.create_task(db, task_id="b", project_id="INBOX", title="B", prompt="p")
    with db._connect() as conn:
        # create_task dual-writes the nodes rows (state 'open'); force 'a' to
        # verified in BOTH stores (snapshot reads graph_by_project from nodes).
        conn.execute("UPDATE graph_tasks SET state='verified' WHERE id='a'")
        conn.execute("UPDATE nodes SET state='verified' WHERE id='a'")
        conn.commit()
    state = snapshot(db)
    assert state.graph_by_project is not None
    counts = state.graph_by_project["INBOX"]
    assert counts["total"] == 2 and counts["verified"] == 1


def test_snapshot_graph_none_when_no_tasks(db):
    state = snapshot(db)
    assert state.graph_by_project is None


def test_snapshot_topic_task_state_none_always(db):
    """P8 pin: task_state is always None — the graph_tasks.thread_id join was
    removed in P8 (nodes has no thread_id column; task_state_by_thread deleted).

    2026-06-20 behavior change from DA m2 (2026-06-10): task_state no longer
    shows a glyph in the thread panel; topics render via thread status only.
    """
    tid = db.create_thread("[a] task thread", session_id="s")
    g.create_task(db, task_id="a", project_id="INBOX", title="A", prompt="p")
    with db._connect() as conn:
        conn.execute(
            "UPDATE graph_tasks SET state='verified', thread_id=? WHERE id='a'", (tid,)
        )
        conn.commit()
    state = snapshot(db)
    topic = next(t for t in state.topics if t.id == tid)
    assert topic.status != "verified"  # thread status unchanged
    assert topic.task_state is None  # P8: no longer read from graph_tasks.thread_id


def test_snapshot_topic_task_state_none_for_unbound(db):
    db.create_thread("plain thread", session_id="s")
    state = snapshot(db)
    assert all(t.task_state is None for t in state.topics)


# ── glyphs (DA m2: sourced from graph_tasks.state) ────────────────────────────


def test_all_task_states_have_glyphs():
    for s in g.VALID_STATES:
        assert s in TASK_STATE_GLYPHS, f"no cockpit glyph for task state {s!r}"


def test_task_bound_topic_uses_task_glyph_not_thread_status():
    topics = [_topic(label="N", status="running", task_state="verified")]
    text = _render_text(render_topics(topics, "wide"))
    assert TASK_STATE_GLYPHS["verified"] in text


def test_unbound_topic_keeps_thread_status_glyph():
    from juggle_cockpit_view import TOPIC_STATUS_GLYPHS

    topics = [_topic(label="P", status="running", task_state=None)]
    text = _render_text(render_topics(topics, "wide"))
    assert TOPIC_STATUS_GLYPHS["running"] in text


# ── aggregate project row (DA m2) ─────────────────────────────────────────────


def test_project_header_shows_aggregate_progress():
    topics = [
        _topic(label="A", project_id="P1"),
        _topic(label="B", project_id="INBOX"),
    ]
    projects = {"P1": "Alpha", "INBOX": "Inbox"}
    graph = {
        "P1": {
            "total": 14,
            "verified": 3,
            "failed": 1,
            "blocked": 0,
            "ready": 2,
            "running": 0,
            "pending": 8,
        }
    }
    text = _render_text(
        render_topics(topics, "wide", projects, graph_by_project=graph)
    )
    assert "3/14 done, 1 failed, 2 ready" in text


def test_project_header_no_progress_without_graph():
    topics = [
        _topic(label="A", project_id="P1"),
        _topic(label="B", project_id="INBOX"),
    ]
    projects = {"P1": "Alpha", "INBOX": "Inbox"}
    text = _render_text(render_topics(topics, "wide", projects))
    assert "done" not in text  # no aggregate fragment leaks without graph data


def test_armed_project_header_visible_with_zero_visible_topics():
    """REGRESSION PIN (DA round-2 minor 3, 2026-06-10): an armed project whose
    tasks have no live threads yet (or whose threads all aged out of the TTL
    window) had NO topics — group_threads_by_project dropped it entirely, so
    the aggregate '⬢ x/y done' row vanished from the cockpit exactly when the
    operator most needs it. A header row must be synthesized for any project
    in graph_by_project."""
    topics = [_topic(label="B", project_id="INBOX")]  # zero topics in P1
    projects = {"P1": "Alpha", "INBOX": "Inbox"}
    graph = {
        "P1": {
            "total": 14,
            "verified": 3,
            "failed": 1,
            "blocked": 0,
            "ready": 2,
            "running": 0,
            "pending": 8,
        }
    }
    text = _render_text(
        render_topics(topics, "wide", projects, graph_by_project=graph)
    )
    assert "ALPHA" in text
    assert "3/14 done, 1 failed, 2 ready" in text


def test_graph_project_header_even_when_it_is_the_only_project():
    """Companion to the minor-3 pin: grouping used to require >1 project —
    a lone armed project with zero visible topics rendered nothing at all."""
    topics: list = []
    projects = {"P1": "Alpha"}
    graph = {"P1": {"total": 2, "verified": 1, "failed": 0, "blocked": 0,
                    "ready": 1, "running": 0, "pending": 0}}
    text = _render_text(
        render_topics(topics, "wide", projects, graph_by_project=graph)
    )
    assert "ALPHA" in text
    assert "1/2 done" in text
