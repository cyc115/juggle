"""Regression pin (fix-title-gen-thread-leak, 2026-07-04).

Background LLM calls (title generation, summarizers) all funnel through
llm_calls.run_claude_p, which shells out to `claude -p`. That subprocess fires
the Juggle plugin hooks in the *orchestrator's* environment, so the title-gen
prompt was recorded as a USER question on the active thread and the model's
clarifying reply as its ANSWER (even auto-filing an action item).

Fix: run_claude_p tags the subprocess env with JUGGLE_INTERNAL_LLM=1, and the
recording hooks (UserPromptSubmit / Stop) treat that flag as "no thread binding"
and skip entirely. These tests pin both halves.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from juggle_db import JuggleDB
import juggle_hooks
import juggle_hooks_config
import llm_calls


@pytest.fixture
def active_db(tmp_path, monkeypatch):
    # Clear the agent flag: this test process may itself run inside a dispatched
    # agent (JUGGLE_IS_AGENT=1), which independently short-circuits the prompt
    # hook — we want to exercise the JUGGLE_INTERNAL_LLM guard, not that one.
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    db.set_active(True)
    tid = db.create_thread("Real Topic", session_id="s1")
    db.set_current_thread(tid)
    monkeypatch.setattr(juggle_hooks_config, "DB_PATH", db.db_path)
    monkeypatch.setattr(juggle_hooks, "DB_PATH", db.db_path)
    return db


_TITLE_GEN_PROMPT = (
    'Write a specific 4-8 word Title Case title naming what this task is '
    'actually about. Task: "improve dispatch".'
)
_CLARIFYING_REPLY = "No task specified — what should I build? I need more context here."


def _msgs(db):
    return db.get_messages(db.get_current_thread(), token_budget=9999)


def test_internal_llm_prompt_not_recorded_on_active_thread(active_db, monkeypatch):
    monkeypatch.setenv("JUGGLE_INTERNAL_LLM", "1")

    with pytest.raises(SystemExit):
        juggle_hooks.handle_user_prompt_submit({"prompt": _TITLE_GEN_PROMPT})

    assert [m for m in _msgs(active_db) if m["role"] == "user"] == []


def test_internal_llm_reply_not_recorded_or_action_filed(active_db, monkeypatch):
    monkeypatch.setenv("JUGGLE_INTERNAL_LLM", "1")

    with pytest.raises(SystemExit):
        juggle_hooks.handle_stop({"last_assistant_message": _CLARIFYING_REPLY})

    assert [m for m in _msgs(active_db) if m["role"] == "assistant"] == []
    assert active_db.get_open_action_items() == []


def test_control_without_flag_reply_is_recorded(active_db, monkeypatch):
    """Guard sanity: without the flag the same Stop DOES record — proves the
    flag (not some unrelated skip) is what suppresses the leak."""
    monkeypatch.delenv("JUGGLE_INTERNAL_LLM", raising=False)

    with pytest.raises(SystemExit):
        juggle_hooks.handle_stop({"last_assistant_message": _CLARIFYING_REPLY})

    assert [m for m in _msgs(active_db) if m["role"] == "assistant"]


def test_run_claude_p_tags_subprocess_env(monkeypatch):
    captured = {}

    class _Done:
        returncode = 0
        stdout = "Some Title\n"
        stderr = ""

    def _fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return _Done()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    llm_calls.run_claude_p("prompt", model="haiku")

    assert captured["env"] is not None, "run_claude_p must pass an explicit env"
    assert captured["env"].get("JUGGLE_INTERNAL_LLM") == "1"
