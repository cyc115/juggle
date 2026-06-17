"""TDD tests for the T hotkey task-detail modal feature.

Covers:
  1. resolve_task_detail — pure resolver (exact id, prefix, label, case, not-found, ambiguous)
  2. BINDINGS — "T" / "shift+t" mapped to task_detail action
  3. action_task_detail method present on CockpitApp
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task(id_, label=None, deps=None):
    t = {"id": id_, "title": f"title-{id_}", "state": "pending", "deps": deps or []}
    if label is not None:
        t["_label"] = label
    return t


# ---------------------------------------------------------------------------
# Cycle 1 — resolve_task_detail pure resolver
# ---------------------------------------------------------------------------


def test_resolve_exact_id():
    """Returns (task, deps) for an exact task-id match."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("AA"), _task("BB")]
    result = resolve_task_detail(tasks, "AA")
    assert result is not None
    task, deps = result
    assert task["id"] == "AA"
    assert deps == []


def test_resolve_exact_id_case_insensitive():
    """Exact id match is case-insensitive."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("AA"), _task("BB")]
    result = resolve_task_detail(tasks, "aa")
    assert result is not None
    assert result[0]["id"] == "AA"


def test_resolve_unique_prefix():
    """Returns task for unique prefix match when no exact id exists."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("ABC"), _task("XYZ")]
    result = resolve_task_detail(tasks, "AB")
    assert result is not None
    assert result[0]["id"] == "ABC"


def test_resolve_exact_beats_prefix():
    """Exact match wins over a prefix match for the same query string."""
    from juggle_cockpit_modals import resolve_task_detail

    # "AB" is both the exact id of one task and a prefix of another
    tasks = [_task("AB"), _task("ABC")]
    result = resolve_task_detail(tasks, "AB")
    assert result is not None
    assert result[0]["id"] == "AB"


def test_resolve_ambiguous_prefix_returns_none():
    """Ambiguous prefix (matches multiple tasks) returns None."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("ABC"), _task("ABD")]
    result = resolve_task_detail(tasks, "AB")
    assert result is None


def test_resolve_label_match():
    """Matches via _label field (thread user_label slug)."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("T1", label="AI"), _task("T2", label="BC")]
    result = resolve_task_detail(tasks, "AI")
    assert result is not None
    assert result[0]["id"] == "T1"


def test_resolve_label_case_insensitive():
    """Label match is case-insensitive."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("T1", label="AI")]
    assert resolve_task_detail(tasks, "ai") is not None
    assert resolve_task_detail(tasks, "Ai") is not None


def test_resolve_not_found_returns_none():
    """Returns None when query matches nothing."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("AA"), _task("BB")]
    assert resolve_task_detail(tasks, "ZZ") is None


def test_resolve_empty_tasks_returns_none():
    """Returns None for empty task list."""
    from juggle_cockpit_modals import resolve_task_detail

    assert resolve_task_detail([], "AA") is None


def test_resolve_deps_from_task_field():
    """Returns deps list from the matched task's own deps field."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("AA", deps=["BB", "CC"])]
    result = resolve_task_detail(tasks, "AA")
    assert result is not None
    assert result[1] == ["BB", "CC"]


def test_resolve_prefix_case_insensitive():
    """Prefix match is case-insensitive."""
    from juggle_cockpit_modals import resolve_task_detail

    tasks = [_task("ABC")]
    result = resolve_task_detail(tasks, "ab")
    assert result is not None
    assert result[0]["id"] == "ABC"


# ---------------------------------------------------------------------------
# Cycle 4 — resolve_thread_detail: thread-label-first lookup (bug fix)
# ---------------------------------------------------------------------------


def _make_topic(label, title="", status="active", tid=None, task_state=None):
    from juggle_cockpit_model import Topic
    return Topic(
        id=tid or f"uuid-{label.lower()}",
        label=label,
        status=status,
        age_secs=0,
        is_current=False,
        title=title,
        task_state=task_state,
    )


def test_resolve_thread_detail_by_label():
    """resolve_thread_detail returns the Topic whose label matches the query."""
    from juggle_cockpit_modals import resolve_thread_detail

    topics = [_make_topic("AO", title="My Topic"), _make_topic("AM", title="Other")]
    result = resolve_thread_detail(topics, "AO")
    assert result is not None
    assert result.label == "AO"
    assert result.title == "My Topic"


def test_resolve_thread_detail_case_insensitive():
    """resolve_thread_detail matches case-insensitively."""
    from juggle_cockpit_modals import resolve_thread_detail

    topics = [_make_topic("AO")]
    assert resolve_thread_detail(topics, "ao") is not None
    assert resolve_thread_detail(topics, "Ao") is not None
    assert resolve_thread_detail(topics, "AO") is not None


def test_resolve_thread_detail_not_found():
    """resolve_thread_detail returns None when no topic matches."""
    from juggle_cockpit_modals import resolve_thread_detail

    topics = [_make_topic("AO"), _make_topic("AM")]
    assert resolve_thread_detail(topics, "ZZ") is None


def test_resolve_thread_detail_empty_topics():
    """resolve_thread_detail returns None for empty list."""
    from juggle_cockpit_modals import resolve_thread_detail

    assert resolve_thread_detail([], "AO") is None


def test_resolve_thread_detail_empty_query():
    """resolve_thread_detail returns None for empty query."""
    from juggle_cockpit_modals import resolve_thread_detail

    topics = [_make_topic("AO")]
    assert resolve_thread_detail(topics, "") is None
    assert resolve_thread_detail(topics, None) is None


def test_topic_detail_modal_exists():
    """_TopicDetailModal class must be importable from juggle_cockpit_modals."""
    from juggle_cockpit_modals import _TopicDetailModal  # noqa: F401
    assert _TopicDetailModal is not None


def test_topic_detail_modal_accepts_topic():
    """_TopicDetailModal can be constructed with a Topic and optional extra dict."""
    from juggle_cockpit_modals import _TopicDetailModal
    topic = _make_topic("AO", title="My Topic")
    modal = _TopicDetailModal(topic)
    assert modal is not None


def test_topic_detail_modal_lines_contain_fields():
    """_TopicDetailModal._lines() includes label, title, and state."""
    from juggle_cockpit_modals import _TopicDetailModal
    topic = _make_topic("AO", title="My Topic", status="active")
    modal = _TopicDetailModal(topic)
    lines = "\n".join(modal._lines())
    assert "AO" in lines
    assert "My Topic" in lines
    assert "active" in lines


def test_topic_detail_modal_shows_graph_task_state():
    """When topic.task_state is set, _lines() includes it."""
    from juggle_cockpit_modals import _TopicDetailModal
    topic = _make_topic("AO", title="My Topic", task_state="running")
    modal = _TopicDetailModal(topic)
    lines = "\n".join(modal._lines())
    assert "running" in lines


def test_topic_detail_modal_extra_data():
    """_TopicDetailModal._lines() includes extra data (agent, summary, recent_msg)."""
    from juggle_cockpit_modals import _TopicDetailModal
    topic = _make_topic("AO", title="My Topic")
    modal = _TopicDetailModal(topic, extra={"agent": "coder-abc", "summary": "A summary", "recent_msg": "Hello"})
    lines = "\n".join(modal._lines())
    assert "coder-abc" in lines
    assert "A summary" in lines
    assert "Hello" in lines


# ---------------------------------------------------------------------------
# Cycle 2 — BINDINGS: "T" or "shift+t" maps to task_detail
# ---------------------------------------------------------------------------


def _task_detail_binding():
    from juggle_cockpit import CockpitApp
    for b in CockpitApp.BINDINGS:
        if b.action == "task_detail":
            return b
    return None


def test_task_detail_binding_exists():
    """CockpitApp.BINDINGS must have a binding for action 'task_detail'."""
    b = _task_detail_binding()
    assert b is not None, "No binding with action='task_detail' found in CockpitApp.BINDINGS"


def test_task_detail_binding_key():
    """The task_detail binding uses key 'i' (information mnemonic)."""
    b = _task_detail_binding()
    assert b is not None
    assert b.key == "i", (
        f"Expected 'i', got '{b.key}'"
    )


def test_task_detail_binding_not_T():
    """'T' must NOT be bound to task_detail (rebind to 'i')."""
    from juggle_cockpit import CockpitApp
    for b in CockpitApp.BINDINGS:
        if b.key in ("T", "shift+t"):
            assert b.action != "task_detail", (
                "'T'/'shift+t' must no longer be bound to task_detail"
            )


# ---------------------------------------------------------------------------
# Cycle 3 — action_task_detail method present
# ---------------------------------------------------------------------------


def test_action_task_detail_method_exists():
    """CockpitApp must have an action_task_detail method."""
    from juggle_cockpit import CockpitApp
    assert hasattr(CockpitApp, "action_task_detail"), (
        "CockpitApp missing action_task_detail method"
    )
    assert callable(CockpitApp.action_task_detail)


# ---------------------------------------------------------------------------
# Cycle 5 — enriched _TopicDetailModal sections (task_input, result_output, recent)
# ---------------------------------------------------------------------------


def test_topic_detail_modal_shows_task_input():
    """_TopicDetailModal renders task_input section when provided in extra."""
    from juggle_cockpit_modals import _TopicDetailModal
    topic = _make_topic("AO", title="My Topic")
    modal = _TopicDetailModal(topic, extra={"task_input": "Please implement feature X"})
    lines = "\n".join(modal._lines())
    assert "task" in lines.lower() and "input" in lines.lower(), (
        "Expected 'task' and 'input' header in modal lines"
    )
    assert "Please implement feature X" in lines


def test_topic_detail_modal_shows_result_output():
    """_TopicDetailModal renders result_output section when provided in extra."""
    from juggle_cockpit_modals import _TopicDetailModal
    topic = _make_topic("AO", title="My Topic")
    modal = _TopicDetailModal(topic, extra={"result_output": "Done: feature implemented"})
    lines = "\n".join(modal._lines())
    assert ("output" in lines.lower() or "result" in lines.lower()), (
        "Expected 'output' or 'result' header in modal lines"
    )
    assert "Done: feature implemented" in lines


def test_topic_detail_modal_shows_recent():
    """_TopicDetailModal renders recent activity list when provided in extra."""
    from juggle_cockpit_modals import _TopicDetailModal
    topic = _make_topic("AO", title="My Topic")
    recent = [
        {"role": "user", "content": "check this please"},
        {"role": "assistant", "content": "all done"},
    ]
    modal = _TopicDetailModal(topic, extra={"recent": recent})
    lines = "\n".join(modal._lines())
    assert "recent" in lines.lower(), "Expected 'recent' header in modal lines"
    assert "check this please" in lines
    assert "all done" in lines
