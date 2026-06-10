"""Tests for open_questions tracking via the AskUserQuestion hooks.

The live path is entirely in juggle_hooks.py: handle_pre_tool_use records
pending decisions into the current thread's open_questions (and files a cockpit
action item); handle_post_tool_use clears them once the tool completes. The old
record/clear-pending-decision CLI commands that duplicated this logic were
removed — these tests cover the handlers that actually run.
"""

import json
import sys
from pathlib import Path

import pytest

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from juggle_db import JuggleDB
import juggle_hooks
import juggle_hooks_config


def _open_questions(thread) -> list:
    oq = thread.get("open_questions") or []
    return json.loads(oq) if isinstance(oq, str) else oq


@pytest.fixture
def hooked_db(tmp_path, monkeypatch):
    """Real DB wired into the hook module via get_db()/is_active() overrides.

    Avoids depending on CLAUDE_PLUGIN_DATA-based DB redirection so the test is
    independent of the ambient settings/environment.
    """
    db = JuggleDB(str(tmp_path / "juggle.db"))
    db.init_db()
    monkeypatch.setattr(juggle_hooks_config, "is_active", lambda: True)
    monkeypatch.setattr(juggle_hooks_config, "get_db", lambda: db)
    monkeypatch.delenv("JUGGLE_IS_AGENT", raising=False)
    return db


def test_pre_tool_use_records_open_questions(hooked_db):
    tid = hooked_db.create_thread("Topic A", session_id="s1")
    hooked_db.set_current_thread(tid)
    data = {
        "tool_name": "AskUserQuestion",
        "tool_use_id": "tool123",
        "tool_input": {
            "questions": [{"question": "Which model?"}, {"question": "Batch size?"}]
        },
    }

    with pytest.raises(SystemExit):
        juggle_hooks.handle_pre_tool_use(data)

    questions = _open_questions(hooked_db.get_thread(tid))
    assert len(questions) == 2
    assert questions[0]["id"] == "tool123:0"
    assert questions[0]["text"] == "Which model?"
    assert questions[1]["id"] == "tool123:1"

    # A cockpit action item is filed for the decision.
    items = hooked_db.get_open_action_items()
    assert any(i["message"].startswith("[tuid:tool123]") for i in items)


def test_post_tool_use_clears_open_questions(hooked_db):
    tid = hooked_db.create_thread("Topic A", session_id="s1")
    hooked_db.set_current_thread(tid)
    hooked_db.update_thread(
        tid,
        open_questions=[
            {"id": "tool123:0", "text": "Q1", "source": "askuser"},
            {"id": "tool456:0", "text": "Q2", "source": "askuser"},
            {"id": "tool123:1", "text": "Q3", "source": "askuser"},
        ],
    )
    hooked_db.add_action_item(
        thread_id=tid,
        message="[tuid:tool123] Decision needed: Q1 / Q3",
        type_="decision",
        priority="normal",
    )

    data = {"tool_name": "AskUserQuestion", "tool_use_id": "tool123"}

    with pytest.raises(SystemExit):
        juggle_hooks.handle_post_tool_use(data)

    questions = _open_questions(hooked_db.get_thread(tid))
    assert len(questions) == 1
    assert questions[0]["id"] == "tool456:0"

    # The matching cockpit action item is dismissed.
    items = hooked_db.get_open_action_items()
    assert not any(i["message"].startswith("[tuid:tool123]") for i in items)


def test_pre_tool_use_skips_when_no_current_thread(hooked_db):
    # Active, but no current thread selected (user is in the main thread).
    data = {
        "tool_name": "AskUserQuestion",
        "tool_use_id": "tool123",
        "tool_input": {"questions": [{"question": "Anything?"}]},
    }

    with pytest.raises(SystemExit):
        juggle_hooks.handle_pre_tool_use(data)

    assert hooked_db.get_open_action_items() == []
