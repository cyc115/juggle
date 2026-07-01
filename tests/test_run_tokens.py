"""read_run_tokens transcript primitive (2026-06-30 orchestration-metrics Task 0)."""
from datetime import datetime, timezone
from pathlib import Path

import juggle_run_tokens as rt

FIX = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"


def test_project_dir_for_cwd():
    assert rt.project_dir_for_cwd("/private/tmp/juggle-juggle-A") == "-private-tmp-juggle-juggle-A"
    assert rt.project_dir_for_cwd("/Users/m/g.x/juggle") == "-Users-m-g-x-juggle"


def test_window_sum_only_inside_record():
    start = datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 30, 12, 10, tzinfo=timezone.utc)
    got = rt.sum_usage_in_window(FIX, start, end)
    assert got == {"input": 200, "output": 20, "cache_read": 7, "cache_write": 2}


def test_read_run_tokens_missing_file_is_zeros():
    run = {"repo_path": "/nope", "session_id": "x", "dispatched_at": "2026-06-30T12:00:00",
           "completed_at": "2026-06-30T12:10:00"}
    assert rt.read_run_tokens(run, projects_root=Path("/does/not/exist")) == {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
