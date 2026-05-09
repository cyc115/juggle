import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import pytest
from unittest.mock import patch


def _make_pre_data(tool_use_id, questions):
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "AskUserQuestion",
        "tool_use_id": tool_use_id,
        "tool_input": {"questions": [{"question": q} for q in questions]},
    }


def test_askuser_creates_action_item(tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    tid = db.create_thread("test", session_id="s1")
    db.set_active(True)
    db.set_current_thread(tid)

    data = _make_pre_data("tuid-abc", ["Option A?", "Option B?"])

    with patch("juggle_hooks.is_active", return_value=True), \
         patch("juggle_hooks.get_db", return_value=db):
        from juggle_hooks import handle_pre_tool_use
        with pytest.raises(SystemExit):
            handle_pre_tool_use(data)

    items = db.get_open_action_items()
    assert len(items) == 1
    assert items[0]["message"] == "[tuid:tuid-abc] Decision needed: Option A? / Option B?"
    assert items[0]["type"] == "decision"


def test_askuser_dismisses_action_item_on_answer(tmp_path):
    from juggle_db import JuggleDB
    db = JuggleDB(db_path=str(tmp_path / "juggle.db"))
    db.init_db()
    tid = db.create_thread("test", session_id="s1")
    db.set_active(True)
    db.set_current_thread(tid)

    # Pre-create the action item (as PreToolUse would)
    db.add_action_item(
        thread_id=tid,
        message="[tuid:tuid-xyz] Decision needed: A? / B?",
        type_="decision",
        priority="normal",
    )
    assert len(db.get_open_action_items()) == 1

    post_data = {
        "hook_event_name": "PostToolUse",
        "tool_name": "AskUserQuestion",
        "tool_use_id": "tuid-xyz",
        "tool_input": {},
        "tool_response": {},
    }

    with patch("juggle_hooks.is_active", return_value=True), \
         patch("juggle_hooks.get_db", return_value=db):
        from juggle_hooks import handle_post_tool_use
        with pytest.raises(SystemExit):
            handle_post_tool_use(post_data)

    assert len(db.get_open_action_items()) == 0
