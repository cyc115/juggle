"""Regression pin (2026-06-17): the topic-detail modal's _fetch_summary called
self.call_from_thread() which doesn't exist on ModalScreen — must use
self.app.call_from_thread() instead. (FM: assert through the unified
_NodeDetailModal._fetch_summary seam.)
"""
import os
import sys
import types
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _make_modal_instance():
    """Build a minimal _NodeDetailModal stand-in with no call_from_thread."""
    from juggle_cockpit_modals import _NodeDetailModal

    summary_ctx = {
        "task_input": "do the thing",
        "result_output": "done",
        "messages_all": [],
        "thread_id": "thread-abc",
        "message_count": 3,
    }
    # Instantiate without __init__ to avoid Textual widget machinery
    obj = object.__new__(_NodeDetailModal)
    obj._node = {"id": "T1", "title": "Test topic", "state": "active"}
    obj._summary_ctx = summary_ctx
    obj._label = "T1"
    obj._is_topic = True
    obj._cursor = 3
    return obj


def test_fetch_summary_uses_app_call_from_thread(monkeypatch):
    """_fetch_summary must call self.app.call_from_thread, not self.call_from_thread.

    Regression: before the fix, this raised AttributeError because ModalScreen
    has no call_from_thread — only App does.
    """
    import juggle_cockpit_modals as mod

    obj = _make_modal_instance()

    # Mock summarize_topic so no network needed
    fake_sections = {"context": "ctx", "why": "why", "what": "what", "result": "res"}
    monkeypatch.setattr(
        "juggle_topic_summary.summarize_topic",
        lambda *a, **kw: fake_sections,
    )

    # Patch the `app` property at class level to return our mock
    app_mock = mock.MagicMock()
    with mock.patch.object(type(obj), "app", new_callable=lambda: property(lambda self: app_mock)):
        # self must NOT have call_from_thread (simulates ModalScreen — only App has it)
        assert not hasattr(obj, "call_from_thread"), (
            "Test setup error: obj should not have call_from_thread"
        )

        # This should NOT raise AttributeError after the fix
        obj._fetch_summary()

    # app.call_from_thread must have been called with _apply_summary + sections
    app_mock.call_from_thread.assert_called_once()
    args = app_mock.call_from_thread.call_args[0]
    assert args[0].__name__ == "_apply_summary"
    assert args[1] == fake_sections


def test_fetch_summary_does_not_cache_empty_or_failed_summary(monkeypatch):
    """R7 (2026-06-21): _fetch_summary cached empty/partial/LLM-failed summaries
    unconditionally (juggle_cockpit_modals.py:600-606), so a broken summary was
    served on every later open. An all-blank (LLM-failed) summary must NEVER be
    cached → next view re-derives; the raw fallback still renders this view.
    """
    import juggle_cockpit_modals as mod

    obj = _make_modal_instance()
    obj._summary_ctx = {**obj._summary_ctx, "thread_id": "thread-r7-empty", "message_count": 3}
    mod._topic_summary_cache.pop(("thread-r7-empty", 3), None)

    empty_sections = {"context": "", "why": "", "what": "", "result": ""}
    monkeypatch.setattr(
        "juggle_topic_summary.summarize_topic",
        lambda *a, **kw: empty_sections,
    )

    app_mock = mock.MagicMock()
    app_mock._db = None  # no L2 in this unit test — assert the L1 write-gate only
    with mock.patch.object(type(obj), "app", new_callable=lambda: property(lambda self: app_mock)):
        obj._fetch_summary()

    # R7: an empty/failed summary is never persisted to L1.
    assert ("thread-r7-empty", 3) not in mod._topic_summary_cache
    # The raw fallback still renders this view (sections handed to _apply_summary).
    app_mock.call_from_thread.assert_called_once()
    assert app_mock.call_from_thread.call_args[0][1] == empty_sections
