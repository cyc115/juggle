"""Regression pin (2026-06-17): _TopicDetailModal._fetch_summary called
self.call_from_thread() which doesn't exist on ModalScreen — must use
self.app.call_from_thread() instead.
"""
import os
import sys
import types
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _make_modal_instance():
    """Build a minimal stand-in for _TopicDetailModal with no call_from_thread."""
    from juggle_cockpit_modals import _TopicDetailModal

    # Minimal topic namedtuple-like object
    topic = types.SimpleNamespace(
        label="T1",
        title="Test topic",
        status="active",
    )
    extra = {
        "task_input": "do the thing",
        "result_output": "done",
        "messages_all": [],
        "thread_id": "thread-abc",
        "message_count": 3,
    }
    # Instantiate without __init__ to avoid Textual widget machinery
    obj = object.__new__(_TopicDetailModal)
    obj._topic = topic
    obj._extra = extra
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
    from juggle_cockpit_modals import _TopicDetailModal
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
