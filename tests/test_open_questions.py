"""Tests for open_questions tracking (Items 2 + 5)."""

import json
import os
import sys
from unittest import mock
from pathlib import Path

SRC_DIR = str(Path(__file__).parent.parent / "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)


def test_record_pending_decision_adds_entry():
    """record-pending-decision should append to open_questions."""
    from juggle_cli import cmd_record_pending_decision

    mock_db = mock.MagicMock()
    mock_thread = {"id": "thread-1", "open_questions": []}
    mock_db.get_current_thread.return_value = "thread-1"
    mock_db.get_thread.return_value = mock_thread

    args = mock.MagicMock()
    args.tool_use_id = "tool123"
    args.questions_json = json.dumps([
        {"q": "Which model?"},
        {"q": "Batch size?"}
    ])

    with mock.patch("juggle_cli.get_db", return_value=mock_db):
        cmd_record_pending_decision(args)

    assert mock_db.update_thread.called
    call_kwargs = mock_db.update_thread.call_args.kwargs
    updated_questions = call_kwargs.get("open_questions", [])

    assert len(updated_questions) == 2
    assert updated_questions[0]["id"] == "tool123:0"
    assert updated_questions[0]["text"] == "Which model?"


def test_clear_pending_decision_removes_entries():
    """clear-pending-decision should remove entries by tool_use_id."""
    from juggle_cli import cmd_clear_pending_decision

    mock_db = mock.MagicMock()
    mock_thread = {
        "id": "thread-1",
        "open_questions": [
            {"id": "tool123:0", "text": "Q1"},
            {"id": "tool456:0", "text": "Q2"},
            {"id": "tool123:1", "text": "Q3"},
        ]
    }
    mock_db.get_current_thread.return_value = "thread-1"
    mock_db.get_thread.return_value = mock_thread

    args = mock.MagicMock()
    args.tool_use_id = "tool123"

    with mock.patch("juggle_cli.get_db", return_value=mock_db):
        cmd_clear_pending_decision(args)

    call_kwargs = mock_db.update_thread.call_args.kwargs
    remaining = call_kwargs.get("open_questions", [])

    assert len(remaining) == 1
    assert remaining[0]["id"] == "tool456:0"


def test_hook_skips_if_no_current_thread():
    """Hooks should silently skip if user is in main thread."""
    from juggle_cli import cmd_record_pending_decision

    mock_db = mock.MagicMock()
    mock_db.get_current_thread.return_value = None

    args = mock.MagicMock()
    args.tool_use_id = "test"
    args.questions_json = "[]"

    with mock.patch("juggle_cli.get_db", return_value=mock_db):
        cmd_record_pending_decision(args)

    mock_db.update_thread.assert_not_called()
