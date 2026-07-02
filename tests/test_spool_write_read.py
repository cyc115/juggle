import json
import os
from pathlib import Path

import pytest

from dbops.spool import SpoolEvent, write_event, read_pending, move_to_dead


@pytest.fixture
def spool_dir(tmp_path):
    d = tmp_path / "spool"
    d.mkdir()
    return d


def test_write_event_creates_json_file_named_by_ts_agent_seq(spool_dir):
    uuid = write_event(spool_dir, "agent_complete", "agent-123", "thread-abc", {"summary": "ok"})
    files = list(spool_dir.glob("*.json"))
    assert len(files) == 1
    assert "agent-123" in files[0].name
    payload = json.loads(files[0].read_text())
    assert payload["uuid"] == uuid
    assert payload["type"] == "agent_complete"
    assert payload["agent_id"] == "agent-123"
    assert payload["thread_id"] == "thread-abc"
    assert payload["args"] == {"summary": "ok"}
    assert "created_at" in payload


def test_write_event_is_atomic_no_tmp_file_left_behind(spool_dir):
    write_event(spool_dir, "agent_complete", "agent-1", "t1", {})
    tmp_files = list(spool_dir.glob("*.tmp"))
    assert tmp_files == []


def test_read_pending_returns_events_in_filename_sorted_order(spool_dir):
    for i in range(3):
        write_event(spool_dir, "agent_complete", f"agent-{i}", f"t{i}", {"seq": i})
    events = read_pending(spool_dir)
    assert [e.args["seq"] for e in events] == [0, 1, 2]
    assert all(isinstance(e, SpoolEvent) for e in events)


def test_read_pending_ignores_dead_subdir(spool_dir):
    (spool_dir / "dead").mkdir()
    (spool_dir / "dead" / "poison.json").write_text("{}")
    write_event(spool_dir, "agent_complete", "a", "t", {})
    events = read_pending(spool_dir)
    assert len(events) == 1


def test_read_pending_skips_malformed_json_without_raising(spool_dir):
    (spool_dir / "0-bad-1.json").write_text("{not valid json")
    write_event(spool_dir, "agent_complete", "a", "t", {})
    events = read_pending(spool_dir)
    assert len(events) == 1  # malformed file skipped, not raised


def test_move_to_dead_relocates_file_and_writes_reason(spool_dir):
    write_event(spool_dir, "agent_complete", "a", "t", {})
    src = next(spool_dir.glob("*.json"))
    move_to_dead(spool_dir, src, "boom: bad args")
    assert not src.exists()
    dead_files = list((spool_dir / "dead").glob("*.json"))
    assert len(dead_files) == 1
    payload = json.loads(dead_files[0].read_text())
    assert payload["dead_reason"] == "boom: bad args"
