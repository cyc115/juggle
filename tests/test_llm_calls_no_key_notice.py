"""Regression: llm_call() must surface a one-time notice when OPENROUTER_KEY is unset.

Topic BW: missing-key fallback to claude -p was completely silent (no log, no
user-visible signal). This pins a one-time (not per-call) notice.
"""

import logging

import llm_calls


def _reset_notice_guard():
    llm_calls._no_key_notice_shown = False


def test_no_key_notice_fires_on_first_call(monkeypatch, caplog):
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    monkeypatch.setattr(llm_calls, "run_claude_p", lambda *a, **k: "fallback output")
    monkeypatch.setattr(
        "juggle_settings.get_settings",
        lambda: {"llm_profiles": {"cheap": {"fallback_model": "haiku", "max_tokens": 200}}},
    )
    _reset_notice_guard()

    with caplog.at_level(logging.WARNING):
        result = llm_calls.llm_call("hello", profile="cheap")

    assert result == "fallback output"
    notices = [r for r in caplog.records if "OPENROUTER_KEY" in r.message]
    assert len(notices) == 1


def test_no_key_notice_fires_only_once_across_calls(monkeypatch, caplog):
    monkeypatch.delenv("OPENROUTER_KEY", raising=False)
    monkeypatch.setattr(llm_calls, "run_claude_p", lambda *a, **k: "fallback output")
    monkeypatch.setattr(
        "juggle_settings.get_settings",
        lambda: {"llm_profiles": {"cheap": {"fallback_model": "haiku", "max_tokens": 200}}},
    )
    _reset_notice_guard()

    with caplog.at_level(logging.WARNING):
        llm_calls.llm_call("hello", profile="cheap")
        llm_calls.llm_call("world", profile="cheap")

    notices = [r for r in caplog.records if "OPENROUTER_KEY" in r.message]
    assert len(notices) == 1


def test_notice_not_shown_when_key_present(monkeypatch, caplog):
    monkeypatch.setenv("OPENROUTER_KEY", "sk-fake")
    monkeypatch.setattr(
        "juggle_settings.get_settings",
        lambda: {"llm_profiles": {"cheap": {"openrouter_model": "m", "fallback_model": "haiku", "max_tokens": 200}}},
    )

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            import json
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: _Resp())
    _reset_notice_guard()

    with caplog.at_level(logging.WARNING):
        result = llm_calls.llm_call("hello", profile="cheap")

    assert result == "ok"
    notices = [r for r in caplog.records if "OPENROUTER_KEY" in r.message]
    assert len(notices) == 0
