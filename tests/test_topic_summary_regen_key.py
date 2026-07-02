"""TDD (2026-07-01): (i)nfo modal — pressing 'r' forces a summary regeneration.

Pins: 'r' invalidates the cached row and triggers exactly one regeneration
worker; the modal shows a visible 'regenerating…' state while it runs; the
'r regen' hint is present in the rendered key legend.
"""
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class _FakeKey:
    def __init__(self, key):
        self.key = key
        self.stopped = False

    def stop(self):
        self.stopped = True


class _FakeStatic:
    def __init__(self):
        self.updates = []

    def update(self, text):
        self.updates.append(text)


def _make_modal_instance(messages_all=None):
    from juggle_cockpit_modals import _NodeDetailModal

    summary_ctx = {
        "task_input": "do the thing",
        "result_output": "done",
        "messages_all": messages_all if messages_all is not None else [{"role": "user", "content": "hi"}],
        "thread_id": "thread-regen",
        "message_count": 3,
    }
    obj = object.__new__(_NodeDetailModal)
    obj._node = {"id": "T1", "title": "Test topic", "state": "active"}
    obj._summary_ctx = summary_ctx
    obj._label = "T1"
    obj._is_topic = True
    obj._cursor = 3
    obj._node_sig = ""
    return obj


def test_r_key_invalidates_cache_and_triggers_one_regeneration(monkeypatch):
    import juggle_cockpit_modals as mod

    mod._topic_summary_cache[("thread-regen", 3, "")] = {"context": "stale", "why": "", "what": "", "result": ""}

    obj = _make_modal_instance()
    body = _FakeStatic()
    obj.query_one = lambda *a, **k: body

    invalidate_calls = []
    monkeypatch.setattr(
        "juggle_topic_summary_cache.invalidate_summary_cache",
        lambda db, thread_id, l1: invalidate_calls.append((thread_id, l1)),
    )

    worker_calls = []
    obj.run_worker = lambda fn, thread=False: worker_calls.append((fn, thread))

    app_mock = mock.MagicMock()
    app_mock._db = None
    with mock.patch.object(type(obj), "app", new_callable=lambda: property(lambda self: app_mock)):
        ev = _FakeKey("r")
        obj.on_key(ev)

    assert ev.stopped
    assert invalidate_calls == [("thread-regen", mod._topic_summary_cache)]
    assert len(worker_calls) == 1
    assert worker_calls[0][0].__name__ == "_fetch_summary"
    assert worker_calls[0][1] is True
    assert body.updates == ["Regenerating…"]


def test_r_key_noop_when_no_conversation():
    """Unbound topic (no messages_all) — 'r' does nothing (nothing to regenerate)."""
    obj = _make_modal_instance(messages_all=[])
    obj.run_worker = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run worker"))
    obj.query_one = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not touch body"))

    ev = _FakeKey("r")
    obj.on_key(ev)
    assert not ev.stopped


def test_regen_hint_present_in_apply_summary_legend():
    obj = _make_modal_instance()
    body = _FakeStatic()
    obj.query_one = lambda *a, **k: body

    obj._apply_summary({"context": "c", "why": "w", "what": "h", "result": "r"})

    assert any("r — regen" in u for u in body.updates)
